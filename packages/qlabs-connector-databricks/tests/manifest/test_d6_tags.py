"""Decision D6: `tags` (and Databricks' tag-derived `classifications`) are `ro` only
when a SQL warehouse is configured, and `na` otherwise.

Builds both manifest shapes explicitly and asserts the difference, rather than trusting
either shape in isolation — an honest `na` beats a silently empty read, and this is the
one field in the whole manifest whose mode is a runtime choice rather than a fixed fact.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.manifest import CapabilityManifest, FieldCapabilityMode
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_connector_databricks.config import DatabricksConfig
from qlabs_connector_databricks.manifest import build_manifest, manifest_for_config


def test_data_product_tags_are_ro_with_a_warehouse_and_na_without_one(
    manifest_with_warehouse: CapabilityManifest,
    manifest_without_warehouse: CapabilityManifest,
) -> None:
    with_warehouse = manifest_with_warehouse.field_capability(EntityType.DATA_PRODUCT, "tags")
    without_warehouse = manifest_without_warehouse.field_capability(EntityType.DATA_PRODUCT, "tags")

    assert with_warehouse is not None
    assert without_warehouse is not None
    assert with_warehouse.mode is FieldCapabilityMode.RO
    assert without_warehouse.mode is FieldCapabilityMode.NA


def test_dataset_tags_are_ro_with_a_warehouse_and_na_without_one(
    manifest_with_warehouse: CapabilityManifest,
    manifest_without_warehouse: CapabilityManifest,
) -> None:
    with_warehouse = manifest_with_warehouse.field_capability(EntityType.DATASET, "tags")
    without_warehouse = manifest_without_warehouse.field_capability(EntityType.DATASET, "tags")

    assert with_warehouse is not None
    assert without_warehouse is not None
    assert with_warehouse.mode is FieldCapabilityMode.RO
    assert without_warehouse.mode is FieldCapabilityMode.NA


def test_dataset_classifications_follow_tags_exactly_since_databricks_proxies_them(
    manifest_with_warehouse: CapabilityManifest,
    manifest_without_warehouse: CapabilityManifest,
) -> None:
    """RS-03 8.2: Databricks realizes `classifications` through the same tag read path
    as `tags`, so it must carry the identical D6 conditionality, not an unconditional
    `ro` that would silently claim a read this connector cannot actually perform without
    a warehouse."""
    with_warehouse = manifest_with_warehouse.field_capability(EntityType.DATASET, "classifications")
    without_warehouse = manifest_without_warehouse.field_capability(
        EntityType.DATASET, "classifications"
    )

    assert with_warehouse is not None
    assert without_warehouse is not None
    assert with_warehouse.mode is FieldCapabilityMode.RO
    assert without_warehouse.mode is FieldCapabilityMode.NA


def test_neither_tags_nor_classifications_is_ever_writable_regardless_of_warehouse(
    manifest_with_warehouse: CapabilityManifest,
    manifest_without_warehouse: CapabilityManifest,
) -> None:
    for manifest in (manifest_with_warehouse, manifest_without_warehouse):
        assert not manifest.is_writable(EntityType.DATA_PRODUCT, "tags")
        assert not manifest.is_writable(EntityType.DATASET, "tags")
        assert not manifest.is_writable(EntityType.DATASET, "classifications")


def test_manifest_for_config_reads_has_sql_warehouse_off_the_config(
    config_with_warehouse: DatabricksConfig, config_without_warehouse: DatabricksConfig
) -> None:
    with_warehouse = manifest_for_config(config_with_warehouse)
    without_warehouse = manifest_for_config(config_without_warehouse)

    tags_with = with_warehouse.field_capability(EntityType.DATA_PRODUCT, "tags")
    tags_without = without_warehouse.field_capability(EntityType.DATA_PRODUCT, "tags")
    assert tags_with is not None and tags_with.mode is FieldCapabilityMode.RO
    assert tags_without is not None and tags_without.mode is FieldCapabilityMode.NA


def test_build_manifest_is_pure_and_deterministic() -> None:
    first = build_manifest(has_sql_warehouse=True)
    second = build_manifest(has_sql_warehouse=True)

    assert first == second
