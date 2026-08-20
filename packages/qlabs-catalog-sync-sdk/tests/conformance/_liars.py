"""Three connectors that lie about what they can do.

Not fixtures used to certify anything — the opposite. The second half of T1.8's
definition of done is that the conformance kit *fails* a dishonest connector, so each of
these deliberately breaks one promise a capability manifest makes, and the tests in
``test_dishonest_manifests.py`` assert the suite (or the HTTP harness) catches it:

* :class:`ClaimsWritableButDropsTheWrite` — declares a field ``rw`` and honors the
  capability check on the way in, but its ``update`` never actually persists the change
  it claims to have made.
* :class:`ClaimsEntitySupportButRefusesToWrite` — declares an entity type
  ``supported=True`` with a writable field, but never overrides ``create``/``update`` —
  so despite what the manifest says, every write refuses.
* :class:`ClaimsEtagButNeverSendsIfMatch` — declares ``concurrency=etag`` but its
  ``update`` never forwards the revision it was given as ``If-Match``.

Each is a minimal, hand-built ``Connector`` — not a ``FakeConnector`` subclass — because
``FakeConnector`` is built to be honest by construction (its ``update`` always calls
``ensure_writable`` and always persists what it accepts); lying convincingly needs
purpose-built code.
"""

from __future__ import annotations

from typing import Any

from qlabs_catalog_sync_sdk.config import ConnectorContext
from qlabs_catalog_sync_sdk.contract import (
    Connector,
    HealthStatus,
    ListChangedResult,
    Watermark,
    WriteResult,
)
from qlabs_catalog_sync_sdk.envelope import build_field_envelopes
from qlabs_catalog_sync_sdk.exceptions import NotFound
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.manifest import (
    CapabilityManifest,
    ConcurrencyMode,
    EntityCapability,
    FieldCapability,
)
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType, FieldDiff, IdentityRef
from qlabs_catalog_sync_sdk.testing import FakeConnectorConfig


class ClaimsWritableButDropsTheWrite(Connector):
    """Manifest: ``DATA_PRODUCT.name`` is ``rw``. Reality: ``update`` never writes it."""

    name = "lying-about-writes"
    ConfigModel = FakeConnectorConfig

    def __init__(self) -> None:
        super().__init__()
        self._entity: DataProduct | None = None
        self._ref: IdentityRef | None = None

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            entities={
                EntityType.DATA_PRODUCT: EntityCapability(
                    supported=True, identity_keys=["id"], fields={"name": FieldCapability.rw()}
                )
            },
            concurrency=ConcurrencyMode.NONE,
        )

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        return None

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus.healthy(self.name)

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        return ListChangedResult.empty(Watermark.initial(self.name, entity_type))

    async def read(self, ref: IdentityRef) -> DataProduct:
        if self._entity is None or self._ref is None or ref.native_key != self._ref.native_key:
            raise NotFound(f"no object {ref.native_key!r}", endpoint=self.name)
        return self._entity

    async def create(self, entity: Any) -> Any:
        assert isinstance(entity, DataProduct)
        self._ref = IdentityRef(
            endpoint=self.name,
            entity_type=EntityType.DATA_PRODUCT,
            native_key="lying-1",
            tenant_id="lying-tenant",
        )
        self._entity = entity.model_copy(
            update={
                "identities": [self._ref],
                "field_envelopes": build_field_envelopes(
                    {"name": entity.name}, source_endpoint=self.name, source_revision="rev-1"
                ),
            }
        )
        return WriteResult.created(self._ref, source_revision="rev-1", written_fields=["name"])

    async def update(self, ref: IdentityRef, diff: FieldDiff) -> Any:
        self.ensure_writable(diff)  # honest about the check...
        # ...but the lie: claims success without ever mutating self._entity.
        return WriteResult.updated(ref, source_revision="rev-1", written_fields=diff.field_names)


class ClaimsEntitySupportButRefusesToWrite(Connector):
    """Manifest: ``CATEGORY`` is ``supported=True`` with a writable field. Reality:
    ``create``/``update``/``delete`` are never overridden, so every write refuses.
    """

    name = "lying-about-support"
    ConfigModel = FakeConnectorConfig

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            entities={
                EntityType.CATEGORY: EntityCapability(
                    supported=True, identity_keys=["id"], fields={"name": FieldCapability.rw()}
                )
            },
            concurrency=ConcurrencyMode.NONE,
        )

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        return None

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus.healthy(self.name)

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        return ListChangedResult.empty(Watermark.initial(self.name, entity_type))

    async def read(self, ref: IdentityRef) -> Any:
        raise NotFound(f"no object {ref.native_key!r}", endpoint=self.name)

    # create/update/delete: deliberately NOT overridden. The base Connector default
    # always raises CapabilityError, regardless of what capabilities() just promised.


class ClaimsEtagButNeverSendsIfMatch(Connector):
    """Manifest: ``concurrency=etag``. Reality: ``update`` never forwards the revision
    it was given as ``If-Match`` — it PATCHes as if concurrency were ``none``.
    """

    name = "lying-about-etag"
    ConfigModel = FakeConnectorConfig

    def __init__(self, http: HttpEndpoint) -> None:
        super().__init__()
        self._http = http

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            entities={
                EntityType.DATA_PRODUCT: EntityCapability(
                    supported=True, identity_keys=["id"], fields={"name": FieldCapability.rw()}
                )
            },
            concurrency=ConcurrencyMode.ETAG,
        )

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        return None

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus.healthy(self.name)

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        return ListChangedResult.empty(Watermark.initial(self.name, entity_type))

    async def read(self, ref: IdentityRef) -> Any:
        raise NotFound(f"no object {ref.native_key!r}", endpoint=self.name)

    async def update(self, ref: IdentityRef, diff: FieldDiff) -> Any:
        self.ensure_writable(diff)
        change = diff.change_for("name")
        assert change is not None
        # THE LIE: `diff.expected_revision` is right here, but it is never sent as
        # If-Match — exactly the gap between a manifest's promise and the wire.
        await self._http.patch(f"/items/{ref.native_key}", json={"name": change.value})
        return WriteResult.updated(ref, source_revision="rev-after", written_fields=["name"])
