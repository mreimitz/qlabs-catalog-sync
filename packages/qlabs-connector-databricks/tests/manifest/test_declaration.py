"""The Databricks capability manifest: supported entities, identity, read-only-ness.

Proves behavior, not the source: every assertion exercises the built
``CapabilityManifest`` through its public query surface (``supports``, ``is_writable``,
``entity_capability``, ``field_capability``), the same surface the engine and
``Connector.ensure_*`` guards use — see ``test_connector_integration.py`` for the
end-to-end version of that.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.manifest import CapabilityManifest, ConcurrencyMode
from qlabs_catalog_sync_sdk.models import DataProduct, Dataset, EntityType, NeutralEntity

# Every field this manifest declares for the two supported entities, gathered once so
# "no field anywhere is writable" can be checked exhaustively rather than by sampling.
_DATA_PRODUCT_FIELDS = [
    "name",
    "description",
    "documentation",
    "status",
    "owners",
    "tags",
    "dataset_refs",
    "glossary_term_refs",
    "placement",
]
_DATASET_FIELDS = [
    "name",
    "description",
    "owners",
    "tags",
    "classifications",
    "glossary_term_refs",
    "physical_ref",
    "asset_type",
]


def test_declared_fields_cover_every_neutral_dataproduct_and_dataset_field() -> None:
    """The field lists above (used to drive the exhaustive checks below) must stay in
    sync with the neutral model — this fails loudly if either model grows a field this
    manifest forgot to make a deliberate ro/na choice about."""
    base_fields = set(NeutralEntity.model_fields)
    assert set(_DATA_PRODUCT_FIELDS) == set(DataProduct.model_fields) - base_fields
    assert set(_DATASET_FIELDS) == set(Dataset.model_fields) - base_fields


def test_supports_true_for_data_product_and_dataset(
    manifest_with_warehouse: CapabilityManifest,
) -> None:
    assert manifest_with_warehouse.supports(EntityType.DATA_PRODUCT)
    assert manifest_with_warehouse.supports(EntityType.DATASET)


def test_supports_false_for_glossary_term_and_category(
    manifest_with_warehouse: CapabilityManifest,
) -> None:
    assert not manifest_with_warehouse.supports(EntityType.GLOSSARY_TERM)
    assert not manifest_with_warehouse.supports(EntityType.CATEGORY)


def test_glossary_is_declared_unsupported_not_omitted(
    manifest_with_warehouse: CapabilityManifest,
) -> None:
    """D5-adjacent: Databricks has no native glossary at all, so this must be a stated
    `supported=False`, not silence that merely happens to also answer `supports()` False."""
    glossary = manifest_with_warehouse.entity_capability(EntityType.GLOSSARY_TERM)
    category = manifest_with_warehouse.entity_capability(EntityType.CATEGORY)

    assert glossary is not None
    assert glossary.supported is False
    assert category is not None
    assert category.supported is False


def test_no_field_of_data_product_is_writable(manifest_with_warehouse: CapabilityManifest) -> None:
    for field in _DATA_PRODUCT_FIELDS:
        assert not manifest_with_warehouse.is_writable(EntityType.DATA_PRODUCT, field)


def test_no_field_of_dataset_is_writable(manifest_with_warehouse: CapabilityManifest) -> None:
    for field in _DATASET_FIELDS:
        assert not manifest_with_warehouse.is_writable(EntityType.DATASET, field)


def test_an_undeclared_field_is_not_writable_either(
    manifest_with_warehouse: CapabilityManifest,
) -> None:
    assert not manifest_with_warehouse.is_writable(EntityType.DATA_PRODUCT, "no_such_field")
    assert not manifest_with_warehouse.is_writable(EntityType.DATASET, "no_such_field")


def test_no_field_requires_full_replace_since_nothing_is_writable(
    manifest_with_warehouse: CapabilityManifest,
) -> None:
    """`requires_full_replace` is gated on writability; a read-only manifest must answer
    False everywhere even though every field's default `partial_update=True` would
    otherwise make this look unremarkable."""
    for field in _DATA_PRODUCT_FIELDS:
        assert not manifest_with_warehouse.requires_full_replace(EntityType.DATA_PRODUCT, field)
    for field in _DATASET_FIELDS:
        assert not manifest_with_warehouse.requires_full_replace(EntityType.DATASET, field)


def test_data_product_identity_keys_are_full_name_and_object_id(
    manifest_with_warehouse: CapabilityManifest,
) -> None:
    capability = manifest_with_warehouse.entity_capability(EntityType.DATA_PRODUCT)
    assert capability is not None
    assert capability.identity_keys
    assert "full_name" in capability.identity_keys
    assert "object_id" in capability.identity_keys


def test_dataset_identity_keys_are_full_name_and_object_id(
    manifest_with_warehouse: CapabilityManifest,
) -> None:
    capability = manifest_with_warehouse.entity_capability(EntityType.DATASET)
    assert capability is not None
    assert capability.identity_keys
    assert "full_name" in capability.identity_keys
    assert "object_id" in capability.identity_keys


def test_concurrency_is_none(manifest_with_warehouse: CapabilityManifest) -> None:
    assert manifest_with_warehouse.concurrency is ConcurrencyMode.NONE


def test_supports_events_is_false_for_both_entities(
    manifest_with_warehouse: CapabilityManifest,
) -> None:
    data_product = manifest_with_warehouse.entity_capability(EntityType.DATA_PRODUCT)
    dataset = manifest_with_warehouse.entity_capability(EntityType.DATASET)
    assert data_product is not None
    assert dataset is not None
    assert data_product.supports_events is False
    assert dataset.supports_events is False


def test_manifest_round_trips_through_json(manifest_with_warehouse: CapabilityManifest) -> None:
    restored = CapabilityManifest.model_validate(manifest_with_warehouse.model_dump(mode="json"))

    assert restored == manifest_with_warehouse


def test_manifest_without_warehouse_also_round_trips(
    manifest_without_warehouse: CapabilityManifest,
) -> None:
    restored = CapabilityManifest.model_validate(manifest_without_warehouse.model_dump(mode="json"))

    assert restored == manifest_without_warehouse
