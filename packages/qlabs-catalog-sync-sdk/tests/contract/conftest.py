"""Shared stubs for the connector-contract tests.

Everything here is a real implementation of the contract, not a mock: the manifest stub
implements ``CapabilityManifestBase``, the connector stubs subclass ``Connector`` and are
actually instantiated and awaited, and ``setup`` receives a real
``ConnectorContext``. The point of these tests is that the contract *works*, so nothing is
patched out.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import Field
from pydantic_settings import BaseSettings

from qlabs_catalog_sync_sdk.contract import (
    CapabilityManifestBase,
    ChangeKind,
    ChangeRef,
    Connector,
    ConnectorContext,
    EntityType,
    FieldDiff,
    HealthStatus,
    IdentityRef,
    ListChangedResult,
    Watermark,
    WriteResult,
)
from qlabs_catalog_sync_sdk.models import DataProduct, NeutralEntity, TextField

SOURCE = "stub_source"
TARGET = "stub_target"
TENANT = "tenant-a"

READ_AT = datetime(2026, 8, 20, 9, 30, 0, tzinfo=UTC)
MODIFIED_AT = datetime(2026, 8, 19, 17, 5, 30, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# A concrete capability manifest — exactly the surface T1.3 must provide
# --------------------------------------------------------------------------------------


class StubManifest(CapabilityManifestBase):
    """Minimal concrete manifest built on the contract's anchor base."""

    supported: set[EntityType] = Field(default_factory=set)
    writable: dict[EntityType, set[str]] = Field(default_factory=dict)
    full_replace: dict[EntityType, set[str]] = Field(default_factory=dict)

    def supports(self, entity_type: EntityType) -> bool:
        return entity_type in self.supported

    def is_writable(self, entity_type: EntityType, field: str) -> bool:
        return field in self.writable.get(entity_type, set())

    def requires_full_replace(self, entity_type: EntityType, field: str) -> bool:
        return field in self.full_replace.get(entity_type, set())


# --------------------------------------------------------------------------------------
# Config + context stubs
# --------------------------------------------------------------------------------------


class StubConfig(BaseSettings):
    base_url: str = "https://example.invalid"


# --------------------------------------------------------------------------------------
# Connector stubs
# --------------------------------------------------------------------------------------


class ReadOnlySourceConnector(Connector):
    """A read-only source connector: it implements no write path at all.

    This is the Databricks/Collibra/Snowflake shape under the v1 guardrails — it inherits
    the contract's refusing write defaults instead of writing stubs.
    """

    name = SOURCE
    ConfigModel = StubConfig

    def __init__(self) -> None:
        super().__init__()
        self.api_calls: list[str] = []
        self.context: ConnectorContext[StubConfig] | None = None
        self.closed = False

    def capabilities(self) -> StubManifest:
        return StubManifest(supported={EntityType.DATA_PRODUCT, EntityType.DATASET})

    async def setup(self, ctx: ConnectorContext[StubConfig]) -> None:
        self.context = ctx

    async def healthcheck(self) -> HealthStatus:
        self.api_calls.append("healthcheck")
        return HealthStatus.healthy(self.name, checked_at=READ_AT)

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        self.api_calls.append("list_changed")
        if since.is_initial:
            return ListChangedResult(
                changes=[
                    ChangeRef(
                        ref=IdentityRef(
                            endpoint=self.name,
                            entity_type=entity_type,
                            native_key="main.retail",
                            tenant_id=TENANT,
                        ),
                        kind=ChangeKind.UPSERT,
                        source_revision="rev-7",
                        last_modified_at=MODIFIED_AT,
                        display_name="retail",
                    )
                ],
                next_watermark=Watermark.at(self.name, entity_type, MODIFIED_AT),
                has_more=True,
            )
        return ListChangedResult.empty(Watermark.at(self.name, entity_type, READ_AT))

    async def read(self, ref: IdentityRef) -> DataProduct:
        self.api_calls.append("read")
        return DataProduct(
            identities=[ref],
            name="Retail Sales",
            description=TextField.plain("Curated retail sales data."),
        )

    async def close(self) -> None:
        self.closed = True


class WriteTargetConnector(Connector):
    """A write connector: overrides the write paths and guards them with the manifest."""

    name = TARGET
    ConfigModel = StubConfig

    def __init__(self) -> None:
        super().__init__()
        self.api_calls: list[str] = []

    def capabilities(self) -> StubManifest:
        return StubManifest(
            supported={EntityType.DATA_PRODUCT},
            writable={EntityType.DATA_PRODUCT: {"description", "tags"}},
            full_replace={EntityType.DATA_PRODUCT: {"tags"}},
        )

    async def setup(self, ctx: ConnectorContext[StubConfig]) -> None:
        return None

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus.healthy(self.name)

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        return ListChangedResult.empty(Watermark.initial(self.name, entity_type))

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        self.api_calls.append("read")
        return DataProduct(identities=[ref], name="Retail Sales")

    async def create(self, entity: NeutralEntity) -> WriteResult:
        self.ensure_supported(type(entity).ENTITY_TYPE, operation="create")
        self.api_calls.append("create")
        ref = IdentityRef(
            endpoint=self.name,
            entity_type=type(entity).ENTITY_TYPE,
            native_key="dp-9",
            tenant_id=TENANT,
        )
        return WriteResult.created(ref, source_revision="etag-1", written_fields=["name"])

    async def update(self, ref: IdentityRef, diff: FieldDiff) -> WriteResult:
        self.ensure_writable(diff)
        if not diff.changes:
            return WriteResult.no_op(ref, source_revision="etag-1", detail="nothing to write")
        self.api_calls.append("update")
        return WriteResult.updated(ref, source_revision="etag-2", written_fields=diff.field_names)

    async def delete(self, ref: IdentityRef) -> None:
        self.api_calls.append("delete")


@pytest.fixture
def source_connector() -> ReadOnlySourceConnector:
    return ReadOnlySourceConnector()


@pytest.fixture
def target_connector() -> WriteTargetConnector:
    return WriteTargetConnector()


@pytest.fixture
def context() -> ConnectorContext[StubConfig]:
    return ConnectorContext.build(config=StubConfig(), endpoint=SOURCE, tenant=TENANT)


@pytest.fixture
def source_ref() -> IdentityRef:
    return IdentityRef(
        endpoint=SOURCE,
        entity_type=EntityType.DATA_PRODUCT,
        native_key="main.retail",
        tenant_id=TENANT,
    )


@pytest.fixture
def target_ref() -> IdentityRef:
    return IdentityRef(
        endpoint=TARGET,
        entity_type=EntityType.DATA_PRODUCT,
        native_key="dp-9",
        tenant_id=TENANT,
    )
