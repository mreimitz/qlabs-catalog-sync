"""QLabs Catalog Sync — Qlik connector (WP3).

The sole WRITE connector in v1: metadata flows from source catalogs into Qlik, and Qlik
is the only write target. Depends only on the SDK plus httpx. Registered via the
``qlabs_catalog_sync.connectors`` entry-point group as ``qlik`` (see ``pyproject.toml``).

T3.1 (this task) builds auth, config and connector setup: :class:`QlikConfig`
(``config.py``), the OAuth2 machine-to-machine wiring (``auth.py``), and here,
:class:`Connector` — its ``setup()`` (builds the authenticated ``HttpEndpoint``) and
``healthcheck()`` (proves the credentials work and the configured target space is
reachable). ``capabilities()`` (T3.2), ``list_changed``/``read`` (T3.3) and the write
paths (T3.4/T3.5/T3.6/T3.7) are **not** implemented here — see the placeholder note on
each method below.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

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
)
from qlabs_catalog_sync_sdk.contract import Connector as ConnectorABC
from qlabs_catalog_sync_sdk.exceptions import AuthError, ConnectorError, TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint

from .auth import build_http_endpoint, classify_response_error, classify_transport_error
from .config import QlikConfig

__all__ = ["Connector"]


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
        """Not implemented here. T3.2 builds the Qlik capability manifest
        (``manifest.py``) — data products and datasets/items as ``rw`` with ETag
        concurrency, product arrays as ``partial_update=false``. Deliberately not a
        fake manifest: a placeholder that claimed capabilities would be picked up by the
        engine's planning and the conformance kit's capability-honesty check as if it
        were real."""
        raise NotImplementedError(
            "T3.2 implements the Qlik capability manifest; see manifest.py"
        )

    # -- read path (T3.3) -------------------------------------------------------------

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        """Not implemented here. T3.3 implements the Qlik read path (``read.py``)."""
        raise NotImplementedError("T3.3 implements the Qlik read path; see read.py")

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        """Not implemented here. T3.3 implements the Qlik read path (``read.py``)."""
        raise NotImplementedError("T3.3 implements the Qlik read path; see read.py")

    # -- write path (T3.4/T3.5/T3.6/T3.7) ---------------------------------------------
    # create/update/delete are intentionally left at the ABC's defaults (they raise
    # CapabilityError) rather than overridden here: they are not abstract, so nothing
    # blocks instantiation, and the honest-refusal default is exactly right until the
    # write tasks land.
