"""The watermark is a typed, persistable, per-stream resume token — not a bare string."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from qlabs_catalog_sync_sdk.contract import EntityType, Watermark, WatermarkKind

EARLY = datetime(2026, 8, 19, 17, 5, 30, tzinfo=UTC)
LATE = datetime(2026, 8, 20, 9, 30, 0, tzinfo=UTC)
LATE_IN_CEST = LATE.astimezone(timezone(timedelta(hours=2)))


def test_initial_is_the_starting_position() -> None:
    mark = Watermark.initial("databricks", EntityType.DATA_PRODUCT)

    assert mark.kind is WatermarkKind.INITIAL
    assert mark.is_initial
    assert mark.timestamp is None
    assert mark.cursor is None


def test_timestamp_watermark_carries_a_modified_since_instant() -> None:
    """A Databricks `updated_at` and a Qlik RFC3339 `updatedAt` are the same shape."""
    mark = Watermark.at("qlik", EntityType.DATA_PRODUCT, LATE, observed_at=LATE)

    assert mark.kind is WatermarkKind.TIMESTAMP
    assert mark.timestamp == LATE
    assert mark.observed_at == LATE
    assert not mark.is_initial


def test_cursor_watermark_carries_an_opaque_page_token() -> None:
    mark = Watermark.from_cursor("qlik", EntityType.DATASET, "eyJwYWdlIjoyfQ==")

    assert mark.kind is WatermarkKind.CURSOR
    assert mark.cursor == "eyJwYWdlIjoyfQ=="
    assert mark.timestamp is None


def test_stream_key_identifies_the_endpoint_and_entity_type() -> None:
    mark = Watermark.initial("databricks", EntityType.DATASET)

    assert mark.stream_key == "databricks:dataset"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": WatermarkKind.TIMESTAMP},
        {"kind": WatermarkKind.CURSOR},
        {"kind": WatermarkKind.INITIAL, "cursor": "tok"},
        {"kind": WatermarkKind.INITIAL, "timestamp": LATE},
        {"kind": WatermarkKind.TIMESTAMP, "timestamp": LATE, "cursor": "tok"},
        {"kind": WatermarkKind.CURSOR, "cursor": "tok", "timestamp": LATE},
    ],
)
def test_payload_must_match_the_declared_kind(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Watermark(endpoint="qlik", entity_type=EntityType.DATA_PRODUCT, **payload)


def test_a_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Watermark(
            endpoint="qlik",
            entity_type=EntityType.DATA_PRODUCT,
            kind=WatermarkKind.TIMESTAMP,
            timestamp=datetime(2026, 8, 20, 9, 30, 0),  # noqa: DTZ001 - deliberately naive
        )


def test_an_empty_endpoint_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Watermark(endpoint="", entity_type=EntityType.DATA_PRODUCT)


@pytest.mark.parametrize(
    "mark",
    [
        Watermark.initial("qlik", EntityType.DATA_PRODUCT),
        Watermark.at("qlik", EntityType.DATA_PRODUCT, LATE, observed_at=EARLY),
        Watermark.from_cursor("qlik", EntityType.DATASET, "tok-1"),
    ],
)
def test_round_trips_through_the_state_store(mark: Watermark) -> None:
    """Persisting is `model_dump(mode="json")`; restoring is `model_validate`."""
    restored = Watermark.model_validate(mark.model_dump(mode="json"))

    assert restored == mark
    assert Watermark.model_validate(mark.model_dump(mode="json", by_alias=True)) == mark


def test_equal_instants_in_different_zones_are_the_same_watermark() -> None:
    utc = Watermark.at("qlik", EntityType.DATA_PRODUCT, LATE)
    cest = Watermark.at("qlik", EntityType.DATA_PRODUCT, LATE_IN_CEST)

    assert utc == cest


def test_timestamps_are_ordered() -> None:
    early = Watermark.at("qlik", EntityType.DATA_PRODUCT, EARLY)
    late = Watermark.at("qlik", EntityType.DATA_PRODUCT, LATE)

    assert late.is_after(early)
    assert not early.is_after(late)
    assert not late.is_after(late)


def test_everything_is_after_initial_and_initial_is_after_nothing() -> None:
    start = Watermark.initial("qlik", EntityType.DATA_PRODUCT)
    late = Watermark.at("qlik", EntityType.DATA_PRODUCT, LATE)
    cursor = Watermark.from_cursor("qlik", EntityType.DATA_PRODUCT, "tok")

    assert late.is_after(start)
    assert cursor.is_after(start)
    assert not start.is_after(late)
    assert not start.is_after(start)


def test_opaque_cursors_are_not_ordered() -> None:
    """A continuation token means nothing to the engine; it commits what it was given."""
    first = Watermark.from_cursor("qlik", EntityType.DATA_PRODUCT, "tok-1")
    second = Watermark.from_cursor("qlik", EntityType.DATA_PRODUCT, "tok-2")

    assert not second.is_after(first)
    assert not first.is_after(second)


def test_comparing_across_streams_is_a_programming_error() -> None:
    products = Watermark.at("qlik", EntityType.DATA_PRODUCT, LATE)
    datasets = Watermark.at("qlik", EntityType.DATASET, EARLY)

    assert not products.same_stream_as(datasets)
    with pytest.raises(ValueError, match="different streams"):
        products.is_after(datasets)


def test_a_watermark_cannot_be_mutated_after_the_connector_proposed_it() -> None:
    mark = Watermark.at("qlik", EntityType.DATA_PRODUCT, LATE)

    with pytest.raises(ValidationError):
        mark.timestamp = EARLY  # type: ignore[misc]

    assert mark.model_copy(update={"cursor": None}).timestamp == LATE
