"""Auth providers base.

WP1 / T1.5 (Sonnet). Base auth providers for API key, OAuth2 machine-to-machine,
and JWT/key-pair, plus an in-memory token cache with refresh. Connectors select
and configure a provider; secrets never get logged or persisted.

Three providers, one base:

* :class:`ApiKeyAuthProvider` — a static bearer token. Qlik API keys and
  Databricks PATs both take this shape (``Authorization: Bearer <key>``).
* :class:`OAuth2ClientCredentialsProvider` — the OAuth2 client-credentials
  (machine-to-machine) grant. Supports both wire encodings seen in the two MVP
  source/target APIs: a JSON body carrying ``client_id``/``client_secret``
  (Qlik Cloud's ``/oauth/token``), and a form-encoded body authenticated with
  HTTP Basic (Databricks' ``/oidc/v1/token``). See
  ``planning/Research/RS-02-qlik-catalog-api/outputs/qlik-catalog-api-reference.md``
  section 3.2 and
  ``planning/Research/RS-01-databricks-catalog-api/outputs/databricks-catalog-api-reference.md``
  section 3.2 for the exact requests this encodes.
* :class:`JWTAuthProvider` — signs a JWT assertion with a private key
  (RS256/ES256 via ``pyjwt``/``cryptography``) and either uses it directly as
  the bearer token or exchanges it at a token endpoint for an access token.
  Snowflake key-pair auth is the eventual consumer (Track B).

All three share :class:`AuthProvider`, which owns the in-memory token cache: a
token is fetched once, reused while valid, and refreshed *before* expiry (with a
configurable safety margin) rather than reactively after a 401. Concurrent
callers share one fetch via an ``asyncio.Lock`` — two coroutines calling
``headers()`` at once never trigger two token requests.

Dependency note (T1.5 scope): the HTTP wrapper (``http.py``, T1.4) and the SDK's
typed exception hierarchy (``exceptions.py``, T1.7) do not exist yet in this
worktree, so this module deliberately does not import either.

* Instead of ``HttpEndpoint``, the OAuth2 and JWT-exchange providers take a
  ``transport`` constructor argument typed against the small local
  :class:`TokenTransport` protocol below (an ``async def post(...)`` duck type).
  A plain ``httpx.AsyncClient`` satisfies it as-is, so callers do not need any
  adapter; once ``HttpEndpoint`` lands, anything it exposes with a compatible
  ``post`` also satisfies the protocol without a change here.
* Auth failures raise the SDK's typed :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError`,
  re-exported from this module for convenience. It is not retryable: the engine
  quarantines the endpoint rather than retrying with the same bad credentials.

Tokens are held in memory only: never written to disk, never logged, and never
included in a provider's or a cached token's ``repr()``/``str()``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol

import httpx
import jwt
import structlog

from qlabs_catalog_sync_sdk.config import Clock, SystemClock
from qlabs_catalog_sync_sdk.exceptions import AuthError

__all__ = [
    "DEFAULT_REFRESH_MARGIN",
    "ApiKeyAuthProvider",
    "AuthError",
    "AuthProvider",
    "Clock",
    "JWTAuthProvider",
    "OAuth2ClientCredentialsProvider",
    "SystemClock",
    "TokenRequestEncoding",
    "TokenTransport",
]

_log = structlog.get_logger(__name__)

#: How long before a cached token's expiry we proactively refresh it. Chosen to
#: comfortably outrun clock skew and one retried HTTP round trip without
#: refreshing so eagerly that we double request volume.
DEFAULT_REFRESH_MARGIN = timedelta(seconds=60)


class TokenTransport(Protocol):
    """Structural seam for the HTTP client used to call a token endpoint.

    Deliberately duck-typed and minimal (one method, ``**kwargs``) rather than
    pinned to ``http.py``'s not-yet-built ``HttpEndpoint``. A plain
    ``httpx.AsyncClient`` already satisfies this protocol, which is what tests
    and callers pass today.
    """

    async def post(self, url: str, **kwargs: Any) -> httpx.Response: ...


class TokenRequestEncoding(StrEnum):
    """How a client-credentials token request is put on the wire."""

    #: JSON body carrying ``client_id``/``client_secret``/``scope``. Qlik
    #: Cloud's ``/oauth/token``.
    JSON = "json"
    #: Form-encoded body (``grant_type``/``scope``) authenticated with HTTP
    #: Basic (``client_id``:``client_secret``). Databricks' ``/oidc/v1/token``.
    FORM = "form"


class _CachedToken:
    """An in-memory token plus its expiry.

    ``__repr__``/``__str__`` deliberately omit ``value`` — only the expiry is
    ever shown, so this is safe to include in logs or debugger output.
    """

    __slots__ = ("expires_at", "value")

    def __init__(self, value: str, expires_at: datetime | None) -> None:
        self.value = value
        self.expires_at = expires_at

    def __repr__(self) -> str:
        return f"_CachedToken(expires_at={self.expires_at!r})"

    __str__ = __repr__


def _parse_bearer_token_response(response: httpx.Response, issued_at: datetime) -> _CachedToken:
    """Parse a ``{"access_token": ..., "expires_in": ...}`` token response.

    Shared by the OAuth2 client-credentials and JWT-exchange providers, which
    both talk to a standard OAuth2-shaped token endpoint. Raises
    :class:`AuthError` — never caches a partial or malformed response.
    """
    try:
        body: object = response.json()
    except ValueError as exc:
        raise AuthError("token endpoint returned a non-JSON response body") from exc
    if not isinstance(body, dict):
        raise AuthError("token endpoint response body was not a JSON object")
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AuthError("token endpoint response is missing a non-empty 'access_token'")
    expires_in = body.get("expires_in")
    if not isinstance(expires_in, int | float) or isinstance(expires_in, bool):
        raise AuthError("token endpoint response is missing a numeric 'expires_in'")
    expires_at = issued_at + timedelta(seconds=float(expires_in))
    return _CachedToken(value=access_token, expires_at=expires_at)


def _check_and_parse(response: httpx.Response, issued_at: datetime, *, what: str) -> _CachedToken:
    if response.status_code >= 400:
        raise AuthError(f"{what} returned HTTP {response.status_code}")
    return _parse_bearer_token_response(response, issued_at)


class AuthProvider(ABC):
    """Base class for the SDK's auth providers.

    Owns the in-memory token cache and its refresh-before-expiry, concurrency-safe
    acquisition. Subclasses implement :meth:`_fetch_token` to actually obtain a
    fresh token (however they need to — a static value, an HTTP round trip, a
    local JWT signature); everything else (caching, the safety margin, making
    concurrent callers share one fetch, building the header) lives here once.

    "Async-safe" here means safe against concurrent coroutines sharing one event
    loop, which is the SDK's execution model end to end (async throughout, per
    the agent guide) — not safe across OS threads or multiple event loops.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        refresh_margin: timedelta = DEFAULT_REFRESH_MARGIN,
        header_name: str = "Authorization",
        scheme: str | None = "Bearer",
    ) -> None:
        self._clock: Clock = clock or SystemClock()
        self._refresh_margin = refresh_margin
        self._header_name = header_name
        self._scheme = scheme
        self._lock = asyncio.Lock()
        self._cached: _CachedToken | None = None

    @abstractmethod
    async def _fetch_token(self) -> _CachedToken:
        """Obtain a fresh token. Must raise :class:`AuthError` on failure and
        must not mutate any cache itself — the base class does that only after
        this returns successfully, so a failed fetch is never cached."""

    async def headers(self) -> dict[str, str]:
        """Return the header(s) to inject, acquiring or refreshing as needed."""
        token = await self._token()
        value = f"{self._scheme} {token}" if self._scheme else token
        return {self._header_name: value}

    async def _token(self) -> str:
        async with self._lock:
            cached = self._cached
            if cached is None or self._needs_refresh(cached):
                is_refresh = cached is not None
                cached = await self._fetch_token()
                self._cached = cached
                await _log.adebug(
                    "auth.token_fetched",
                    provider=type(self).__name__,
                    refresh=is_refresh,
                    expires_at=cached.expires_at.isoformat() if cached.expires_at else None,
                )
            return cached.value

    def _needs_refresh(self, cached: _CachedToken) -> bool:
        if cached.expires_at is None:
            return False
        return self._clock.now() >= cached.expires_at - self._refresh_margin

    def _repr_fields(self) -> dict[str, object]:
        """Non-secret fields to show in ``repr()``/``str()``. Override per
        subclass; never include a token, API key, client secret, or private
        key here."""
        return {}

    def __repr__(self) -> str:
        fields = ", ".join(f"{k}={v!r}" for k, v in self._repr_fields().items())
        return f"{type(self).__name__}({fields})"

    __str__ = __repr__


class ApiKeyAuthProvider(AuthProvider):
    """A static bearer token: Qlik API keys and Databricks PATs both take this
    shape (``Authorization: Bearer <key>``). Fetched once, never expires, so it
    is cached for the lifetime of the provider."""

    def __init__(
        self,
        api_key: str,
        *,
        header_name: str = "Authorization",
        scheme: str | None = "Bearer",
        clock: Clock | None = None,
    ) -> None:
        super().__init__(clock=clock, header_name=header_name, scheme=scheme)
        self._api_key = api_key

    async def _fetch_token(self) -> _CachedToken:
        return _CachedToken(value=self._api_key, expires_at=None)

    def _repr_fields(self) -> dict[str, object]:
        return {"header_name": self._header_name, "scheme": self._scheme}


class OAuth2ClientCredentialsProvider(AuthProvider):
    """OAuth2 client-credentials (machine-to-machine) grant.

    Posts ``client_id``/``client_secret`` (+ optional ``scope``) to a token
    endpoint, reads back ``access_token``/``expires_in``, and caches the
    result, refreshing before it expires. ``encoding`` selects the wire shape:

    * :attr:`TokenRequestEncoding.JSON` — JSON body including the credentials
      (Qlik Cloud ``/oauth/token``).
    * :attr:`TokenRequestEncoding.FORM` — form-encoded body (``grant_type``,
      optional ``scope``) with the credentials sent as HTTP Basic auth
      (Databricks ``/oidc/v1/token``, typically with ``scope="all-apis"``).
    """

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        transport: TokenTransport,
        scope: str | None = None,
        encoding: TokenRequestEncoding = TokenRequestEncoding.JSON,
        grant_type: str = "client_credentials",
        refresh_margin: timedelta = DEFAULT_REFRESH_MARGIN,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(clock=clock, refresh_margin=refresh_margin)
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport
        self._scope = scope
        self._encoding = encoding
        self._grant_type = grant_type

    async def _fetch_token(self) -> _CachedToken:
        issued_at = self._clock.now()
        try:
            response = await self._post()
        except httpx.HTTPError as exc:
            raise AuthError(
                "OAuth2 client-credentials request failed before a response was received"
            ) from exc
        return _check_and_parse(response, issued_at, what="OAuth2 token endpoint")

    async def _post(self) -> httpx.Response:
        if self._encoding is TokenRequestEncoding.JSON:
            payload: dict[str, str] = {
                "grant_type": self._grant_type,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
            if self._scope:
                payload["scope"] = self._scope
            return await self._transport.post(self._token_url, json=payload)

        form: dict[str, str] = {"grant_type": self._grant_type}
        if self._scope:
            form["scope"] = self._scope
        return await self._transport.post(
            self._token_url,
            data=form,
            auth=(self._client_id, self._client_secret),
        )

    def _repr_fields(self) -> dict[str, object]:
        return {
            "token_url": self._token_url,
            "client_id": self._client_id,
            "encoding": self._encoding.value,
        }


class JWTAuthProvider(AuthProvider):
    """JWT / key-pair auth: signs a JWT assertion with a private key and
    either uses it directly as the bearer token, or exchanges it at a token
    endpoint for an access token (set ``token_url`` for the latter).

    Snowflake key-pair auth (Track B) is the eventual consumer of the
    direct-use mode; the exchange mode covers JWT-bearer-grant token
    endpoints generally.
    """

    def __init__(
        self,
        *,
        private_key: str | bytes,
        issuer: str,
        subject: str | None = None,
        audience: str | None = None,
        algorithm: Literal["RS256", "ES256"] = "RS256",
        key_id: str | None = None,
        additional_claims: Mapping[str, object] | None = None,
        assertion_ttl: timedelta = timedelta(minutes=5),
        token_url: str | None = None,
        transport: TokenTransport | None = None,
        grant_type: str = "urn:ietf:params:oauth:grant-type:jwt-bearer",
        scope: str | None = None,
        refresh_margin: timedelta = DEFAULT_REFRESH_MARGIN,
        clock: Clock | None = None,
    ) -> None:
        if token_url is not None and transport is None:
            raise ValueError("transport is required when token_url is set")
        super().__init__(clock=clock, refresh_margin=refresh_margin)
        self._private_key = private_key
        self._issuer = issuer
        self._subject = subject
        self._audience = audience
        self._algorithm: Literal["RS256", "ES256"] = algorithm
        self._key_id = key_id
        self._additional_claims: Mapping[str, object] = additional_claims or {}
        self._assertion_ttl = assertion_ttl
        self._token_url = token_url
        self._transport = transport
        self._grant_type = grant_type
        self._scope = scope

    def _sign_assertion(self) -> tuple[str, datetime, datetime]:
        issued_at = self._clock.now()
        expires_at = issued_at + self._assertion_ttl
        claims: dict[str, object] = {
            "iss": self._issuer,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        if self._subject is not None:
            claims["sub"] = self._subject
        if self._audience is not None:
            claims["aud"] = self._audience
        claims.update(self._additional_claims)
        jwt_headers = {"kid": self._key_id} if self._key_id else None
        assertion = jwt.encode(
            claims, self._private_key, algorithm=self._algorithm, headers=jwt_headers
        )
        return assertion, issued_at, expires_at

    async def _fetch_token(self) -> _CachedToken:
        assertion, issued_at, expires_at = self._sign_assertion()
        if self._token_url is None:
            return _CachedToken(value=assertion, expires_at=expires_at)

        transport = self._transport
        assert transport is not None  # enforced in __init__

        form: dict[str, str] = {"grant_type": self._grant_type, "assertion": assertion}
        if self._scope:
            form["scope"] = self._scope
        try:
            response = await transport.post(self._token_url, data=form)
        except httpx.HTTPError as exc:
            raise AuthError(
                "JWT-bearer token exchange failed before a response was received"
            ) from exc
        return _check_and_parse(response, issued_at, what="JWT-bearer token exchange")

    def _repr_fields(self) -> dict[str, object]:
        return {
            "issuer": self._issuer,
            "algorithm": self._algorithm,
            "token_url": self._token_url,
            "mode": "exchange" if self._token_url else "direct",
        }
