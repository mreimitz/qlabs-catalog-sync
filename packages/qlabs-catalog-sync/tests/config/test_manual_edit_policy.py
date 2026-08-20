"""ManualEditPolicy: source-wins default, per-entity and per-field overrides."""

from __future__ import annotations

from qlabs_catalog_sync.config import ManualEditMode, ManualEditPolicy
from qlabs_catalog_sync_sdk.models import EntityType


def test_default_is_source_wins_when_nothing_configured() -> None:
    policy = ManualEditPolicy()
    assert policy.mode_for(EntityType.DATA_PRODUCT) == ManualEditMode.SOURCE_WINS
    assert policy.mode_for(EntityType.DATA_PRODUCT, "description") == ManualEditMode.SOURCE_WINS


def test_explicit_default_applies_when_no_override_matches() -> None:
    policy = ManualEditPolicy(default=ManualEditMode.PRESERVE_LOCAL)
    assert policy.mode_for(EntityType.DATASET) == ManualEditMode.PRESERVE_LOCAL


def test_per_entity_override_wins_over_default() -> None:
    policy = ManualEditPolicy(
        default=ManualEditMode.SOURCE_WINS,
        per_entity={EntityType.DATA_PRODUCT: ManualEditMode.PRESERVE_LOCAL},
    )

    assert policy.mode_for(EntityType.DATA_PRODUCT) == ManualEditMode.PRESERVE_LOCAL
    # An entity type with no override still falls through to the default.
    assert policy.mode_for(EntityType.DATASET) == ManualEditMode.SOURCE_WINS


def test_per_field_override_wins_over_per_entity_and_default() -> None:
    policy = ManualEditPolicy(
        default=ManualEditMode.SOURCE_WINS,
        per_entity={EntityType.DATA_PRODUCT: ManualEditMode.SOURCE_WINS},
        per_field={"data_product.description": ManualEditMode.PRESERVE_LOCAL},
    )

    # The specific field is preserved...
    assert policy.mode_for(EntityType.DATA_PRODUCT, "description") == ManualEditMode.PRESERVE_LOCAL
    # ...but any other field on the same entity type still follows the per-entity mode.
    assert policy.mode_for(EntityType.DATA_PRODUCT, "name") == ManualEditMode.SOURCE_WINS
    # And without a field at all, the per-entity mode applies too.
    assert policy.mode_for(EntityType.DATA_PRODUCT) == ManualEditMode.SOURCE_WINS


def test_per_field_key_is_scoped_to_its_entity_type() -> None:
    # "dataset.description" must not affect DATA_PRODUCT's "description" field.
    policy = ManualEditPolicy(per_field={"dataset.description": ManualEditMode.PRESERVE_LOCAL})

    assert policy.mode_for(EntityType.DATASET, "description") == ManualEditMode.PRESERVE_LOCAL
    assert policy.mode_for(EntityType.DATA_PRODUCT, "description") == ManualEditMode.SOURCE_WINS
