"""qlik_capability_manifest(): the honest declaration the engine plans every write from.

Every assertion here checks something a consumer of the manifest actually relies on
(the engine's write planner, the contract's ``ensure_writable``/``ensure_supported``
guards) rather than merely restating the literal dict this module happens to have
written — see ``test_manifest_connector_integration.py`` for the guards themselves
exercised end to end through a real ``Connector``.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.manifest import CapabilityManifest, ConcurrencyMode
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_connector_qlik.manifest import QLIK_DATA_PRODUCT_PATCH_PATHS, qlik_capability_manifest

#: Every neutral DataProduct field this manifest declares writable.
DATA_PRODUCT_RW_FIELDS = [
    "name",
    "description",
    "documentation",
    "owners",
    "tags",
    "dataset_refs",
]

#: `status` is deliberately NOT among them. Qlik changes it through the activate /
#: deactivate actions rather than the PATCH path enum, and decision D7 makes activation
#: opt-in per pair and off by default — nothing in v1 enables it. Declaring it writable
#: would have the manifest promise a write the engine plans and the connector refuses.

#: The subset of those that are array-valued and therefore full-replace-only
#: (RS-02 section 2: "Array paths are full-replace").
DATA_PRODUCT_ARRAY_FIELDS = ["owners", "tags", "dataset_refs"]

#: The scalar rw fields — must NOT require a full replace.
DATA_PRODUCT_SCALAR_RW_FIELDS = ["name", "description", "documentation"]

DATASET_DECLARED_FIELDS = [
    "name",
    "description",
    "owners",
    "tags",
    "classifications",
    "glossary_term_refs",
    "physical_ref",
    "asset_type",
]

QLIK_DATA_PRODUCT_PATCH_PATH_SET = {
    "/name",
    "/description",
    "/datasetIds",
    "/glossaryIds",
    "/readMe",
    "/keyContacts",
    "/tags",
    "/apiConsumableDatasetIds",
}


# --------------------------------------------------------------------------------------
# supports()
# --------------------------------------------------------------------------------------


def test_supports_data_product_and_dataset(qlik_manifest: CapabilityManifest) -> None:
    assert qlik_manifest.supports(EntityType.DATA_PRODUCT)
    assert qlik_manifest.supports(EntityType.DATASET)


def test_glossary_term_and_category_are_unsupported_per_d5(
    qlik_manifest: CapabilityManifest,
) -> None:
    assert not qlik_manifest.supports(EntityType.GLOSSARY_TERM)
    assert not qlik_manifest.supports(EntityType.CATEGORY)


def test_glossary_and_category_are_declared_not_omitted(qlik_manifest: CapabilityManifest) -> None:
    # D5 is a deliberate choice, not an oversight -- entity_capability must resolve to
    # a real EntityCapability(supported=False), not None.
    glossary = qlik_manifest.entity_capability(EntityType.GLOSSARY_TERM)
    category = qlik_manifest.entity_capability(EntityType.CATEGORY)
    assert glossary is not None
    assert glossary.supported is False
    assert category is not None
    assert category.supported is False


# --------------------------------------------------------------------------------------
# is_writable()
# --------------------------------------------------------------------------------------


def test_is_writable_true_for_every_declared_rw_data_product_field(
    qlik_manifest: CapabilityManifest,
) -> None:
    for field in DATA_PRODUCT_RW_FIELDS:
        assert qlik_manifest.is_writable(EntityType.DATA_PRODUCT, field), field


def test_is_writable_false_for_the_ro_placement_field(qlik_manifest: CapabilityManifest) -> None:
    # spaceId is create/move writable, never through the field-level PATCH path enum.
    assert not qlik_manifest.is_writable(EntityType.DATA_PRODUCT, "placement")


def test_is_writable_false_for_the_na_glossary_field(qlik_manifest: CapabilityManifest) -> None:
    # D5: declared na, not rw, even though /glossaryIds is a real PATCH path.
    assert not qlik_manifest.is_writable(EntityType.DATA_PRODUCT, "glossary_term_refs")


def test_is_writable_false_for_an_undeclared_field(qlik_manifest: CapabilityManifest) -> None:
    assert not qlik_manifest.is_writable(EntityType.DATA_PRODUCT, "no_such_field")


def test_dataset_has_no_writable_fields_at_all(qlik_manifest: CapabilityManifest) -> None:
    # Decision D2: the connector resolves datasets, it never writes one.
    for field in DATASET_DECLARED_FIELDS:
        assert not qlik_manifest.is_writable(EntityType.DATASET, field), field


def test_is_writable_false_for_every_field_of_an_unsupported_entity(
    qlik_manifest: CapabilityManifest,
) -> None:
    for field in ("name", "description", "no_such_field"):
        assert not qlik_manifest.is_writable(EntityType.GLOSSARY_TERM, field)
        assert not qlik_manifest.is_writable(EntityType.CATEGORY, field)


# --------------------------------------------------------------------------------------
# requires_full_replace()
# --------------------------------------------------------------------------------------


def test_requires_full_replace_true_for_exactly_the_array_valued_product_fields(
    qlik_manifest: CapabilityManifest,
) -> None:
    for field in DATA_PRODUCT_ARRAY_FIELDS:
        assert qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, field), field
    for field in DATA_PRODUCT_SCALAR_RW_FIELDS:
        assert not qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, field), field


def test_requires_full_replace_false_for_non_writable_fields(
    qlik_manifest: CapabilityManifest,
) -> None:
    assert not qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, "placement")
    assert not qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, "glossary_term_refs")
    assert not qlik_manifest.requires_full_replace(EntityType.DATA_PRODUCT, "no_such_field")
    assert not qlik_manifest.requires_full_replace(EntityType.DATASET, "tags")


# --------------------------------------------------------------------------------------
# allowed_update_paths / max_update_operations
# --------------------------------------------------------------------------------------


def test_allowed_update_paths_is_exactly_the_closed_eight_path_enum(
    qlik_manifest: CapabilityManifest,
) -> None:
    capability = qlik_manifest.entity_capability(EntityType.DATA_PRODUCT)
    assert capability is not None
    assert capability.allowed_update_paths is not None
    assert set(capability.allowed_update_paths) == QLIK_DATA_PRODUCT_PATCH_PATH_SET
    assert len(capability.allowed_update_paths) == 8


def test_allowed_update_paths_excludes_a_plausible_but_wrong_path(
    qlik_manifest: CapabilityManifest,
) -> None:
    capability = qlik_manifest.entity_capability(EntityType.DATA_PRODUCT)
    assert capability is not None
    assert capability.allowed_update_paths is not None
    # spaceId looks like an obvious PATCH candidate (it's writable at create) but is
    # not one of the 8 closed paths -- it only ever moves via the `move` action.
    assert "/spaceId" not in capability.allowed_update_paths


def test_allowed_update_paths_matches_the_exported_constant(
    qlik_manifest: CapabilityManifest,
) -> None:
    capability = qlik_manifest.entity_capability(EntityType.DATA_PRODUCT)
    assert capability is not None
    assert capability.allowed_update_paths == QLIK_DATA_PRODUCT_PATCH_PATHS


def test_dataset_declares_no_closed_update_path_enum(qlik_manifest: CapabilityManifest) -> None:
    capability = qlik_manifest.entity_capability(EntityType.DATASET)
    assert capability is not None
    assert capability.allowed_update_paths is None


def test_max_update_operations_is_eight(qlik_manifest: CapabilityManifest) -> None:
    capability = qlik_manifest.entity_capability(EntityType.DATA_PRODUCT)
    assert capability is not None
    assert capability.max_update_operations == 8


# --------------------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------------------


def test_concurrency_is_etag(qlik_manifest: CapabilityManifest) -> None:
    assert qlik_manifest.concurrency is ConcurrencyMode.ETAG


# --------------------------------------------------------------------------------------
# identity_keys
# --------------------------------------------------------------------------------------


def test_every_supported_entity_declares_non_empty_identity_keys(
    qlik_manifest: CapabilityManifest,
) -> None:
    for entity_type in (EntityType.DATA_PRODUCT, EntityType.DATASET):
        capability = qlik_manifest.entity_capability(entity_type)
        assert capability is not None
        assert capability.identity_keys


def test_data_product_identity_keys_are_id_and_qri(qlik_manifest: CapabilityManifest) -> None:
    capability = qlik_manifest.entity_capability(EntityType.DATA_PRODUCT)
    assert capability is not None
    assert capability.identity_keys == ["id", "qri"]


def test_dataset_identity_keys_lead_with_secure_qri(qlik_manifest: CapabilityManifest) -> None:
    capability = qlik_manifest.entity_capability(EntityType.DATASET)
    assert capability is not None
    assert capability.identity_keys[0] == "secure_qri"
    assert set(capability.identity_keys) == {"secure_qri", "id", "resource_id"}


# --------------------------------------------------------------------------------------
# supports_events
# --------------------------------------------------------------------------------------


def test_supports_events_is_false_for_every_declared_entity(
    qlik_manifest: CapabilityManifest,
) -> None:
    for entity_type in (EntityType.DATA_PRODUCT, EntityType.DATASET):
        capability = qlik_manifest.entity_capability(entity_type)
        assert capability is not None
        assert capability.supports_events is False


# --------------------------------------------------------------------------------------
# Round-trip / construction
# --------------------------------------------------------------------------------------


def test_manifest_round_trips_through_json(qlik_manifest: CapabilityManifest) -> None:
    restored = CapabilityManifest.model_validate(qlik_manifest.model_dump(mode="json"))
    assert restored == qlik_manifest


def test_qlik_capability_manifest_returns_an_equal_but_independent_object() -> None:
    first = qlik_capability_manifest()
    second = qlik_capability_manifest()
    assert first == second
    assert first is not second
    assert first.entities is not second.entities
