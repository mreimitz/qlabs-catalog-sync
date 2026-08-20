"""QLabs Catalog Sync — Qlik connector (WP3).

The sole WRITE connector in v1: metadata flows from source catalogs into Qlik, and Qlik
is the only write target. Depends only on the SDK plus httpx. Registered via the
``qlabs_catalog_sync.connectors`` entry-point group as ``qlik`` (see ``pyproject.toml``).

T3.1 (this task) builds auth, config and connector setup: :class:`QlikConfig`
(``config.py``), the OAuth2 machine-to-machine wiring (``auth.py``), and here,
:class:`Connector` — its ``setup()`` (builds the authenticated ``HttpEndpoint``) and
``healthcheck()`` (proves the credentials work and the configured target space is
reachable). ``list_changed``/``read`` (T3.3) and the write paths (T3.4/T3.5/T3.6/T3.7) are **not**
implemented here — see the placeholder note on each method below.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from qlabs_catalog_sync_sdk.auth import OAuth2ClientCredentialsProvider
from qlabs_catalog_sync_sdk.contract import (
    CapabilityManifestBase,
    ConnectorContext,
    EntityType,
    HealthState,
    HealthStatus,
    IdentityRef,
    ListChangedResult,
    NeutralEntity,
    Watermark,
    WriteResult,
)
from qlabs_catalog_sync_sdk.contract import Connector as ConnectorABC
from qlabs_catalog_sync_sdk.exceptions import AuthError, ConnectorError, TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint

from . import lifecycle, read, write
from .auth import build_http_endpoint, classify_response_error, classify_transport_error
from .config import QlikConfig
from .manifest import qlik_capability_manifest
from .resolve import DatasetIdentityLookup

__all__ = ["Connector"]


async def _no_identity_binding(neutral_id: uuid.UUID) -> str | None:
    """Default dataset identity seam: no answer.

    The engine owns the IdentityMap, and a connector may never import it, so it injects
    a real lookup before ``setup()``. Answering "unknown" is honest; the writer then
    falls back to a name match within the target space and reports whatever it still
    cannot place (decision D2).
    """
    del neutral_id
    return None


class Connector(ConnectorABC):
    """The Qlik connector — sole write target in v1.

    Holds no sync state (per the contract): everything here is either static
    declaration (``name``, ``ConfigModel``) or live clients built once in :meth:`setup`
    and reused for the connector's lifetime.
    """

    name: ClassVar[str] = "qlik"
    ConfigModel: ClassVar[type[QlikConfig]] = QlikConfig

    def __init__(self) -> None:
        super().__init__()
        self.ctx: ConnectorContext[QlikConfig] | None = None
        self.http: HttpEndpoint | None = None
        self.writer: write.QlikWriter | None = None
        self.lifecycle: lifecycle.LifecycleActions | None = None
        #: Destructive actions this instance may perform. Empty by design: D4 says v1
        #: never deletes in Qlik and D7 makes activation opt-in per pair, so a caller
        #: has to name an action explicitly before it can ever be issued.
        self.enabled_destructive_actions: frozenset[lifecycle.DestructiveAction] = frozenset()
        self.dataset_identity_lookup: DatasetIdentityLookup = _no_identity_binding
        self.dataset_name_lookup: write.DatasetNameLookup = write.no_dataset_names
        self._oauth_provider: OAuth2ClientCredentialsProvider | None = None
        self._token_client: httpx.AsyncClient | None = None

    # -- lifecycle -----------------------------------------------------------------

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        """Build the authenticated ``HttpEndpoint`` for this configured Qlik tenant.

        Called once per endpoint before any other I/O method (contract requirement).
        No I/O happens here: the OAuth2 token is fetched lazily on the first real
        request, not eagerly during setup.
        """
        config: QlikConfig = ctx.config
        http, oauth_provider, token_client = build_http_endpoint(config, clock=ctx.clock)
        self.ctx = ctx
        self.http = http
        self.writer = write.build_writer(
            http,
            endpoint=self.name,
            tenant_id=self._tenant_id(),
            space_id=ctx.config.space_id,
            dataset_identity_lookup=self.dataset_identity_lookup,
            dataset_name_lookup=self.dataset_name_lookup,
        )
        self.lifecycle = lifecycle.build_lifecycle_actions(
            http,
            endpoint=self.name,
            enabled_actions=self.enabled_destructive_actions,
        )
        self._oauth_provider = oauth_provider
        self._token_client = token_client
        await ctx.logger.ainfo(
            "qlik.setup.complete",
            base_url=config.base_url,
            space_id=config.space_id,
            client_id=config.client_id,
        )

    async def healthcheck(self) -> HealthStatus:
        """Verify the configured credentials work and the target space is reachable.

        Calls ``GET /api/v1/spaces/{space_id}`` against the configured target space
        (RS-02 section 3.2's own authenticated-call example is a ``spaces`` GET; using
        the *configured* space rather than a generic list call also proves the specific
        config this endpoint will write against, not just that some token works).

        Failures are mapped honestly: an auth failure (401/403, or the OAuth2 token
        exchange itself failing) is not retryable, so it reports
        :attr:`~qlabs_catalog_sync_sdk.contract.HealthState.UNHEALTHY` — the engine
        quarantines the endpoint. A rate-limit or transient server/network failure
        (429/5xx once ``HttpEndpoint``'s own retries are exhausted, or a transport
        error) is retryable, so it reports
        :attr:`~qlabs_catalog_sync_sdk.contract.HealthState.DEGRADED` — still scheduled,
        expected to recover on its own. Anything else unrecognized (e.g. the configured
        space does not exist) fails closed as ``UNHEALTHY`` rather than being reported
        healthy on a guess.
        """
        if self.ctx is None or self.http is None:
            raise RuntimeError(f"connector {self.name!r}: setup() must run before healthcheck()")
        ctx = self.ctx
        http = self.http
        config: QlikConfig = ctx.config
        checked_at = ctx.clock.now()

        try:
            response = await http.get(f"/api/v1/spaces/{config.space_id}")
        except AuthError as exc:
            # The OAuth2 token exchange itself failed (bad client id/secret, token
            # endpoint unreachable in an auth-shaped way) — raised directly by the SDK's
            # OAuth2ClientCredentialsProvider, before any API call was even attempted.
            return await self._unhealthy(exc, checked_at)
        except httpx.HTTPStatusError as exc:
            error = classify_response_error(exc, endpoint=self.name)
            return await self._unhealthy(error, checked_at)
        except httpx.TransportError as exc:
            error = classify_transport_error(exc, endpoint=self.name)
            return await self._unhealthy(error, checked_at)

        await ctx.logger.ainfo("qlik.healthcheck.ok", status_code=response.status_code)
        return HealthStatus.healthy(
            self.name,
            checked_at=checked_at,
            details={"status_code": response.status_code, "space_id": config.space_id},
        )

    async def _unhealthy(self, error: ConnectorError, checked_at: datetime) -> HealthStatus:
        """Build the non-healthy :class:`HealthStatus` for a classified connector error.

        Retryable errors (:class:`TransientError`) degrade rather than quarantine;
        everything else — chiefly :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError`
        — is unhealthy, since the engine must stop scheduling this endpoint until the
        configuration is fixed.
        """
        assert self.ctx is not None  # narrowed by the caller before `_unhealthy` runs
        state = (
            HealthState.DEGRADED if isinstance(error, TransientError) else HealthState.UNHEALTHY
        )
        reason = str(error)
        await self.ctx.logger.awarning(
            "qlik.healthcheck.failed", state=state.value, reason=reason
        )
        if state is HealthState.DEGRADED:
            return HealthStatus.degraded(self.name, reason, checked_at=checked_at)
        return HealthStatus.unhealthy(self.name, reason, checked_at=checked_at)

    async def close(self) -> None:
        """Release both HTTP clients this connector owns: the pooled API client inside
        ``HttpEndpoint`` and the dedicated token-exchange client from ``auth.py``."""
        if self.http is not None:
            await self.http.aclose()
        if self._token_client is not None:
            await self._token_client.aclose()

    # -- declaration -----------------------------------------------------------------

    def capabilities(self) -> CapabilityManifestBase:
        """What this connector genuinely supports — see ``manifest.py``.

        Data products are writable through the closed eight-path JSON Patch enum with
        ETag concurrency and full-replace arrays; datasets are read-only (D2: the
        connector resolves datasets that already exist, it never creates one); glossary
        entities are unsupported in v1 (D5). The engine plans strictly from this.
        """
        return qlik_capability_manifest()

    # -- read path (T3.3) -------------------------------------------------------------

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        """Candidates changed since ``since``, plus the proposed next watermark."""
        if self.ctx is None or self.http is None:
            raise RuntimeError("setup() must be called before list_changed()")
        return await read.list_changed(
            self.http,
            entity_type,
            since,
            endpoint=self.name,
            tenant_id=self._tenant_id(),
            space_id=self.ctx.config.space_id,
        )

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        """The current state of ``ref`` as a neutral entity with field envelopes."""
        if self.http is None:
            raise RuntimeError("setup() must be called before read()")
        return await read.read(self.http, ref, endpoint=self.name)

    def _tenant_id(self) -> str:
        """The tenant every ``IdentityRef`` is scoped by.

        Qlik ids are unique only within a tenant, so this is never allowed to be empty.
        The engine's configured ``tenant`` wins when it is set; otherwise the tenant
        hostname out of the configured base URL is used, which is what actually
        identifies a Qlik Cloud tenant (``https://<tenant>.<region>.qlikcloud.com``).
        """
        assert self.ctx is not None  # callers narrow before reaching here
        if self.ctx.tenant:
            return self.ctx.tenant
        host = urlsplit(self.ctx.config.base_url).hostname
        if not host:
            raise ValueError(
                f"cannot derive a tenant id: base_url {self.ctx.config.base_url!r} has no host"
            )
        return host

    # -- write path -------------------------------------------------------------------

    async def create(self, entity: NeutralEntity) -> WriteResult:
        """Create ``entity`` as a Qlik data product in the configured space.

        Never creates a Qlik dataset or user to make a reference resolve (D2, D3) and
        never activates the new product (D7). Members and owners that cannot be placed
        are omitted from the payload and reported on the result.
        """
        if self.writer is None:
            raise RuntimeError("setup() must be called before create()")
        return await self.writer.create(entity)

    async def delete(self, ref: IdentityRef) -> None:
        """Delete the Qlik data product — refused unless explicitly enabled.

        Implemented for contract completeness only. D4: v1 never deletes in Qlik, and
        the engine has no code path that calls this. It refuses with ``CapabilityError``
        unless this instance was built with ``DestructiveAction.DELETE`` enabled, which
        nothing in v1 does.
        """
        if self.lifecycle is None:
            raise RuntimeError("setup() must be called before delete()")
        await self.lifecycle.delete(ref)

    # -- remaining write path (T3.5) ----------------------------------------------------
    # create/update/delete are intentionally left at the ABC's defaults (they raise
    # CapabilityError) rather than overridden here: they are not abstract, so nothing
    # blocks instantiation, and the honest-refusal default is exactly right until the
    # write tasks land.
