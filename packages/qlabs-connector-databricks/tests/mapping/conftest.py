"""Shared fixtures for the Databricks connector's mapping tests (T4.5).

Independent of ``tests/read/conftest.py`` (T4.4's own fixtures) on purpose: this suite tests
``mapping.py`` in isolation against raw Unity Catalog payloads it builds itself, so it never
depends on -- and can never accidentally couple to -- ``read.py``'s test fixtures or its
private helpers.
"""

from __future__ import annotations

from typing import Any

ENDPOINT = "databricks"


def make_raw_schema(**overrides: Any) -> dict[str, Any]:
    """A realistic ``GET /api/2.1/unity-catalog/schemas`` list-row payload."""
    payload: dict[str, Any] = {
        "name": "sales",
        "full_name": "prod.sales",
        "catalog_name": "prod",
        "comment": "Sales domain schema, owned by the commercial analytics team.",
        "owner": "sales-analytics@acme.com",
        "properties": {
            "team": "sales",
            "cost_center": 4821,
            "pii": False,
            "tier": None,
            "ratio": 0.5,
            "tags_freeform": ["gold", "curated"],
            "nested": {"contact": {"slack": "#sales-data", "escalation_minutes": 30}},
        },
        "created_at": 1700000000000,
        "created_by": "admin@acme.com",
        "updated_at": 1700003600000,
        "updated_by": "admin@acme.com",
        "schema_id": "schema-uuid-sales",
        "metastore_id": "metastore-11111111",
    }
    payload.update(overrides)
    return payload


def make_raw_table(**overrides: Any) -> dict[str, Any]:
    """A realistic ``GET /api/2.1/unity-catalog/tables`` payload."""
    payload: dict[str, Any] = {
        "name": "orders",
        "full_name": "prod.sales.orders",
        "catalog_name": "prod",
        "schema_name": "sales",
        "table_type": "MANAGED",
        "data_source_format": "DELTA",
        "columns": [
            {"name": "id", "type_name": "BIGINT", "nullable": False, "position": 0},
            {"name": "amount", "type_name": "DECIMAL", "nullable": True, "position": 1},
        ],
        "comment": "Order header rows, one per checkout.",
        "owner": "e3b0c442-98fc-4e1c-8b1a-3f1b2c4d5e6f",
        "properties": {"delta.minReaderVersion": "1", "quality": "gold"},
        "storage_location": "s3://acme-lake/prod/sales/orders",
        "table_id": "table-uuid-orders",
        "metastore_id": "metastore-11111111",
        "created_at": 1700000000000,
        "created_by": "admin@acme.com",
        "updated_at": 1700003600000,
        "updated_by": "admin@acme.com",
    }
    payload.update(overrides)
    return payload
