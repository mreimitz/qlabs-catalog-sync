"""Qlik OAuth2 M2M auth and HTTP endpoint wiring.

WP3 / T3.1. Builds the SDK's :class:`~qlabs_catalog_sync_sdk.auth.OAuth2ClientCredentialsProvider`
(T1.5) configured exactly the way Qlik Cloud's client-credentials grant wants it — JSON body,
POST ``<base_url>/oauth/token`` — and wires it into an SDK
:class:`~qlabs_catalog_sync_sdk.http.HttpEndpoint` (T1.4) bound to the tenant's base URL
(RS-02 ``qlik-catalog-api-reference.md`` section 3.2). This module owns no token logic of
its own: caching, refresh-before-expiry and single-flight concurrency are entirely the
SDK provider's job.

Also provides :func:`classify_response_error` / :func:`classify_transport_error`, which
turn the raw ``httpx`` errors ``HttpEndpoint`` raises (its own docstring: T1.7's typed
exceptions "can be layered on top later by catching ``httpx.HTTPStatusError`` ... at the
call site") into the SDK's typed exception vocabulary, per RS-02 sections 3.2-3.4:
401/403 -> :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError` (quarantine), 404 ->
:class:`~qlabs_catalog_sync_sdk.exceptions.NotFound`, 429/5xx (retries already exhausted
by ``HttpEndpoint``) -> :class:`~qlabs_catalog_sync_sdk.exceptions.TransientError`.
:meth:`Connector.healthcheck` (``__init__.py``) is the first caller; later read/write
tasks (T3.3-T3.7) are free to reuse the same classification at their own call sites.
"""

from __future__ import annotations

import httpx

from qlabs_catalog_sync_sdk.auth import (
    Clock,
    OAuth2ClientCredentialsProvider,
    TokenRequestEncoding,
)
from qlabs_catalog_sync_sdk.exceptions import AuthError, ConnectorError, NotFound, TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint

from .config import QlikConfig

__all__ = [
    "build_http_endpoint",
    "classify_response_error",
    "classify_transport_error",
]


def build_http_endpoint(
    config: QlikConfig,
    *,
    clock: Clock | None = None,
    **http_kwargs: object,
) -> tuple[HttpEndpoint, OAuth2ClientCredentialsProvider, httpx.AsyncClient]:
    """Build the authenticated ``HttpEndpoint`` for one configured Qlik tenant.

    Returns ``(http, oauth_provider, token_client)``:

    * ``http`` — an ``HttpEndpoint`` bound to ``config.base_url`` with the OAuth2
      provider injected as its auth. Every call through it carries a fresh (or
      refresh-before-expiry-cached) ``Authorization: Bearer <token>`` header.
    * ``oauth_provider`` — the underlying provider, kept for callers that want direct
      introspection (tests) rather than going through an HTTP call.
    * ``token_client`` — a small dedicated ``httpx.AsyncClient`` used only to POST
      ``/oauth/token``. It is deliberately separate from the pooled client
      ``HttpEndpoint`` manages internally for API calls (that client is private to
      ``HttpEndpoint`` and not reusable as a token transport), so the caller
      (``Connector.setup``/``Connector.close``) owns and must close it alongside
      ``http.aclose()``.

    ``clock`` is forwarded to the OAuth2 provider unchanged — pass ``ctx.clock`` from
    :class:`~qlabs_catalog_sync_sdk.config.ConnectorContext` in production so token
    refresh timing follows the same clock as the rest of the connector, and an injected
    ``ManualClock`` in tests so refresh-before-expiry is provable without sleeping.
    ``http_kwargs`` are forwarded to ``HttpEndpoint`` unchanged (timeouts, pooling,
    retry tuning, or a fake ``sleep``/``clock`` for deterministic retry tests); no field
    of ``QlikConfig`` exposes these — they are not part of what a connector operator
    configures.
    """
    token_client = httpx.AsyncClient()
    oauth_provider = OAuth2ClientCredentialsProvider(
        token_url=config.token_url,
        client_id=config.client_id,
        client_secret=config.client_secret.get_secret_value(),
        transport=token_client,
        scope=config.scope,
        encoding=TokenRequestEncoding.JSON,
        clock=clock,
    )
    http = HttpEndpoint(
        config.base_url,
        auth=oauth_provider,
        **http_kwargs,  # type: ignore[arg-type]
    )
    return http, oauth_provider, token_client


def classify_response_error(
    exc: httpx.HTTPStatusError,
    *,
    endpoint: str,
    entity_type: str | None = None,
) -> ConnectorError:
    """Turn a terminal HTTP error status from ``HttpEndpoint`` into an SDK typed error.

    "Terminal" here means what ``HttpEndpoint.request`` actually raises: either a status
    that is never retried (anything but 429/5xx), or a 429/5xx once its retries are
    exhausted. Rate-limit and transient-server handling is therefore already done by the
    time this runs; a 429/5xx reaching here means genuinely unwell, not "about to
    succeed on the next attempt" (RS-02 section 3.4).
    """
    status = exc.response.status_code
    if status in (401, 403):
        return AuthError(
            f"Qlik rejected the request as unauthorized (HTTP {status})",
            endpoint=endpoint,
            entity_type=entity_type,
            cause=exc,
        )
    if status == 404:
        return NotFound(
            "the requested Qlik resource was not found",
            endpoint=endpoint,
            entity_type=entity_type,
            cause=exc,
        )
    if status == 429 or status >= 500:
        return TransientError(
            f"Qlik returned HTTP {status} and retries were exhausted",
            endpoint=endpoint,
            entity_type=entity_type,
            cause=exc,
        )
    return ConnectorError(
        f"Qlik returned HTTP {status}",
        endpoint=endpoint,
        entity_type=entity_type,
        cause=exc,
    )


def classify_transport_error(
    exc: httpx.TransportError,
    *,
    endpoint: str,
    entity_type: str | None = None,
) -> TransientError:
    """Turn a terminal transport failure (retries exhausted) into a ``TransientError``."""
    return TransientError(
        f"could not reach the Qlik tenant: {type(exc).__name__}",
        endpoint=endpoint,
        entity_type=entity_type,
        cause=exc,
    )
