"""Shared manifest fixtures for T1.3's tests.

Two realistic manifests anchor every test in this directory: a Qlik-shaped *write*
manifest (the sole v1 write target) and a Databricks-shaped *read-only* manifest (a v1
source connector). Both are built from the real :class:`CapabilityManifest`, not a
hand-rolled stub — the point of this suite is that the concrete type behaves correctly,
not that some substitute does. Two real ``Connector`` subclasses are wired to them so
the contract's guards (``ensure_supported``, ``ensure_writable``) can be exercised
end to end, exactly as the engine would use them.
"""

from __future__ import annotations

import pytest
from pydantic_settings import BaseSettings

from qlabs_catalog_sync_sdk.contract import (
    Connector,
    ConnectorContext,
    FieldDiff,
    HealthStatus,
    IdentityRef,
    ListChangedResult,
    Watermark,
    WriteResult,
)
from qlabs_catalog_sync_sdk.manifest import (
    CapabilityManifest,
    ConcurrencyMode,
    EntityCapability,
    FieldCapability,
)
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType

QLIK_ENDPOINT = "qlik"
DATABRICKS_ENDPOINT = "databricks"
TENANT = "tenant-a"

#: The exact eight paths Qlik's data-product PATCH accepts (RS-02
#: qlik-two-way-sync-readiness.md section 2).
QLIK_DATA_PRODUCT_PATHS = [
    "/name",
    "/description",
    "/datasetIds",
    "/glossaryIds",
    "/readMe",
    "/keyContacts",
    "/tags",
    "/apiConsumableDatasetIds",
]


def build_qlik_manifest() -> CapabilityManifest:
    """A Qlik-shaped write manifest: data products fully described, glossary out per D5."""
    return CapabilityManifest(
        entities={
            EntityType.DATA_PRODUCT: EntityCapability(
                supported=True,
                identity_keys=["id", "qri"],
                fields={
                    "name": FieldCapability.rw(writable_via="rest-patch"),
                    "description": FieldCapability.rw(writable_via="rest-patch"),
                    "documentation": FieldCapability.rw(writable_via="rest-patch"),
                    "status": FieldCapability.rw(writable_via="lifecycle-action"),
                    "owners": FieldCapability.rw(writable_via="rest-patch", partial_update=False),
                    "tags": FieldCapability.rw(writable_via="rest-patch", partial_update=False),
                    "dataset_refs": FieldCapability.rw(
                        writable_via="rest-patch", partial_update=False
                    ),
                    # Glossary is out of the MVP (decision D5): the FK field is `na`,
                    # not merely unset, so the omission reads as a deliberate choice.
                    "glossary_term_refs": FieldCapability.na(),
                    # spaceId changes go through the `move` lifecycle action, not the
                    # field-level PATCH path enum, so it is read-only for the diff writer.
                    "placement": FieldCapability.ro(),
                },
                allowed_update_paths=list(QLIK_DATA_PRODUCT_PATHS),
                max_update_operations=8,
            ),
            EntityType.DATASET: EntityCapability(
                supported=True,
                identity_keys=["secure_qri"],
                fields={
                    "name": FieldCapability.ro(),
                    "description": FieldCapability.ro(),
                    "physical_ref": FieldCapability.ro(),
                },
            ),
            # Decision D5: glossary is out of the MVP. Declared, not omitted, so a
            # reader can tell this was a choice rather than an oversight.
            EntityType.GLOSSARY_TERM: EntityCapability(supported=False),
        },
        concurrency=ConcurrencyMode.ETAG,
    )


def build_databricks_manifest(*, has_sql_warehouse: bool) -> CapabilityManifest:
    """A Databricks-shaped read-only manifest.

    Decision D6: ``tags`` needs a SQL warehouse configured even to *read* (it goes
    through ``INFORMATION_SCHEMA`` over the Statement Execution API) — ``ro`` when one is
    configured, ``na`` otherwise. Every field is `ro`/`na`, never `rw`: source connectors
    are read-only under the v1 guardrails.
    """
    tags = FieldCapability.ro() if has_sql_warehouse else FieldCapability.na()
    return CapabilityManifest(
        entities={
            EntityType.DATA_PRODUCT: EntityCapability(
                supported=True,
                identity_keys=["full_name"],
                fields={
                    "name": FieldCapability.ro(),
                    "description": FieldCapability.ro(),
                    "tags": tags,
                    "owners": FieldCapability.ro(),
                },
            ),
            EntityType.DATASET: EntityCapability(
                supported=True,
                identity_keys=["full_name", "object_id"],
                fields={
                    "name": FieldCapability.ro(),
                    "description": FieldCapability.ro(),
                    "tags": tags,
                    "physical_ref": FieldCapability.ro(),
                },
            ),
            # No native glossary at all: never mentioned, not even as `na`.
        },
        concurrency=ConcurrencyMode.NONE,
    )


class _StubConfig(BaseSettings):
    """An empty connector config — these fixtures need no real settings."""


class FakeQlikConnector(Connector):
    """A real ``Connector`` subclass wired to the Qlik-shaped manifest, write path included.

    Proves the manifest works through the actual contract guards
    (``ensure_supported``/``ensure_writable``), not just in isolation.
    """

    name = QLIK_ENDPOINT
    ConfigModel = _StubConfig

    def capabilities(self) -> CapabilityManifest:
        return build_qlik_manifest()

    async def setup(self, ctx: ConnectorContext[_StubConfig]) -> None:
        return None

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus.healthy(self.name)

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        return ListChangedResult.empty(since)

    async def read(self, ref: IdentityRef) -> DataProduct:
        return DataProduct(identities=[ref], name="stub")

    async def update(self, ref: IdentityRef, diff: FieldDiff) -> WriteResult:
        self.ensure_writable(diff)
        return WriteResult.updated(ref, written_fields=diff.field_names)


class FakeDatabricksConnector(Connector):
    """A real, read-only ``Connector`` subclass wired to the Databricks-shaped manifest."""

    name = DATABRICKS_ENDPOINT
    ConfigModel = _StubConfig

    def __init__(self, *, has_sql_warehouse: bool = True) -> None:
        super().__init__()
        self._has_sql_warehouse = has_sql_warehouse

    def capabilities(self) -> CapabilityManifest:
        return build_databricks_manifest(has_sql_warehouse=self._has_sql_warehouse)

    async def setup(self, ctx: ConnectorContext[_StubConfig]) -> None:
        return None

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus.healthy(self.name)

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        return ListChangedResult.empty(since)

    async def read(self, ref: IdentityRef) -> DataProduct:
        return DataProduct(identities=[ref], name="stub")


@pytest.fixture
def qlik_manifest() -> CapabilityManifest:
    return build_qlik_manifest()


@pytest.fixture
def databricks_manifest() -> CapabilityManifest:
    return build_databricks_manifest(has_sql_warehouse=True)


@pytest.fixture
def databricks_manifest_no_warehouse() -> CapabilityManifest:
    return build_databricks_manifest(has_sql_warehouse=False)


@pytest.fixture
def qlik_connector() -> FakeQlikConnector:
    return FakeQlikConnector()


@pytest.fixture
def databricks_connector() -> FakeDatabricksConnector:
    return FakeDatabricksConnector()


@pytest.fixture
def qlik_data_product_ref() -> IdentityRef:
    return IdentityRef(
        endpoint=QLIK_ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key="dp-1",
        tenant_id=TENANT,
    )
