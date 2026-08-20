"""A UC payload missing every optional field never raises -- only structural identity fields
(``name``, ``full_name``, ids) are guaranteed; ``comment``, ``owner`` and ``properties`` are
not."""

from __future__ import annotations

from qlabs_connector_databricks.mapping import (
    map_custom_attributes,
    map_data_product_fields,
    map_dataset_fields,
    map_description,
    map_owners,
    map_physical_ref,
    owner_party,
)

_MINIMAL_SCHEMA = {"name": "sales", "full_name": "prod.sales"}
_MINIMAL_TABLE = {"name": "orders", "full_name": "prod.sales.orders"}


def test_minimal_schema_payload_does_not_raise() -> None:
    assert map_description(_MINIMAL_SCHEMA) == {}
    assert map_owners(_MINIMAL_SCHEMA) == {}
    assert map_custom_attributes(_MINIMAL_SCHEMA) == _MINIMAL_SCHEMA
    assert map_data_product_fields(_MINIMAL_SCHEMA) == {}


def test_minimal_table_payload_does_not_raise() -> None:
    assert map_description(_MINIMAL_TABLE) == {}
    assert map_owners(_MINIMAL_TABLE) == {}
    assert map_physical_ref(_MINIMAL_TABLE) == {"physical_ref": "prod.sales.orders"}
    assert map_dataset_fields(_MINIMAL_TABLE) == {"physical_ref": "prod.sales.orders"}


def test_completely_empty_payload_does_not_raise() -> None:
    assert map_description({}) == {}
    assert map_owners({}) == {}
    assert map_physical_ref({}) == {}
    assert map_custom_attributes({}) == {}
    assert map_data_product_fields({}) == {}
    assert map_dataset_fields({}) == {}
    assert owner_party(None) is None
