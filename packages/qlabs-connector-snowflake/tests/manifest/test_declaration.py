"""The Snowflake capability manifest: supported entities, read-only-ness, identity keys
(RS-05 section 4.3), and tags/comments support (the DoD's explicit requirements).

Proves behavior, not the source: every assertion exercises the built
``CapabilityManifest`` through its public query surface (``supports``, ``is_writable``,
``entity_capability``, ``field_capability``), the same surface the engine and
``Connector.ensure_*`` guards use — see ``test_connector_integration.py`` for the
end-to-end version of that.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.manifest import CapabilityManifest, ConcurrencyMode, FieldCapabilityMode
from qlabs_catalog_sync_sdk.models import DataProduct, Dataset, EntityType, NeutralEntity
from qlabs_connector_snowflake.manifest import build_manifest

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


def test_supports_true_for_data_product_and_dataset(manifest: CapabilityManifest) -> None:
    assert manifest.supports(EntityType.DATA_PRODUCT)
    assert manifest.supports(EntityType.DATASET)


def test_supports_false_for_glossary_term_and_category(manifest: CapabilityManifest) -> None:
    assert not manifest.supports(EntityType.GLOSSARY_TERM)
    assert not manifest.supports(EntityType.CATEGORY)


def test_unsupported_entities_are_declared_not_omitted(manifest: CapabilityManifest) -> None:
    """Declared `supported=False`, not silence that merely happens to also answer
    `supports()` False — lets a reader tell "considered and excluded" apart from
    "never thought about"."""
    glossary = manifest.entity_capability(EntityType.GLOSSARY_TERM)
    category = manifest.entity_capability(EntityType.CATEGORY)

    assert glossary is not None
    assert glossary.supported is False
    assert category is not None
    assert category.supported is False


def test_no_field_of_data_product_is_writable(manifest: CapabilityManifest) -> None:
    for field in _DATA_PRODUCT_FIELDS:
        assert not manifest.is_writable(EntityType.DATA_PRODUCT, field)


def test_no_field_of_dataset_is_writable(manifest: CapabilityManifest) -> None:
    for field in _DATASET_FIELDS:
        assert not manifest.is_writable(EntityType.DATASET, field)


def test_an_undeclared_field_is_not_writable_either(manifest: CapabilityManifest) -> None:
    assert not manifest.is_writable(EntityType.DATA_PRODUCT, "no_such_field")
    assert not manifest.is_writable(EntityType.DATASET, "no_such_field")


def test_no_field_requires_full_replace_since_nothing_is_writable(
    manifest: CapabilityManifest,
) -> None:
    for field in _DATA_PRODUCT_FIELDS:
        assert not manifest.requires_full_replace(EntityType.DATA_PRODUCT, field)
    for field in _DATASET_FIELDS:
        assert not manifest.requires_full_replace(EntityType.DATASET, field)


def test_no_field_anywhere_carries_writable_via_or_allowed_update_paths(
    manifest: CapabilityManifest,
) -> None:
    """A stronger honesty check than "not writable": nothing here even carries the
    machinery a write path would use, so there is no dead code implying otherwise."""
    for entity_type, fields in (
        (EntityType.DATA_PRODUCT, _DATA_PRODUCT_FIELDS),
        (EntityType.DATASET, _DATASET_FIELDS),
    ):
        capability = manifest.entity_capability(entity_type)
        assert capability is not None
        assert capability.allowed_update_paths is None
        assert capability.max_update_operations is None
        for field in fields:
            field_capability = manifest.field_capability(entity_type, field)
            assert field_capability is not None
            assert field_capability.writable_via is None


def test_data_product_identity_keys_are_fqn_and_listing_global_name(
    manifest: CapabilityManifest,
) -> None:
    """RS-05 section 4.3: structural objects key on the FQN; listings key on the
    listing global name. DATA_PRODUCT covers both shapes (see manifest.py)."""
    capability = manifest.entity_capability(EntityType.DATA_PRODUCT)
    assert capability is not None
    assert capability.identity_keys
    assert "fully_qualified_name" in capability.identity_keys
    assert "listing_global_name" in capability.identity_keys


def test_dataset_identity_keys_are_fqn_and_object_id(manifest: CapabilityManifest) -> None:
    """RS-05 section 4.3: structural objects key on the FQN plus the internal numeric
    id (rename detection)."""
    capability = manifest.entity_capability(EntityType.DATASET)
    assert capability is not None
    assert capability.identity_keys
    assert "fully_qualified_name" in capability.identity_keys
    assert "object_id" in capability.identity_keys


def test_tags_are_declared_readable_for_both_entities(manifest: CapabilityManifest) -> None:
    data_product_tags = manifest.field_capability(EntityType.DATA_PRODUCT, "tags")
    dataset_tags = manifest.field_capability(EntityType.DATASET, "tags")

    assert data_product_tags is not None and data_product_tags.mode is FieldCapabilityMode.RO
    assert dataset_tags is not None and dataset_tags.mode is FieldCapabilityMode.RO


def test_comments_ie_description_are_declared_readable_for_both_entities(
    manifest: CapabilityManifest,
) -> None:
    data_product_description = manifest.field_capability(EntityType.DATA_PRODUCT, "description")
    dataset_description = manifest.field_capability(EntityType.DATASET, "description")

    assert (
        data_product_description is not None
        and data_product_description.mode is FieldCapabilityMode.RO
    )
    assert dataset_description is not None and dataset_description.mode is FieldCapabilityMode.RO


def test_dataset_classifications_are_declared_readable(manifest: CapabilityManifest) -> None:
    classifications = manifest.field_capability(EntityType.DATASET, "classifications")
    assert classifications is not None
    assert classifications.mode is FieldCapabilityMode.RO


def test_concurrency_is_none(manifest: CapabilityManifest) -> None:
    assert manifest.concurrency is ConcurrencyMode.NONE


def test_supports_events_is_false_for_both_entities(manifest: CapabilityManifest) -> None:
    data_product = manifest.entity_capability(EntityType.DATA_PRODUCT)
    dataset = manifest.entity_capability(EntityType.DATASET)
    assert data_product is not None
    assert dataset is not None
    assert data_product.supports_events is False
    assert dataset.supports_events is False


def test_manifest_round_trips_through_json(manifest: CapabilityManifest) -> None:
    restored = CapabilityManifest.model_validate(manifest.model_dump(mode="json"))

    assert restored == manifest


def test_build_manifest_is_pure_and_deterministic() -> None:
    first = build_manifest()
    second = build_manifest()

    assert first == second
