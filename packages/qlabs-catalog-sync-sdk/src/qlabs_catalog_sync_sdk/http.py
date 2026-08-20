"""HTTP helper — httpx wrapper with retry/backoff and pagination.

WP1 / T1.4 (Sonnet). Provides an httpx-based endpoint wrapper (base URL, auth
injection, timeouts, connection pooling), tenacity-driven retry/backoff that honors
429/Retry-After, and pagination helpers. Connectors use this instead of calling
httpx directly.

Auth injection (T1.5 is not built yet in this worktree)
---------------------------------------------------------
The SDK's eventual ``AuthProvider`` base (T1.5) does not exist here, so
:class:`HttpEndpoint` accepts auth through a small structural :class:`Protocol`,
:class:`AuthHeaderProvider`, defined locally in this module rather than imported.
Any object exposing an async ``get_headers() -> Mapping[str, str]`` satisfies it —
including T1.5's future ``AuthProvider`` — so wiring that provider in later is a
call-site no-op: nothing here needs to change, only T1.5's class needs the method.
A static ``(scheme, token)`` tuple is also accepted directly, matching the
``HttpEndpoint(ctx.config.base_url, auth=("Bearer", ctx.config.api_key))`` usage
shown in the RS-08 connector SDK spec and the agent guide's connector skeleton.

Error classification (T1.7 is not built yet in this worktree)
-----------------------------------------------------------------
This module raises httpx's own exception types — :class:`httpx.HTTPStatusError`
for a terminal (non-retried, or retries-exhausted) error response, and
:class:`httpx.TransportError` for a terminal transport failure — rather than
inventing SDK-specific exceptions. T1.7's typed exceptions (``TransientError``,
``AuthError``, ``NotFound``, ``ConflictError``, ``CapabilityError``) can be layered
on top later by catching ``httpx.HTTPStatusError`` and switching on
``exc.response.status_code`` at the call site, without any change to how
:class:`HttpEndpoint` is called.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Mapping,
    Sequence,
)
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
)

__all__ = ["AuthHeaderProvider", "HttpEndpoint"]

_logger = structlog.get_logger("qlabs_catalog_sync_sdk.http")

_RETRYABLE_STATUS_FLOOR = 500


class AuthHeaderProvider(Protocol):
    """Structural contract for injecting auth headers into every request.

    Defined locally because the SDK's ``AuthProvider`` base (T1.5) does not exist
    in this worktree yet. This is deliberately the smallest possible surface — one
    async method returning header key/value pairs — so any future provider
    (API key, OAuth2 M2M, JWT/key-pair, with token caching and refresh) satisfies
    it structurally without inheriting from anything declared here.
    """

    async def get_headers(self) -> Mapping[str, str]:
        """Return headers to merge into the outgoing request (e.g. ``Authorization``)."""
        ...


class _StaticAuth(httpx.Auth):
    """Fixed ``<scheme> <credential>`` Authorization header, e.g. ``Bearer <token>``."""

    def __init__(self, scheme: str, credential: str) -> None:
        self._header_value = f"{scheme} {credential}"

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = self._header_value
        yield request


class _ProviderAuth(httpx.Auth):
    """Adapts an :class:`AuthHeaderProvider` into an httpx auth flow."""

    def __init__(self, provider: AuthHeaderProvider) -> None:
        self._provider = provider

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        headers = await self._provider.get_headers()
        for key, value in headers.items():
            request.headers[key] = value
        yield request


def _build_auth(auth: AuthHeaderProvider | tuple[str, str] | None) -> httpx.Auth | None:
    if auth is None:
        return None
    if isinstance(auth, tuple):
        scheme, credential = auth
        return _StaticAuth(scheme, credential)
    return _ProviderAuth(auth)


def _parse_retry_after(value: str, *, now: datetime) -> float | None:
    """Parse a ``Retry-After`` header: delta-seconds or an HTTP-date, per RFC 9110 §10.2.3."""
    value = value.strip()
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return max((target - now).total_seconds(), 0.0)


def _get_path(obj: Any, path: Sequence[str]) -> Any | None:
    current = obj
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= _RETRYABLE_STATUS_FLOOR
    return isinstance(exc, httpx.TransportError)


class HttpEndpoint:
    """A configured async HTTP client for one API base URL.

    Wraps :class:`httpx.AsyncClient` with base URL, auth injection, connect/read
    timeouts, connection pooling, tenacity-driven retry/backoff (honoring
    ``Retry-After`` on 429, jittered exponential backoff on 429/5xx/transport
    errors otherwise, never retrying any other 4xx), and cursor/offset pagination
    helpers that return async iterators.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth: AuthHeaderProvider | tuple[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        write_timeout: float = 30.0,
        pool_timeout: float = 5.0,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        max_attempts: int = 5,
        backoff_base_seconds: float = 0.5,
        backoff_max_seconds: float = 20.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._sleep = sleep if sleep is not None else _default_sleep
        self._clock = clock if clock is not None else _default_clock
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=_build_auth(auth),
            headers=dict(headers) if headers is not None else None,
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=write_timeout,
                pool=pool_timeout,
            ),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
            ),
            transport=transport,
        )

    async def aclose(self) -> None:
        """Release the underlying connection pool. Safe to call more than once."""
        await self._client.aclose()

    async def __aenter__(self) -> HttpEndpoint:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _wait_for_retry(self, retry_state: RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome is not None else None
        if isinstance(exc, httpx.HTTPStatusError):
            header = exc.response.headers.get("retry-after")
            if header:
                honored = _parse_retry_after(header, now=self._clock())
                if honored is not None:
                    return honored
        return self._exponential_backoff(retry_state.attempt_number)

    def _exponential_backoff(self, attempt_number: int) -> float:
        exponent = max(attempt_number - 1, 0)
        capped = min(self._backoff_base_seconds * (2**exponent), self._backoff_max_seconds)
        return random.uniform(0, capped)

    def _log_before_sleep(self, retry_state: RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome is not None else None
        status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        wait_seconds = retry_state.next_action.sleep if retry_state.next_action is not None else 0.0
        _logger.warning(
            "http.retry",
            base_url=self._base_url,
            attempt=retry_state.attempt_number,
            status_code=status_code,
            error_type=None if status_code is not None else type(exc).__name__,
            wait_seconds=round(wait_seconds, 3),
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send one logical request, retrying on 429/5xx/transport errors.

        Returns the response on success (2xx/3xx). Raises
        :class:`httpx.HTTPStatusError` for a 4xx that is not 429, or for a
        429/5xx once retries are exhausted; raises :class:`httpx.TransportError`
        for a transport failure once retries are exhausted.
        """

        async def _send() -> httpx.Response:
            response = await self._client.request(method, url, **kwargs)
            response.raise_for_status()
            return response

        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._wait_for_retry,
            retry=retry_if_exception(_is_retryable),
            before_sleep=self._log_before_sleep,
            sleep=self._sleep,
            reraise=True,
        )
        return await retrying(_send)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def paginate_cursor(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        items_key: str = "data",
        next_href_path: Sequence[str] = ("links", "next", "href"),
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Cursor-style pagination: follow ``links.next.href`` (Qlik) until absent.

        Fetches lazily — a page is only requested once the previous page's items
        have all been yielded and the caller asks for more, so a consumer that
        stops early (e.g. ``break``) never triggers a page it will not see.
        """
        next_url: str | None = url
        next_params: Mapping[str, Any] | None = params
        while next_url is not None:
            response = await self.request(method, next_url, params=next_params, **kwargs)
            body = response.json()
            items = body.get(items_key, []) if isinstance(body, dict) else []
            for item in items:
                yield item
            next_href = _get_path(body, next_href_path)
            next_url = next_href if isinstance(next_href, str) and next_href else None
            next_params = None

    async def paginate_offset(
        self,
        method: str,
        url: str,
        *,
        items_key: str,
        params: Mapping[str, Any] | None = None,
        max_results: int | None = None,
        max_results_param: str = "max_results",
        page_token_param: str = "page_token",
        next_token_key: str = "next_page_token",
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Offset/token pagination: pass ``next_page_token`` back as ``page_token``
        (Databricks) until absent.

        Fetches lazily, one page per call, and stops as soon as a page omits (or
        empties) ``next_token_key`` — including an empty first page, which yields
        nothing and makes exactly one request.
        """
        base_params: dict[str, Any] = dict(params or {})
        if max_results is not None:
            base_params[max_results_param] = max_results
        page_token: str | None = base_params.pop(page_token_param, None)
        while True:
            call_params = dict(base_params)
            if page_token is not None:
                call_params[page_token_param] = page_token
            response = await self.request(method, url, params=call_params, **kwargs)
            body = response.json()
            items = body.get(items_key, []) if isinstance(body, dict) else []
            for item in items:
                yield item
            next_token = body.get(next_token_key) if isinstance(body, dict) else None
            if not next_token:
                return
            page_token = str(next_token)


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _default_clock() -> datetime:
    return datetime.now(UTC)
