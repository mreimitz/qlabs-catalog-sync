"""Databricks auth + client config.

WP4 / T4.1 (Sonnet). OAuth M2M (service-principal client-credentials) auth, a
``databricks-sdk`` ``WorkspaceClient`` factory, a cheap Unity Catalog reachability
probe, and translation of ``databricks-sdk`` errors onto the SDK's typed exceptions.

Design decisions (see the T4.1 report for the full reasoning):

* **Token acquisition is ours, not databricks-sdk's.** We build the SDK's own
  :class:`~qlabs_catalog_sync_sdk.auth.OAuth2ClientCredentialsProvider` with
  :attr:`~qlabs_catalog_sync_sdk.auth.TokenRequestEncoding.FORM` — the exact shape the
  SDK's ``auth.py`` docstring names Databricks' ``/oidc/v1/token`` as the intended
  consumer of. It runs over an injectable ``httpx.AsyncClient``, so the whole OAuth
  dance (grant_type=client_credentials, scope=all-apis, HTTP Basic, form body) is
  unit-testable with ``respx`` and never touches ``databricks-sdk``'s own transport.
  ``databricks-sdk``'s built-in ``oauth-m2m`` credential strategy talks over
  ``requests`` internally (confirmed by reading the installed package: ``core.py`` /
  ``_base_client.py`` build a ``requests.Session``), which ``respx`` — an httpx mock —
  cannot intercept. Reusing the SDK's own provider keeps the whole auth path testable
  without a live workspace, per the RM-01 "no live tenants; respx mocks and fakes only"
  convention (agent-guide.md, decision-databricks-to-qlik-mvp.md).
* **The vendor wrinkle**: ``WorkspaceClient`` is synchronous. We bridge it with
  ``asyncio.to_thread`` at every call site (:func:`probe_unity_catalog` here; T4.3/T4.4
  will do the same for ``list_changed``/``read``), which is the standard way to keep a
  blocking vendor client off the event loop without re-implementing it.
* **Fresh token per health check.** ``WorkspaceClient`` bakes whatever token it is given
  at construction into a static header closure (confirmed by reading
  ``credentials_provider.py``'s ``pat_auth``: it captures ``cfg.token`` once, so mutating
  ``config.token`` afterwards has no effect). Rather than reimplement the refreshing
  ``oauth-m2m`` strategy, the connector rebuilds ``WorkspaceClient`` from a
  freshly-fetched token every time :meth:`Connector.healthcheck` runs — and per the SDK
  contract, ``healthcheck()`` runs before every sync cycle, so the client used for that
  cycle's reads is never staler than one health-check interval. The OAuth provider's own
  cache (refresh-before-expiry, ~1h token TTL) makes repeated fetches cheap when the
  token is still fresh.
* **Error mapping.** ``databricks-sdk`` raises typed errors from
  ``databricks.sdk.errors`` (``Unauthenticated``/``PermissionDenied`` for 401/403,
  ``TooManyRequests`` for 429, ``InternalError``/``TemporarilyUnavailable`` for 5xx) plus
  ``OSError`` subclasses (via ``requests``) for transport failures.
  :func:`translate_databricks_error` maps these onto the SDK's
  :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError` /
  :class:`~qlabs_catalog_sync_sdk.exceptions.TransientError` so the rest of the
  connector — and the engine — only ever sees the SDK's own exception hierarchy.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors.base import DatabricksError
from databricks.sdk.errors.platform import (
    InternalError,
    PermissionDenied,
    TemporarilyUnavailable,
    TooManyRequests,
    Unauthenticated,
)

from qlabs_catalog_sync_sdk.auth import Clock as AuthClock
from qlabs_catalog_sync_sdk.auth import OAuth2ClientCredentialsProvider, TokenRequestEncoding
from qlabs_catalog_sync_sdk.exceptions import AuthError, ConnectorError, TransientError

from .config import OAUTH_SCOPE, DatabricksConfig

__all__ = [
    "OAUTH_SCOPE",
    "AuthClock",
    "WorkspaceClientFactory",
    "build_oauth_provider",
    "default_workspace_client_factory",
    "fetch_bearer_token",
    "probe_unity_catalog",
    "translate_databricks_error",
]


class _AsyncClientTokenTransport:
    """Adapts ``httpx.AsyncClient.post`` to the SDK's ``TokenTransport`` protocol.

    ``TokenTransport`` (``qlabs_catalog_sync_sdk.auth``) declares
    ``async def post(self, url: str, **kwargs: Any) -> httpx.Response``. At runtime a
    plain ``httpx.AsyncClient`` satisfies that duck type fine (its own docstring says
    so), but ``AsyncClient.post`` is typed with a long list of specific keyword
    parameters rather than ``**kwargs: Any``, which strict-mode structural matching
    rejects. This tiny proxy has the exact declared shape, so passing one through
    :func:`build_oauth_provider` type-checks without editing the SDK (out of scope for
    this task) or loosening anything.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._client.post(url, **kwargs)


def build_oauth_provider(
    config: DatabricksConfig,
    *,
    transport: httpx.AsyncClient,
    clock: AuthClock | None = None,
) -> OAuth2ClientCredentialsProvider:
    """Build the OAuth M2M provider for ``config``, wired to the Databricks wire shape.

    Form-encoded body, HTTP Basic client credentials, ``scope=all-apis`` — exactly
    section 3.2 of the RS-01 Databricks API reference. ``transport`` is injected (never
    constructed here) so tests can hand it a ``respx``-mocked ``httpx.AsyncClient`` and
    production code can share one client across the provider and any direct REST calls.
    ``clock`` is likewise injected (defaults to the provider's own real-time clock) so
    tests can prove refresh-before-expiry caching deterministically, without sleeping.
    """
    return OAuth2ClientCredentialsProvider(
        token_url=config.token_url,
        client_id=config.client_id,
        client_secret=config.client_secret.get_secret_value(),
        transport=_AsyncClientTokenTransport(transport),
        scope=OAUTH_SCOPE,
        encoding=TokenRequestEncoding.FORM,
        clock=clock,
    )


async def fetch_bearer_token(provider: OAuth2ClientCredentialsProvider) -> str:
    """Fetch (or reuse the cached, still-valid) access token as a bare bearer value.

    ``provider.headers()`` returns ``{"Authorization": "Bearer <token>"}``; this strips
    the scheme so callers that need the raw token (building a ``WorkspaceClient``) don't
    each re-parse it. Raises :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError` on any
    failure — that is ``OAuth2ClientCredentialsProvider``'s own behavior, not something
    this function adds.
    """
    headers = await provider.headers()
    value = headers.get("Authorization", "")
    if not value.startswith("Bearer "):
        raise AuthError("OAuth2 token endpoint did not yield a bearer-scheme token")
    return value.removeprefix("Bearer ").strip()


class WorkspaceClientFactory(Protocol):
    """Structural seam for building a ``WorkspaceClient``-shaped object.

    Deliberately duck-typed: a test can inject a fake exposing just enough surface
    (e.g. ``.catalogs.list()``) to exercise :meth:`Connector.healthcheck` without a real
    ``databricks-sdk`` client or any network access.
    """

    def __call__(self, *, host: str, token: str) -> WorkspaceClient: ...


def default_workspace_client_factory(*, host: str, token: str) -> WorkspaceClient:
    """Build a real ``databricks-sdk`` ``WorkspaceClient`` from an already-fetched token.

    Uses the ``pat`` credential strategy (a static ``Authorization: Bearer <token>``
    header) rather than ``databricks-sdk``'s own ``oauth-m2m`` strategy: our OAuth
    provider (:func:`build_oauth_provider`) is already the single source of truth for
    tokens, obtained over a transport we control end to end for testing. Handing
    ``databricks-sdk`` a second, independent set of credentials to manage internally
    would just be a second untestable OAuth flow alongside the first. Construction does
    no I/O — auth is only exercised on the first real request.
    """
    return WorkspaceClient(host=host, token=token, auth_type="pat")


def probe_unity_catalog(client: WorkspaceClient) -> None:
    """Make one cheap, real Unity Catalog call to prove auth + reachability.

    ``catalogs.list(max_results=1)`` is chosen because it is the smallest possible
    read on the UC REST surface (``GET /api/2.1/unity-catalog/catalogs``): any
    authenticated principal can call it (results are simply filtered to what they can
    see, per the endpoint's own docs), it needs no pre-existing object name, and
    ``max_results=1`` bounds it to at most one page. ``catalogs.list()`` returns a lazy
    iterator — ``databricks-sdk`` does not perform the HTTP GET until it is actually
    consumed — so this pulls exactly one item (or confirms there are none) rather than
    just constructing the iterator.

    Synchronous and blocking by design: call sites bridge it with
    ``asyncio.to_thread`` rather than making this function pretend to be async over a
    client that fundamentally is not.
    """
    next(iter(client.catalogs.list(max_results=1)), None)


def translate_databricks_error(exc: DatabricksError | OSError, *, endpoint: str) -> ConnectorError:
    """Map a ``databricks-sdk`` (or transport-level) error onto an SDK typed exception.

    * ``Unauthenticated`` (401) / ``PermissionDenied`` (403) -> :class:`AuthError` —
      not retryable; the engine quarantines the endpoint.
    * ``TooManyRequests`` (429) -> :class:`TransientError`, carrying ``retry_after_secs``
      when Databricks supplied one.
    * ``InternalError`` / ``TemporarilyUnavailable`` (5xx) -> :class:`TransientError`.
    * Any other ``DatabricksError`` or transport-level ``OSError`` (connection reset,
      timeout, DNS failure, ...) -> :class:`TransientError` as the honest fallback: we
      don't know the object is gone or the plan was wrong, only that this attempt
      failed and another might not.

    Only call this from a handler that already caught ``(DatabricksError, OSError)`` —
    it does not itself decide what is worth catching, so a bug that raises something
    else (a ``TypeError`` in our own code, say) keeps propagating instead of being
    silently folded into "transient".
    """
    if isinstance(exc, Unauthenticated | PermissionDenied):
        return AuthError(str(exc), endpoint=endpoint, cause=exc)
    if isinstance(exc, TooManyRequests):
        retry_after = getattr(exc, "retry_after_secs", None)
        return TransientError(
            str(exc),
            endpoint=endpoint,
            cause=exc,
            retry_after_seconds=float(retry_after) if retry_after is not None else None,
        )
    if isinstance(exc, InternalError | TemporarilyUnavailable):
        return TransientError(str(exc), endpoint=endpoint, cause=exc)
    return TransientError(str(exc), endpoint=endpoint, cause=exc)
