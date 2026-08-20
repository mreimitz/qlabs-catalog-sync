"""QLabs Catalog Sync — Databricks connector (WP4).

Read-only source connector: reads Unity Catalog objects plus shares/listings into
neutral entities. It never writes (Qlik is the sole write target in v1). Depends only
on the SDK plus ``databricks-sdk``. Registered as ``databricks`` under the
``qlabs_catalog_sync.connectors`` entry-point group.

This module (T4.1) implements the connector's identity, config wiring, ``setup()`` and
``healthcheck()``. ``capabilities()`` (T4.2), ``list_changed()`` (T4.3), ``read()``
(T4.4) and source-to-neutral mapping (T4.5) are later tasks; their methods are present
(the ABC requires it to instantiate) but raise ``NotImplementedError`` rather than any
placeholder that could pass for real behavior. The write path (``create``/``update``/
``delete``) is deliberately left untouched: the inherited ``Connector`` defaults already
refuse with ``CapabilityError``, which is exactly what a read-only source connector
wants.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors.base import DatabricksError

from qlabs_catalog_sync_sdk.auth import OAuth2ClientCredentialsProvider
from qlabs_catalog_sync_sdk.config import ConnectorContext
from qlabs_catalog_sync_sdk.contract import (
    CapabilityManifestBase,
    HealthStatus,
    ListChangedResult,
    Watermark,
)
from qlabs_catalog_sync_sdk.contract import Connector as ConnectorABC
from qlabs_catalog_sync_sdk.exceptions import AuthError, TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import EntityType, IdentityRef, NeutralEntity

from . import changes, read
from .auth import (
    AuthClock,
    WorkspaceClientFactory,
    build_oauth_provider,
    default_workspace_client_factory,
    fetch_bearer_token,
    probe_unity_catalog,
    translate_databricks_error,
)
from .config import DatabricksConfig
from .manifest import manifest_for_config

__all__ = ["Connector"]


class Connector(ConnectorABC):
    """The Databricks read-only source connector.

    ``ConfigModel`` is :class:`~qlabs_connector_databricks.config.DatabricksConfig`.
    ``setup()`` authenticates via OAuth M2M and builds a ``databricks-sdk``
    ``WorkspaceClient``; ``healthcheck()`` refreshes that client's token and makes one
    cheap Unity Catalog call to prove the credentials work and the workspace is
    reachable.
    """

    name = "databricks"
    ConfigModel = DatabricksConfig

    def __init__(
        self,
        *,
        workspace_client_factory: WorkspaceClientFactory | None = None,
        transport: httpx.AsyncClient | None = None,
        clock: AuthClock | None = None,
    ) -> None:
        """``workspace_client_factory``, ``transport`` and ``clock`` are test seams.

        Production code never passes any of them: the connector builds a real
        ``WorkspaceClient``, owns (and closes) its own ``httpx.AsyncClient``, and lets
        the OAuth provider use real wall-clock time. Tests inject a fake factory (to
        exercise ``healthcheck()`` without a live workspace), a ``respx``-mocked
        transport (to exercise the OAuth token fetch), and/or a fake clock (to prove
        refresh-before-expiry caching deterministically).
        """
        super().__init__()
        self._workspace_client_factory: WorkspaceClientFactory = (
            workspace_client_factory or default_workspace_client_factory
        )
        self._owns_transport = transport is None
        self._transport: httpx.AsyncClient = (
            transport if transport is not None else httpx.AsyncClient()
        )
        self._clock = clock
        self._token_provider: OAuth2ClientCredentialsProvider | None = None
        self._client: WorkspaceClient | None = None
        self._http: HttpEndpoint | None = None
        self._ctx: ConnectorContext[DatabricksConfig] | None = None

    # -- declaration -----------------------------------------------------------------

    def capabilities(self) -> CapabilityManifestBase:
        """What this connector genuinely supports — see ``manifest.py``.

        Every field is ``ro`` or ``na``: this is a read-only source connector, so the
        base class's write refusals are honest rather than incidental. ``tags`` (and
        ``classifications``, which Databricks realizes through the same mechanism) are
        declared readable only when a SQL warehouse is configured, because Unity Catalog
        object tags are reachable only through ``INFORMATION_SCHEMA`` over the Statement
        Execution API (D6).

        Depends on ``setup()`` having run, since the manifest varies with the resolved
        config — that is the order RS-08's connector lifecycle already specifies
        (discover, configure, ``setup``, then ``capabilities``).
        """
        if self._ctx is None:
            raise RuntimeError(
                "capabilities() needs the resolved config: call setup(ctx) first"
            )
        return manifest_for_config(self._ctx.config)

    # -- lifecycle ---------------------------------------------------------------------

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        """Authenticate via OAuth M2M and build the ``databricks-sdk`` workspace client.

        Fetches the initial access token (the actual "authenticate" step — a bad
        client id/secret surfaces here as :class:`AuthError`, propagated rather than
        swallowed) and constructs :attr:`_client` from it. Called once per endpoint,
        before any other I/O method, per the ``Connector`` contract.
        """
        self._ctx = ctx
        config = ctx.config
        self._token_provider = build_oauth_provider(
            config, transport=self._transport, clock=self._clock
        )
        await self._refresh_client()
        # Unity Catalog's REST surface is read straight over HTTP (read.py, changes.py):
        # databricks-sdk's client is synchronous and cannot be intercepted by respx, so
        # the SDK's HttpEndpoint carries the retry/backoff and pagination those modules
        # need. The OAuth provider satisfies its auth protocol directly.
        self._http = HttpEndpoint(config.host, auth=self._token_provider)
        ctx.logger.info(
            "databricks.setup.complete",
            host=config.host,
            has_sql_warehouse=config.has_sql_warehouse,
        )

    async def _refresh_client(self) -> None:
        """Rebuild :attr:`_client` from a freshly-fetched (or still-cached) token.

        ``WorkspaceClient``'s ``pat`` credential strategy captures its token once at
        construction time (confirmed by reading ``databricks-sdk``'s
        ``credentials_provider.py``: ``pat_auth`` closes over ``cfg.token`` and never
        re-reads it), so there is no way to "refresh" one in place — rebuilding is the
        correct fix, and it's cheap: construction does no I/O.
        """
        assert self._token_provider is not None and self._ctx is not None
        token = await fetch_bearer_token(self._token_provider)
        self._client = self._workspace_client_factory(host=self._ctx.config.host, token=token)

    async def healthcheck(self) -> HealthStatus:
        """Refresh the token, then make one cheap Unity Catalog call.

        Runs before every sync cycle per the ``Connector`` contract, so this doubles as
        the connector's token-refresh point: the ``WorkspaceClient`` rebuilt here is
        never staler than one health-check interval by the time ``read()``/
        ``list_changed()`` (T4.3/T4.4) use it.
        """
        if self._ctx is None:
            raise RuntimeError("setup() must be called before healthcheck()")
        endpoint = self._ctx.endpoint

        try:
            await self._refresh_client()
        except AuthError as exc:
            return HealthStatus.unhealthy(endpoint, reason=str(exc))
        except TransientError as exc:
            return HealthStatus.degraded(endpoint, reason=str(exc))

        assert self._client is not None
        try:
            await asyncio.to_thread(probe_unity_catalog, self._client)
        except (DatabricksError, OSError) as exc:
            mapped = translate_databricks_error(exc, endpoint=endpoint)
            if isinstance(mapped, AuthError):
                return HealthStatus.unhealthy(endpoint, reason=str(mapped))
            return HealthStatus.degraded(endpoint, reason=str(mapped))

        return HealthStatus.healthy(endpoint, details={"probe": "unity_catalog.catalogs.list"})

    async def close(self) -> None:
        """Release the HTTP endpoint and the owned ``httpx.AsyncClient``, if any."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._owns_transport:
            await self._transport.aclose()

    # -- read path (T4.3 / T4.4) --------------------------------------------------------

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        """UC objects changed since ``since``, plus the proposed next watermark.

        Unity Catalog has no server-side "modified since" filter, so this compares a
        canonical checksum per object against the snapshot carried in the watermark —
        see ``changes.py``. Identity comes from ``read.py``'s builders, so a ref listed
        here is exactly the ref :meth:`read` accepts.
        """
        return await changes.list_changed(
            self._require_http(), entity_type, since, endpoint=self.name
        )

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        """The current state of ``ref`` as a neutral entity with field envelopes.

        A ``DATA_PRODUCT`` ref is a Unity Catalog schema and a ``DATASET`` ref one of
        its tables or views (D1).
        """
        return await read.read_entity(
            self._require_http(),
            ref,
            catalog_schema_patterns=self._require_ctx().config.catalog_schema_patterns,
        )

    def _require_http(self) -> HttpEndpoint:
        if self._http is None:
            raise RuntimeError("setup() must be called before the read path is used")
        return self._http

    def _require_ctx(self) -> ConnectorContext[DatabricksConfig]:
        if self._ctx is None:
            raise RuntimeError("setup() must be called before the read path is used")
        return self._ctx

    # -- write path: intentionally NOT overridden ---------------------------------------
    # create()/update()/delete() inherit the ConnectorABC defaults, which refuse with
    # CapabilityError. Databricks is a read-only source connector (v1 scope guardrail);
    # overriding any of these would violate it.
