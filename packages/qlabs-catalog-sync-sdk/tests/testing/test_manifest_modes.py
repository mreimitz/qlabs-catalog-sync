"""Configurable manifest: the same class plays a read-only source or the write target,
purely from what its manifest declares.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.contract import CapabilityManifestBase
from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.manifest import CapabilityManifest, EntityCapability, FieldCapability
from qlabs_catalog_sync_sdk.models import (
    Category,
    DataProduct,
    Dataset,
    EntityType,
    FieldChange,
    FieldDiff,
)
from qlabs_catalog_sync_sdk.testing import (
    FakeConnector,
    databricks_shaped_manifest,
    qlik_shaped_manifest,
)


def test_read_only_source_defaults_to_the_databricks_shape(source: FakeConnector) -> None:
    manifest = source.capabilities()
    assert isinstance(manifest, CapabilityManifestBase)
    assert manifest.supports(EntityType.DATA_PRODUCT)
    assert not manifest.is_writable(EntityType.DATA_PRODUCT, "name")


def test_write_target_defaults_to_the_qlik_shape(target: FakeConnector) -> None:
    manifest = target.capabilities()
    assert manifest.supports(EntityType.DATA_PRODUCT)
    assert manifest.is_writable(EntityType.DATA_PRODUCT, "name")
    assert not manifest.is_writable(EntityType.DATA_PRODUCT, "glossary_term_refs")  # D5


async def test_read_only_manifest_refuses_create_with_capability_error(
    source: FakeConnector,
) -> None:
    with pytest.raises(CapabilityError):
        await source.create(DataProduct(name="Retail Sales"))
    assert source.call_count("create") == 1  # attempted, not silently skipped


async def test_read_only_manifest_refuses_update_with_capability_error(
    source: FakeConnector,
) -> None:
    ref = source.seed(DataProduct(name="Retail Sales"))
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT, changes=[FieldChange(field="name", value="x")]
    )
    with pytest.raises(CapabilityError):
        await source.update(ref, diff)


async def test_read_only_manifest_refuses_delete_with_capability_error(
    source: FakeConnector,
) -> None:
    ref = source.seed(DataProduct(name="Retail Sales"))
    with pytest.raises(CapabilityError):
        await source.delete(ref)


async def test_write_manifest_accepts_create(target: FakeConnector) -> None:
    result = await target.create(DataProduct(name="Retail Sales"))
    assert result.ref.endpoint == target.name


async def test_write_manifest_accepts_update_of_a_writable_field(target: FakeConnector) -> None:
    created = await target.create(DataProduct(name="Retail Sales"))
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="name", value="Retail Sales v2")],
    )
    result = await target.update(created.ref, diff)
    assert result.written_fields == ["name"]


async def test_write_manifest_still_refuses_a_read_only_field(target: FakeConnector) -> None:
    """``placement`` is `ro` even on the Qlik-shaped write manifest (moves go through a
    lifecycle action, not the field-level PATCH path)."""
    created = await target.create(DataProduct(name="Retail Sales"))
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="placement", value="spaces/other")],
    )
    with pytest.raises(CapabilityError):
        await target.update(created.ref, diff)


async def test_write_manifest_refuses_creating_a_dataset(target: FakeConnector) -> None:
    """Decision D2: the Qlik connector never creates Qlik datasets, only data products —
    so even the write-shaped manifest declares `dataset` fields read-only."""
    with pytest.raises(CapabilityError):
        await target.create(Dataset(name="a_table"))


def test_databricks_shaped_manifest_gates_tags_on_sql_warehouse() -> None:
    with_warehouse = databricks_shaped_manifest(has_sql_warehouse=True)
    without_warehouse = databricks_shaped_manifest(has_sql_warehouse=False)
    assert with_warehouse.is_writable(EntityType.DATA_PRODUCT, "tags") is False
    with_capability = with_warehouse.entity_capability(EntityType.DATA_PRODUCT)
    without_capability = without_warehouse.entity_capability(EntityType.DATA_PRODUCT)
    assert with_capability is not None and without_capability is not None
    assert with_capability.fields["tags"].mode.value == "ro"
    assert without_capability.fields["tags"].mode.value == "na"


def test_qlik_shaped_manifest_is_the_real_concrete_manifest_type() -> None:
    assert isinstance(qlik_shaped_manifest(), CapabilityManifest)


def test_an_arbitrary_manifest_is_still_accepted() -> None:
    """The base constructor keeps arbitrary manifests possible — the two canned shapes
    are convenience, not the only option."""
    manifest = CapabilityManifest(
        entities={
            EntityType.CATEGORY: EntityCapability(
                supported=True,
                identity_keys=["id"],
                fields={"name": FieldCapability.rw(writable_via="rest-patch")},
            )
        }
    )
    connector = FakeConnector(manifest=manifest)
    assert connector.capabilities() is manifest


async def test_arbitrary_manifest_governs_writes() -> None:
    manifest = CapabilityManifest(
        entities={
            EntityType.CATEGORY: EntityCapability(
                supported=True,
                identity_keys=["id"],
                fields={"name": FieldCapability.rw(writable_via="rest-patch")},
            )
        }
    )
    connector = FakeConnector(manifest=manifest)
    result = await connector.create(Category(name="Finance"))
    assert result.written_fields
