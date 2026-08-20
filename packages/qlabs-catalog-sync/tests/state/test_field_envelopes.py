"""field_envelopes: round-trip write/read, and lossless nested-JSON values."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import JsonValue

from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.models import EntityType, FieldEnvelope

MODIFIED_AT = datetime(2026, 8, 19, 17, 5, 30, tzinfo=UTC)
SYNCED_AT = datetime(2026, 8, 20, 9, 30, 0, tzinfo=UTC)

NESTED_VALUE: JsonValue = {
    "tags": ["gold", "finance", "pii"],
    "properties": [
        {"key": "delta.minReaderVersion", "value": 2},
        {"key": "owner_verified", "value": True},
    ],
    "meta": {"nested": {"deeply": {"list": [1, 2, 3.5, None, "x"]}}},
    "empty_list": [],
    "empty_map": {},
    "nothing": None,
}


async def test_scalar_envelope_round_trips_every_field(store: StateStore) -> None:
    neutral_id = uuid4()
    envelope = FieldEnvelope[JsonValue](
        value="Retail Sales",
        source_endpoint="databricks",
        source_revision="rev-42",
        last_modified_at=MODIFIED_AT,
        last_synced_at=SYNCED_AT,
        checksum="sha256:abc123",
    )

    async with store.unit_of_work() as uow:
        await uow.upsert_field_envelope(
            neutral_id, "databricks", EntityType.DATA_PRODUCT, "name", envelope, now=SYNCED_AT
        )

    fetched = await store.fetch_envelopes(neutral_id, "databricks")
    assert fetched["name"] == envelope


async def test_nested_json_value_round_trips_losslessly(store: StateStore) -> None:
    neutral_id = uuid4()
    envelope = FieldEnvelope[JsonValue](
        value=NESTED_VALUE,
        source_endpoint="databricks",
    )

    async with store.unit_of_work() as uow:
        await uow.upsert_field_envelope(
            neutral_id,
            "databricks",
            EntityType.DATASET,
            "custom_attributes",
            envelope,
            now=SYNCED_AT,
        )

    fetched = await store.fetch_envelopes(neutral_id, "databricks")
    assert fetched["custom_attributes"].value == NESTED_VALUE


async def test_minimal_envelope_with_only_required_fields_round_trips(store: StateStore) -> None:
    neutral_id = uuid4()
    envelope = FieldEnvelope[JsonValue](value=None, source_endpoint="qlik")

    async with store.unit_of_work() as uow:
        await uow.upsert_field_envelope(
            neutral_id, "qlik", EntityType.GLOSSARY_TERM, "definition", envelope, now=SYNCED_AT
        )

    fetched = await store.fetch_envelopes(neutral_id, "qlik")
    got = fetched["definition"]
    assert got.value is None
    assert got.source_revision is None
    assert got.last_modified_at is None
    assert got.last_synced_at is None
    assert got.checksum is None


async def test_fetch_envelopes_is_scoped_to_one_entity_at_one_endpoint(store: StateStore) -> None:
    target = uuid4()
    other_entity = uuid4()

    async with store.unit_of_work() as uow:
        await uow.upsert_field_envelope(
            target,
            "databricks",
            EntityType.DATASET,
            "name",
            FieldEnvelope[JsonValue](value="orders", source_endpoint="databricks"),
            now=SYNCED_AT,
        )
        await uow.upsert_field_envelope(
            target,
            "databricks",
            EntityType.DATASET,
            "description",
            FieldEnvelope[JsonValue](value="order facts", source_endpoint="databricks"),
            now=SYNCED_AT,
        )
        # Same neutral_id, different endpoint -- must not show up in the databricks fetch.
        await uow.upsert_field_envelope(
            target,
            "qlik",
            EntityType.DATASET,
            "name",
            FieldEnvelope[JsonValue](value="orders (qlik)", source_endpoint="qlik"),
            now=SYNCED_AT,
        )
        # Different neutral_id, same endpoint -- must not show up either.
        await uow.upsert_field_envelope(
            other_entity,
            "databricks",
            EntityType.DATASET,
            "name",
            FieldEnvelope[JsonValue](value="customers", source_endpoint="databricks"),
            now=SYNCED_AT,
        )

    fetched = await store.fetch_envelopes(target, "databricks")
    assert set(fetched) == {"name", "description"}
    assert fetched["name"].value == "orders"


async def test_upsert_field_envelope_overwrites_the_previous_value(store: StateStore) -> None:
    neutral_id = uuid4()
    first = FieldEnvelope[JsonValue](value="v1", source_endpoint="databricks", checksum="c1")
    second = FieldEnvelope[JsonValue](value="v2", source_endpoint="databricks", checksum="c2")

    async with store.unit_of_work() as uow:
        await uow.upsert_field_envelope(
            neutral_id, "databricks", EntityType.DATASET, "name", first, now=SYNCED_AT
        )
    async with store.unit_of_work() as uow:
        await uow.upsert_field_envelope(
            neutral_id, "databricks", EntityType.DATASET, "name", second, now=SYNCED_AT
        )

    fetched = await store.fetch_envelopes(neutral_id, "databricks")
    assert fetched["name"].value == "v2"
    assert fetched["name"].checksum == "c2"


async def test_fetch_envelopes_for_unknown_entity_is_empty(store: StateStore) -> None:
    assert await store.fetch_envelopes(uuid4(), "databricks") == {}
