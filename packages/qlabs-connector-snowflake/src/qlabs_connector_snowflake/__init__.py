"""QLabs Catalog Sync — Snowflake connector (WP6).

Read-only source connector: reads objects plus listings/shares into neutral entities.
It never writes. Depends only on the SDK plus ``snowflake-connector-python``.
Registered as ``snowflake`` under the ``qlabs_catalog_sync.connectors`` entry-point
group.

This module (T6.1) implements the connector's identity, config wiring, ``setup()`` and
``healthcheck()`` (key-pair JWT auth per RS-05 section 3.2, via ``auth.py``), plus wires
in the read-only capability manifest (T6.2, ``manifest.py``). ``list_changed()`` (T6.3)
and ``read()`` (T6.4) are later tasks; their methods are present (the ABC requires it to
instantiate) but raise ``NotImplementedError`` rather than any placeholder that could
pass for real behavior. The write path (``create``/``update``/``delete``) is
deliberately left untouched: the inherited ``Connector`` defaults already refuse with
``CapabilityError``, which is exactly what a read-only source connector wants.
"""

from __future__ import annotations

from typing import Any

import httpx

from qlabs_catalog_sync_sdk.auth import Clock as AuthClock
from qlabs_catalog_sync_sdk.auth import JWTAuthProvider
from qlabs_catalog_sync_sdk.config import ConnectorContext
from qlabs_catalog_sync_sdk.contract import (
    CapabilityManifestBase,
    HealthStatus,
    ListChangedResult,
    Watermark,
)
from qlabs_catalog_sync_sdk.contract import Connector as ConnectorABC
from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import EntityType, IdentityRef, NeutralEntity

from .auth import (
    KEYPAIR_JWT_TOKEN_TYPE,
    SnowflakeConfig,
    build_key_pair_jwt_provider,
    translate_snowflake_error,
)
from .manifest import build_manifest

__all__ = ["Connector"]

#: The healthcheck probe: a bounded metadata-only listing that needs no configured
#: warehouse (RS-05 section 3.7). ``showLimit`` is the resource REST API's own
#: pagination parameter name (RS-05 section 3.7), not the informal `limit` a casual
#: description of this endpoint might suggest.
_HEALTHCHECK_PATH = "/api/v2/databases"
_HEALTHCHECK_PARAMS = {"showLimit": 1}


class Connector(ConnectorABC):
    """The Snowflake read-only source connector.

    ``ConfigModel`` is :class:`~qlabs_connector_snowflake.auth.SnowflakeConfig``.
    ``setup()`` builds the key-pair JWT auth provider (RS-05 section 3.2) and an
    ``httpx``-based :class:`~qlabs_catalog_sync_sdk.http.HttpEndpoint` against the
    account base URL; ``healthcheck()`` makes one cheap authenticated REST call to prove
    the credentials work and the account is reachable.
    """

    name = "snowflake"
    ConfigModel = SnowflakeConfig

    def __init__(self, *, clock: AuthClock | None = None) -> None:
        """``clock`` is a test seam: production code never passes it, so the auth
        provider uses real wall-clock time. Tests inject a
        :class:`~qlabs_catalog_sync_sdk.config.ManualClock` to prove refresh-before-
        expiry caching deterministically, without sleeping.
        """
        super().__init__()
        self._clock = clock
        self._token_provider: JWTAuthProvider | None = None
        self._http: HttpEndpoint | None = None
        self._ctx: ConnectorContext[SnowflakeConfig] | None = None

    # -- declaration -----------------------------------------------------------------

    def capabilities(self) -> CapabilityManifestBase:
        """The read-only Snowflake manifest — see ``manifest.py``.

        Unlike the Databricks connector, this manifest does not vary with the resolved
        config (``manifest.build_manifest()`` takes no arguments and never imports
        ``auth.py``), so this is safe to call before :meth:`setup`.
        """
        return build_manifest()

    # -- lifecycle ---------------------------------------------------------------------

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        """Build the key-pair JWT auth provider and the account ``HttpEndpoint``.

        The provider is built (and therefore the private key parsed and its fingerprint
        computed) here, not lazily on first use — a bad key or passphrase surfaces as
        :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError` from :meth:`setup` itself,
        matching the ``Connector`` contract's "``setup()``... validate config against
        the endpoint" and the Databricks connector's precedent of failing fast on bad
        credentials during setup rather than on first read.
        """
        self._ctx = ctx
        config = ctx.config
        self._token_provider = build_key_pair_jwt_provider(
            config, endpoint=self.name, clock=self._clock
        )
        # RS-05 section 3.2: every request carries both `Authorization: Bearer <jwt>`
        # (the provider's own header) and `X-Snowflake-Authorization-Token-Type:
        # KEYPAIR_JWT` (static, wired here as a default header on every request this
        # HttpEndpoint sends).
        self._http = HttpEndpoint(
            config.resolved_base_url,
            auth=self._token_provider,
            headers={"X-Snowflake-Authorization-Token-Type": KEYPAIR_JWT_TOKEN_TYPE},
        )
        ctx.logger.info(
            "snowflake.setup.complete",
            base_url=config.resolved_base_url,
            account_identifier=config.account_identifier,
        )

    async def healthcheck(self) -> HealthStatus:
        """One cheap authenticated call: list databases, bounded to a single result.

        Chosen over a SQL REST API ``SELECT 1`` (RS-05 section 3.8) because listing
        databases needs no configured warehouse (RS-05 section 3.7's resource REST
        APIs are metadata-only), so this probe is meaningful for every valid config,
        not only ones with a warehouse set.
        """
        if self._ctx is None or self._http is None:
            raise RuntimeError("setup() must be called before healthcheck()")
        endpoint = self._ctx.endpoint

        try:
            await self._http.get(_HEALTHCHECK_PATH, params=_HEALTHCHECK_PARAMS)
        except httpx.HTTPStatusError as exc:
            mapped = translate_snowflake_error(exc, endpoint=endpoint)
            if isinstance(mapped, AuthError):
                return HealthStatus.unhealthy(endpoint, reason=str(mapped))
            return HealthStatus.degraded(endpoint, reason=str(mapped))
        except httpx.TransportError as exc:
            mapped = translate_snowflake_error(exc, endpoint=endpoint)
            return HealthStatus.degraded(endpoint, reason=str(mapped))

        return HealthStatus.healthy(
            endpoint, details={"probe": f"GET {_HEALTHCHECK_PATH}?showLimit=1"}
        )

    async def close(self) -> None:
        """Release the HTTP endpoint's connection pool, if built."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- read path (T6.3 / T6.4) --------------------------------------------------------

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        """Not implemented yet.

        TODO(T6.3): list candidates changed since ``since`` via SHOW /
        INFORMATION_SCHEMA / ACCOUNT_USAGE (note the ~2h ACCOUNT_USAGE lag, RS-05
        section 1.4), plus the proposed next watermark.
        """
        raise NotImplementedError("Connector.list_changed is implemented in T6.3")

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        """Not implemented yet.

        TODO(T6.4): read one object (schema/table/view) or one listing/share into a
        neutral entity with field envelopes.
        """
        raise NotImplementedError("Connector.read is implemented in T6.4")

    # -- write path: intentionally NOT overridden ---------------------------------------
    # create()/update()/delete() inherit the ConnectorABC defaults, which refuse with
    # CapabilityError. Snowflake is a read-only source connector (v1 scope guardrail);
    # overriding any of these would violate it.
