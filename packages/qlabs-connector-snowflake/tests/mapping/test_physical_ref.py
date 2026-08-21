"""``DATABASE.SCHEMA.OBJECT`` -> ``physical_ref``, Dataset only (a DataProduct has no
``physical_ref`` field at all -- RS-03 section 3.1)."""

from __future__ import annotations

from qlabs_connector_snowflake.mapping import map_physical_ref

from .conftest import make_raw_table, make_raw_view


def test_the_three_catalog_columns_assemble_the_fully_qualified_name() -> None:
    fields = map_physical_ref(make_raw_table())

    assert fields == {"physical_ref": "SALES_DB.PUBLIC.ORDERS"}


def test_a_views_row_assembles_the_same_way() -> None:
    fields = map_physical_ref(make_raw_view())

    assert fields == {"physical_ref": "SALES_DB.PUBLIC.ORDERS_EU"}


def test_the_form_is_unquoted_and_dot_separated() -> None:
    """RS-05 sections 1.2/4.3 name the unquoted dotted form as the primary matching key --
    the same string ``read.py`` uses as the ``IdentityRef`` native key."""
    physical_ref = map_physical_ref(make_raw_table())["physical_ref"]

    assert '"' not in physical_ref
    assert physical_ref.count(".") == 2


def test_case_is_preserved_exactly_as_snowflake_reported_it() -> None:
    raw = make_raw_table(TABLE_CATALOG="Sales_DB", TABLE_SCHEMA="public", TABLE_NAME="Orders")

    assert map_physical_ref(raw) == {"physical_ref": "Sales_DB.public.Orders"}


def test_a_precomputed_fully_qualified_name_column_wins() -> None:
    raw = make_raw_table(FULLY_QUALIFIED_NAME="OTHER_DB.OTHER_SCHEMA.OTHER")

    assert map_physical_ref(raw) == {"physical_ref": "OTHER_DB.OTHER_SCHEMA.OTHER"}


def test_a_missing_part_produces_no_fragment_rather_than_a_partial_name() -> None:
    raw = make_raw_table()
    del raw["TABLE_SCHEMA"]

    assert map_physical_ref(raw) == {}


def test_a_null_or_non_string_part_produces_no_fragment() -> None:
    assert map_physical_ref(make_raw_table(TABLE_NAME=None)) == {}
    assert map_physical_ref(make_raw_table(TABLE_NAME=123)) == {}
    assert map_physical_ref(make_raw_table(TABLE_NAME="")) == {}


def test_an_empty_row_produces_no_fragment() -> None:
    assert map_physical_ref({}) == {}
