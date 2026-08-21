"""A Snowflake row missing every optional column never raises, and never invents a default.

Only the identity columns are guaranteed by the projections ``read.py`` issues;
``COMMENT``, the owner column, tags and the listing manifest fields are all genuinely
optional, and a field Snowflake did not return must come through absent -- never as a
fabricated value.
"""

from __future__ import annotations

from qlabs_connector_snowflake.mapping import (
    asset_type_for_object,
    map_classifications,
    map_custom_attributes,
    map_data_product_fields,
    map_dataset_fields,
    map_description,
    map_documentation,
    map_listing_fields,
    map_owners,
    map_physical_ref,
    map_status,
    map_tags,
    owner_party,
    tag_reference_from_row,
)

_MINIMAL_TABLE = {"TABLE_CATALOG": "SALES_DB", "TABLE_SCHEMA": "PUBLIC", "TABLE_NAME": "ORDERS"}
_MINIMAL_SCHEMA = {"CATALOG_NAME": "SALES_DB", "SCHEMA_NAME": "PUBLIC"}
_MINIMAL_LISTING = {"global_name": "GZTSZAS2KH9", "name": "SALES_DAILY"}


def test_a_minimal_table_row_yields_only_the_physical_ref() -> None:
    assert map_dataset_fields(_MINIMAL_TABLE) == {"physical_ref": "SALES_DB.PUBLIC.ORDERS"}


def test_a_minimal_schema_row_yields_nothing_at_all() -> None:
    assert map_data_product_fields(_MINIMAL_SCHEMA) == {}


def test_a_minimal_listing_row_yields_nothing_at_all() -> None:
    assert map_listing_fields(_MINIMAL_LISTING) == {}


def test_a_completely_empty_row_never_raises() -> None:
    assert map_description({}) == {}
    assert map_documentation({}) == {}
    assert map_owners({}) == {}
    assert map_physical_ref({}) == {}
    assert map_status({}) == {}
    assert map_custom_attributes({}) == {}
    assert map_data_product_fields({}) == {}
    assert map_dataset_fields({}) == {}
    assert map_listing_fields({}) == {}
    assert owner_party(None) is None
    assert tag_reference_from_row({}) is None
    assert asset_type_for_object({}).value == "other"


def test_unread_tags_leave_both_tag_fields_absent_not_empty() -> None:
    """Absent means "this connector has nothing to say" and the engine leaves the target
    alone; ``[]`` would claim "there are none". The difference is load-bearing."""
    fields = map_dataset_fields(_MINIMAL_TABLE, tag_references=None)

    assert "tags" not in fields
    assert "classifications" not in fields
    assert map_tags(None) == {}
    assert map_classifications(None) == {}


def test_read_but_untagged_reports_explicit_empties() -> None:
    fields = map_dataset_fields(_MINIMAL_TABLE, tag_references=[])

    assert fields["tags"] == []
    assert fields["classifications"] == []


def test_no_neutral_field_is_ever_given_an_invented_default() -> None:
    """Every key present in a fragment came from a column the row actually carried."""
    assert set(map_data_product_fields(_MINIMAL_SCHEMA)) == set()
    assert set(map_dataset_fields(_MINIMAL_TABLE)) == {"physical_ref"}
    assert set(map_listing_fields(_MINIMAL_LISTING)) == set()
