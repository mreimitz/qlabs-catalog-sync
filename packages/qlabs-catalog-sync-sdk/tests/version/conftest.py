"""Real ``Connector`` subclasses for the compatibility-gate tests.

Everything here is a genuine, instantiable implementation of the contract, not a mock —
the same shape as ``tests/contract/conftest.py``'s stubs. The gate itself only ever
inspects the *class* (it runs at discovery, before any connector is instantiated), so
these are exercised as classes in the gate tests; being fully real and instantiable
just keeps them honest stand-ins for what ``ep.load()`` actually hands the engine.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings

from qlabs_catalog_sync_sdk.contract import (
    CapabilityManifestBase,
    Connector,
    ConnectorContext,
    EntityType,
    HealthStatus,
    IdentityRef,
    ListChangedResult,
    Watermark,
)
from qlabs_catalog_sync_sdk.models import DataProduct, NeutralEntity, TextField


class _StubManifest(CapabilityManifestBase):
    """The smallest concrete manifest the contract's anchor base allows."""

    def supports(self, entity_type: EntityType) -> bool:
        return entity_type is EntityType.DATA_PRODUCT

    def is_writable(self, entity_type: EntityType, field: str) -> bool:
        return False

    def requires_full_replace(self, entity_type: EntityType, field: str) -> bool:
        return False


class _StubConfig(BaseSettings):
    pass


class RealConnector(Connector):
    """A real, fully implemented read-only connector built against this SDK.

    Stamped with :data:`~qlabs_catalog_sync_sdk.contract.SDK_CONTRACT_VERSION` the same
    way any freshly built connector is: by simply inheriting the base class's class
    attribute, without overriding it.
    """

    name = "real_stub"
    ConfigModel = _StubConfig

    def capabilities(self) -> _StubManifest:
        return _StubManifest()

    async def setup(self, ctx: ConnectorContext[_StubConfig]) -> None:
        return None

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus.healthy(self.name)

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        return ListChangedResult.empty(Watermark.initial(self.name, entity_type))

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        return DataProduct(identities=[ref], name="Stub", description=TextField.plain("stub"))


class StaleConnector(RealConnector):
    """A connector built against a contract major the current SDK has moved past."""

    name = "stale_stub"
    sdk_contract_version = RealConnector.sdk_contract_version + 1


class UnstampedConnector(RealConnector):
    """A connector whose ``sdk_contract_version`` was overridden with a non-``int``.

    Not reachable by an ordinary connector — the base class always stamps an ``int`` —
    but a foreign, un-type-checked connector package could still ship this, so the gate
    must refuse it rather than compare it with ``!=`` and raise a confusing ``TypeError``
    from deep inside its own message formatting.
    """

    name = "unstamped_stub"
    sdk_contract_version = None  # type: ignore[assignment]


class NotAConnector:
    """A plain class that does not subclass ``Connector`` at all.

    Stands in for an entry point that points at the wrong object — the case the gate's
    first check exists for.
    """

    name = "not_a_connector"
