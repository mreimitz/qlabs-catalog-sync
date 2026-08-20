"""Serialization round-trip: every entity survives model_dump -> model_validate intact."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from qlabs_catalog_sync_sdk.models import (
    Category,
    DataProduct,
    Dataset,
    FieldEnvelope,
    GlossaryTerm,
    IdentityRef,
    NeutralEntity,
    Party,
    PartyRole,
    Tag,
    TextField,
    TextFormat,
)

ENTITY_FIXTURES = ("data_product", "dataset", "glossary_term", "category")
ENTITY_TYPES: dict[str, type[NeutralEntity]] = {
    "data_product": DataProduct,
    "dataset": Dataset,
    "glossary_term": GlossaryTerm,
    "category": Category,
}


@pytest.mark.parametrize("fixture_name", ENTITY_FIXTURES)
def test_entity_json_roundtrip_by_field_name(fixture_name: str, request: Any) -> None:
    entity: NeutralEntity = request.getfixturevalue(fixture_name)
    model = ENTITY_TYPES[fixture_name]
    assert model.model_validate(entity.model_dump(mode="json")) == entity


@pytest.mark.parametrize("fixture_name", ENTITY_FIXTURES)
def test_entity_json_roundtrip_by_alias(fixture_name: str, request: Any) -> None:
    entity: NeutralEntity = request.getfixturevalue(fixture_name)
    model = ENTITY_TYPES[fixture_name]
    assert model.model_validate(entity.model_dump(mode="json", by_alias=True)) == entity


@pytest.mark.parametrize("fixture_name", ENTITY_FIXTURES)
def test_entity_json_string_roundtrip(fixture_name: str, request: Any) -> None:
    entity: NeutralEntity = request.getfixturevalue(fixture_name)
    model = ENTITY_TYPES[fixture_name]
    assert model.model_validate_json(entity.model_dump_json()) == entity


@pytest.mark.parametrize("fixture_name", ENTITY_FIXTURES)
def test_entity_python_roundtrip(fixture_name: str, request: Any) -> None:
    entity: NeutralEntity = request.getfixturevalue(fixture_name)
    model = ENTITY_TYPES[fixture_name]
    assert model.model_validate(entity.model_dump()) == entity


@pytest.mark.parametrize("fixture_name", ENTITY_FIXTURES)
def test_aliases_are_camel_case(fixture_name: str, request: Any) -> None:
    entity: NeutralEntity = request.getfixturevalue(fixture_name)
    dumped = entity.model_dump(mode="json", by_alias=True)
    assert "neutralId" in dumped
    assert "customAttributes" in dumped
    assert "fieldEnvelopes" in dumped
    assert "neutral_id" not in dumped


def test_value_types_roundtrip(identity_ref: IdentityRef) -> None:
    assert IdentityRef.model_validate(identity_ref.model_dump(mode="json")) == identity_ref

    for value_type, instance in (
        (TextField, TextField.markdown("# Title")),
        (Tag, Tag(key="pii", value=None)),
        (Party, Party(email="a@example.com", role=PartyRole.STEWARD)),
    ):
        assert value_type.model_validate(instance.model_dump(mode="json")) == instance


def test_datetime_roundtrip_preserves_the_instant() -> None:
    plus_two = timezone(timedelta(hours=2))
    envelope: FieldEnvelope[str] = FieldEnvelope(
        value="x",
        source_endpoint="qlik",
        source_revision="etag-1",
        last_modified_at=datetime(2026, 8, 19, 17, 5, 30, tzinfo=plus_two),
        last_synced_at=datetime(2026, 8, 20, 9, 30, tzinfo=UTC),
        checksum=None,
    )
    revived = FieldEnvelope[str].model_validate(envelope.model_dump(mode="json"))
    assert revived.last_modified_at == envelope.last_modified_at
    assert revived.last_synced_at == envelope.last_synced_at


def test_nested_value_types_survive_the_round_trip(data_product: DataProduct) -> None:
    revived = DataProduct.model_validate(data_product.model_dump(mode="json"))
    assert revived.documentation is not None
    assert revived.documentation.format is TextFormat.MARKDOWN
    assert revived.owners[0].role is PartyRole.OWNER
    assert revived.identities[0].secondary_keys == {"metastore_id": "ms-1"}
    assert revived.field_envelopes["name"].source_revision == "rev-7"
    assert revived.dataset_refs == data_product.dataset_refs
