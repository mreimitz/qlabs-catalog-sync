"""FieldEnvelope shape: generic over its value, provenance optional, checksum not computed here."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from qlabs_catalog_sync_sdk.models import Dataset, FieldEnvelope, Tag, TextField


def test_envelope_carries_the_six_rs03_slots() -> None:
    envelope: FieldEnvelope[str] = FieldEnvelope(
        value="Retail Sales",
        source_endpoint="qlik",
        source_revision="etag-9",
        last_modified_at=datetime(2026, 8, 19, 17, 5, tzinfo=UTC),
        last_synced_at=datetime(2026, 8, 20, 9, 30, tzinfo=UTC),
        checksum="sha256:abc",
    )
    dumped = envelope.model_dump(mode="json", by_alias=True)
    assert set(dumped) == {
        "value",
        "sourceEndpoint",
        "sourceRevision",
        "lastModifiedAt",
        "lastSyncedAt",
        "checksum",
    }


def test_envelope_is_generic_over_its_value() -> None:
    text: FieldEnvelope[TextField] = FieldEnvelope(
        value=TextField.markdown("**hi**"), source_endpoint="qlik"
    )
    tags: FieldEnvelope[list[Tag]] = FieldEnvelope(
        value=[Tag(key="gold")], source_endpoint="databricks"
    )
    assert FieldEnvelope[TextField].model_validate(text.model_dump(mode="json")) == text
    assert FieldEnvelope[list[Tag]].model_validate(tags.model_dump(mode="json")) == tags


def test_envelope_rejects_a_wrong_typed_value() -> None:
    with pytest.raises(ValidationError):
        FieldEnvelope[int].model_validate({"value": "not-an-int", "sourceEndpoint": "qlik"})


def test_provenance_is_optional_but_source_endpoint_is_not() -> None:
    minimal: FieldEnvelope[str] = FieldEnvelope(value="x", source_endpoint="databricks")
    assert minimal.source_revision is None
    assert minimal.last_modified_at is None
    assert minimal.checksum is None

    with pytest.raises(ValidationError):
        FieldEnvelope[str].model_validate({"value": "x"})
    with pytest.raises(ValidationError):
        FieldEnvelope[str].model_validate({"value": "x", "sourceEndpoint": ""})


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        FieldEnvelope[str].model_validate(
            {"value": "x", "sourceEndpoint": "qlik", "lastSyncedAt": "2026-08-20T09:30:00"}
        )


def test_entity_envelope_sidecar_is_keyed_by_neutral_field_name(dataset: Dataset) -> None:
    envelope = dataset.envelope_for("name")
    assert envelope is not None
    assert envelope.source_endpoint == "databricks"
    assert dataset.envelope_for("description") is None
