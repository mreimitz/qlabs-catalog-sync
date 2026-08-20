"""``full_name`` -> ``physical_ref``, Dataset only (see the module docstring for why a
DataProduct never gets one)."""

from __future__ import annotations

from qlabs_connector_databricks.mapping import map_physical_ref

from .conftest import make_raw_table


def test_full_name_maps_to_physical_ref() -> None:
    raw = make_raw_table(full_name="prod.sales.orders")

    fields = map_physical_ref(raw)

    assert fields == {"physical_ref": "prod.sales.orders"}


def test_missing_full_name_produces_no_fragment() -> None:
    assert map_physical_ref({}) == {}


def test_blank_full_name_produces_no_fragment() -> None:
    assert map_physical_ref({"full_name": ""}) == {}


def test_non_string_full_name_produces_no_fragment() -> None:
    assert map_physical_ref({"full_name": 123}) == {}
