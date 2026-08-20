"""FieldDiff carries per-field full-replace vs partial-patch intent."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qlabs_catalog_sync_sdk.models import (
    EntityType,
    FieldChange,
    FieldDiff,
    FieldEnvelope,
    FieldUpdateMode,
    Tag,
    TextField,
)


def _diff() -> FieldDiff:
    return FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[
            FieldChange(
                field="description",
                mode=FieldUpdateMode.PATCH,
                value=TextField.plain("New text").model_dump(mode="json"),
                previous=TextField.plain("Old text").model_dump(mode="json"),
            ),
            FieldChange(
                field="dataset_refs",
                mode=FieldUpdateMode.REPLACE,
                value=["11111111-1111-4111-8111-111111111111"],
            ),
        ],
        expected_revision="etag-4",
    )


def test_default_mode_is_partial_patch() -> None:
    assert FieldChange(field="name", value="x").mode is FieldUpdateMode.PATCH
    assert FieldChange(field="name", value="x").is_full_replace is False


def test_full_replace_intent_is_readable_per_field() -> None:
    diff = _diff()
    assert diff.requires_full_replace("dataset_refs") is True
    assert diff.requires_full_replace("description") is False
    assert diff.requires_full_replace("not_in_diff") is False
    assert diff.field_names == ["description", "dataset_refs"]
    assert diff.change_for("not_in_diff") is None


def test_diff_roundtrips() -> None:
    diff = _diff()
    assert FieldDiff.model_validate(diff.model_dump(mode="json")) == diff
    assert FieldDiff.model_validate(diff.model_dump(mode="json", by_alias=True)) == diff


def test_diff_change_can_carry_the_source_envelope() -> None:
    change = FieldChange(
        field="tags",
        mode=FieldUpdateMode.REPLACE,
        value=[Tag(key="gold").model_dump(mode="json")],
        envelope=FieldEnvelope(
            value=[{"key": "gold", "value": None}], source_endpoint="databricks"
        ),
    )
    revived = FieldChange.model_validate(change.model_dump(mode="json"))
    assert revived == change
    assert revived.envelope is not None
    assert revived.envelope.source_endpoint == "databricks"


def test_duplicate_fields_in_one_diff_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate field in diff"):
        FieldDiff(
            entity_type=EntityType.DATASET,
            changes=[FieldChange(field="name", value="a"), FieldChange(field="name", value="b")],
        )


def test_an_empty_diff_is_valid_and_touches_nothing() -> None:
    diff = FieldDiff(entity_type=EntityType.DATASET)
    assert diff.changes == []
    assert diff.field_names == []
    assert diff.expected_revision is None
