"""Same input mapped twice produces identical checksums -- the property the engine's
idempotency (``envelope.py``'s whole reason for existing) rests on. Every function here is a
pure transform of a plain dict, so this is a directness proof, not a mocked one: build the
fragment from two independent copies of the same raw payload and compare checksums."""

from __future__ import annotations

from qlabs_catalog_sync_sdk.envelope import compute_checksum
from qlabs_connector_databricks.mapping import (
    map_custom_attributes,
    map_data_product_fields,
    map_dataset_fields,
    map_description,
    map_owners,
)

from .conftest import make_raw_schema, make_raw_table


def test_description_checksum_is_deterministic() -> None:
    raw = make_raw_schema(comment="Sales domain schema.")

    first = map_description(dict(raw))["description"]
    second = map_description(dict(raw))["description"]

    assert compute_checksum(first) == compute_checksum(second)


def test_owners_checksum_is_deterministic() -> None:
    raw = make_raw_schema(owner="sales-analytics@acme.com")

    first = map_owners(dict(raw))["owners"]
    second = map_owners(dict(raw))["owners"]

    assert compute_checksum(first) == compute_checksum(second)


def test_custom_attributes_checksum_is_deterministic_including_nested_properties() -> None:
    raw = make_raw_table()

    first = map_custom_attributes(dict(raw))
    second = map_custom_attributes(dict(raw))

    assert compute_checksum(first) == compute_checksum(second)


def test_data_product_content_fields_checksum_is_deterministic() -> None:
    raw = make_raw_schema()

    first = map_data_product_fields(dict(raw))
    second = map_data_product_fields(dict(raw))

    assert set(first) == set(second)
    for key in first:
        assert compute_checksum(first[key]) == compute_checksum(second[key]), key


def test_dataset_content_fields_checksum_is_deterministic() -> None:
    raw = make_raw_table()

    first = map_dataset_fields(dict(raw))
    second = map_dataset_fields(dict(raw))

    assert set(first) == set(second)
    for key in first:
        assert compute_checksum(first[key]) == compute_checksum(second[key]), key
