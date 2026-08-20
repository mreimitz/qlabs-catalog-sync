"""``properties`` -> ``custom_attributes``: lossless round-trip, one level of nesting,
``comment``/``owner`` excluded once they have their own neutral field."""

from __future__ import annotations

from qlabs_connector_databricks.mapping import MAPPED_SOURCE_FIELDS, map_custom_attributes

from .conftest import make_raw_schema, make_raw_table

_SCHEMA_STRUCTURAL_FIELDS = frozenset(
    {"name", "full_name", "catalog_name", "schema_id", "metastore_id"}
)
_TABLE_STRUCTURAL_FIELDS = frozenset(
    {"name", "full_name", "catalog_name", "schema_name", "table_id", "metastore_id"}
)


def test_properties_round_trip_byte_identical_including_nested_and_mixed_types() -> None:
    properties = {
        "team": "sales",
        "cost_center": 4821,
        "pii": False,
        "tier": None,
        "ratio": 0.5,
        "tags_freeform": ["gold", "curated"],
        "nested": {"contact": {"slack": "#sales-data", "escalation_minutes": 30}},
    }
    raw = make_raw_schema(properties=properties)

    attributes = map_custom_attributes(raw, exclude=_SCHEMA_STRUCTURAL_FIELDS)

    assert attributes["properties"] == properties


def test_comment_and_owner_are_excluded_even_without_a_caller_supplied_exclude_set() -> None:
    raw = make_raw_schema()

    attributes = map_custom_attributes(raw)

    assert "comment" not in attributes
    assert "owner" not in attributes
    assert attributes["properties"] == raw["properties"]


def test_caller_supplied_exclude_removes_structural_fields_and_keeps_the_rest() -> None:
    raw = make_raw_table()

    attributes = map_custom_attributes(raw, exclude=_TABLE_STRUCTURAL_FIELDS)

    for field in _TABLE_STRUCTURAL_FIELDS:
        assert field not in attributes
    assert attributes["properties"] == raw["properties"]
    assert attributes["columns"] == raw["columns"]
    assert attributes["table_type"] == "MANAGED"
    assert attributes["storage_location"] == raw["storage_location"]
    assert "comment" not in attributes
    assert "owner" not in attributes


def test_properties_are_never_flattened_into_the_top_level() -> None:
    raw = make_raw_schema(
        properties={"comment": "a property that happens to share a raw field name"}
    )

    attributes = map_custom_attributes(raw)

    # The top-level "comment" (the schema's actual comment) is excluded via
    # MAPPED_SOURCE_FIELDS; the property named "comment" survives untouched, nested under
    # "properties" -- the two never collide because properties is never flattened into the
    # same namespace as the sibling raw fields.
    assert attributes["properties"]["comment"] == (
        "a property that happens to share a raw field name"
    )
    assert "comment" not in attributes


def test_properties_are_never_double_nested() -> None:
    raw = make_raw_schema(properties={"team": "sales"})

    attributes = map_custom_attributes(raw)

    assert attributes["properties"] == {"team": "sales"}
    assert "properties" not in attributes["properties"]


def test_empty_input_does_not_raise() -> None:
    assert map_custom_attributes({}) == {}


def test_mapped_source_fields_is_exactly_comment_and_owner() -> None:
    assert frozenset({"comment", "owner"}) == MAPPED_SOURCE_FIELDS
