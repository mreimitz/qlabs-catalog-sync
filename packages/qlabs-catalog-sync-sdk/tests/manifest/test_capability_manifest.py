"""CapabilityManifest: the concrete type contract.CapabilityManifestBase anchors.

Exercises the manifest against two realistic shapes built in ``conftest.py`` — a
Qlik-shaped write manifest and a Databricks-shaped read-only one — rather than a toy
manifest, so the behavior proven here is the behavior T3.2 (Qlik), T4.2 (Databricks) and
the conformance kit (T1.8) will actually depend on.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.contract import CapabilityManifestBase
from qlabs_catalog_sync_sdk.manifest import CapabilityManifest, ConcurrencyMode, FieldCapabilityMode
from qlabs_catalog_sync_sdk.models import EntityType


def test_manifest_is_a_capability_manifest_base(qlik_manifest: CapabilityManifest) -> None:
    assert isinstance(qlik_manifest, CapabilityManifestBase)


def test_supports_matches_declared_entities(qlik_manifest: CapabilityManifest) -> None:
    assert qlik_manifest.supports(EntityType.DATA_PRODUCT)
    assert qlik_manifest.supports(EntityType.DATASET)
    assert not qlik_manifest.supports(EntityType.GLOSSARY_TERM)  # explicitly unsupported, D5
    assert not qlik_manifest.supports(EntityType.CATEGORY)  # never mentioned at all


def test_is_writable_true_only_for_declared_rw_fields(qlik_manifest: CapabilityManifest) -> None:
    assert qlik_manifest.is_writable(EntityType.DATA_PRODUCT, "name")
    assert qlik_manifest.is_writable(EntityType.DATA_PRODUCT, "tags")
    assert qlik_manifest.is_writable(EntityType.DATA_PRODUCT, "dataset_refs")


def test_is_writable_false_for_ro(qlik_manifest: CapabilityManifest) -> None:
    assert not qlik_manifest.is_writable(EntityType.DATA_PRODUCT, "placement")


def test_is_writable_false_for_na(qlik_manifest: CapabilityManifest) -> None:
    assert not qlik_manifest.is_writable(EntityType.DATA_PRODUCT, "glossary_term_refs")


def test_is_writable_false_for_an_undeclared_field(qlik_manifest: CapabilityManifest) -> None:
    assert not qlik_manifest.is_writable(EntityType.DATA_PRODUCT, "no_such_field")


def test_is_writable_false_for_an_explicitly_unsupported_entity(
    qlik_manifest: CapabilityManifest,
) -> None:
    assert not qlik_manifest.is_writable(EntityType.GLOSSARY_TERM, "name")


def test_is_writable_false_for_an_entity_never_mentioned(qlik_manifest: CapabilityManifest) -> None:
    assert not qlik_manifest.is_writable(EntityType.CATEGORY, "name")


def test_requires_full_replace_true_exactly_where_partial_update_is_false(
    qlik_manifest: CapabilityManifest,
) -> None:
    assert qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, "tags")
    assert qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, "owners")
    assert qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, "dataset_refs")
    assert not qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, "name")
    assert not qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, "description")
    assert not qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, "documentation")


def test_requires_full_replace_false_for_non_writable_and_undeclared(
    qlik_manifest: CapabilityManifest,
) -> None:
    assert not qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, "placement")
    assert not qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, "glossary_term_refs")
    assert not qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, "no_such_field")
    assert not qlik_manifest.requires_full_replace(EntityType.CATEGORY, "name")


def test_an_unsupported_entity_answers_false_everywhere_without_raising(
    qlik_manifest: CapabilityManifest,
) -> None:
    for field in ("name", "description", "no_such_field"):
        assert not qlik_manifest.is_writable(EntityType.GLOSSARY_TERM, field)
        assert not qlik_manifest.requires_full_replace(EntityType.GLOSSARY_TERM, field)
    assert not qlik_manifest.supports(EntityType.GLOSSARY_TERM)


def test_a_never_mentioned_entity_answers_false_everywhere_without_raising(
    qlik_manifest: CapabilityManifest,
) -> None:
    for field in ("name", "description", "no_such_field"):
        assert not qlik_manifest.is_writable(EntityType.CATEGORY, field)
        assert not qlik_manifest.requires_full_replace(EntityType.CATEGORY, field)
    assert not qlik_manifest.supports(EntityType.CATEGORY)


def test_read_only_source_connector_manifest_writes_nothing(
    databricks_manifest: CapabilityManifest,
) -> None:
    for entity_type in (EntityType.DATA_PRODUCT, EntityType.DATASET):
        for field in ("name", "description", "tags", "owners", "physical_ref"):
            assert not databricks_manifest.is_writable(entity_type, field)


def test_d6_tags_are_ro_with_a_warehouse_and_na_without_one(
    databricks_manifest: CapabilityManifest,
    databricks_manifest_no_warehouse: CapabilityManifest,
) -> None:
    with_warehouse = databricks_manifest.field_capability(EntityType.DATA_PRODUCT, "tags")
    without_warehouse = databricks_manifest_no_warehouse.field_capability(
        EntityType.DATA_PRODUCT, "tags"
    )

    assert with_warehouse is not None
    assert without_warehouse is not None
    assert with_warehouse.mode is FieldCapabilityMode.RO
    assert without_warehouse.mode is FieldCapabilityMode.NA


def test_manifest_round_trips_through_json(qlik_manifest: CapabilityManifest) -> None:
    restored = CapabilityManifest.model_validate(qlik_manifest.model_dump(mode="json"))

    assert restored == qlik_manifest


def test_databricks_manifest_round_trips_through_json(
    databricks_manifest: CapabilityManifest,
) -> None:
    restored = CapabilityManifest.model_validate(databricks_manifest.model_dump(mode="json"))

    assert restored == databricks_manifest


def test_concurrency_accepts_a_bare_string_like_the_agent_guide_example() -> None:
    manifest = CapabilityManifest(entities={}, concurrency="none")

    assert manifest.concurrency is ConcurrencyMode.NONE


def test_qlik_manifest_declares_etag_concurrency(qlik_manifest: CapabilityManifest) -> None:
    assert qlik_manifest.concurrency is ConcurrencyMode.ETAG


def test_databricks_manifest_declares_no_concurrency_token(
    databricks_manifest: CapabilityManifest,
) -> None:
    assert databricks_manifest.concurrency is ConcurrencyMode.NONE


def test_supported_entity_types_property(qlik_manifest: CapabilityManifest) -> None:
    assert qlik_manifest.supported_entity_types == {EntityType.DATA_PRODUCT, EntityType.DATASET}


def test_entity_capability_lookup_is_none_for_a_never_mentioned_entity(
    qlik_manifest: CapabilityManifest,
) -> None:
    assert qlik_manifest.entity_capability(EntityType.CATEGORY) is None


def test_field_capability_lookup_is_none_for_an_undeclared_field(
    qlik_manifest: CapabilityManifest,
) -> None:
    assert qlik_manifest.field_capability(EntityType.DATA_PRODUCT, "no_such_field") is None
