"""Shared fixtures for the Databricks connector's read-path tests (T4.4).

Mirrors the SDK's own ``tests/http/conftest.py``: an :class:`HttpEndpoint` factory
wired for fast, deterministic tests (no real sleeping, small retry counts), plus
realistic Unity Catalog schema/table payloads (RS-01 sections 1.2 and 4.1) and
``IdentityRef`` builders so each test file stays focused on the behavior it proves.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import EntityType, IdentityRef

BASE_URL = "https://acme.cloud.databricks.com"
ENDPOINT = "databricks"
TENANT_ID = "metastore-11111111"


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def base_url() -> str:
    return BASE_URL


@pytest.fixture
async def make_http() -> AsyncIterator[Callable[..., HttpEndpoint]]:
    """Factory for a test :class:`HttpEndpoint`: no real sleeping, small bounded
    attempt counts, so a retries-exhausted test stays fast. Endpoints built through
    the factory are closed automatically at teardown.
    """
    made: list[HttpEndpoint] = []

    def _make(**overrides: object) -> HttpEndpoint:
        kwargs: dict[str, object] = dict(
            sleep=_no_sleep,
            max_attempts=2,
            backoff_base_seconds=0.001,
            backoff_max_seconds=0.005,
        )
        kwargs.update(overrides)
        endpoint = HttpEndpoint(BASE_URL, **kwargs)  # type: ignore[arg-type]
        made.append(endpoint)
        return endpoint

    yield _make
    for endpoint in made:
        await endpoint.aclose()


def make_raw_schema(**overrides: Any) -> dict[str, Any]:
    """A realistic ``GET /api/2.1/unity-catalog/schemas`` list-row payload."""
    payload: dict[str, Any] = {
        "name": "sales",
        "full_name": "prod.sales",
        "catalog_name": "prod",
        "comment": "Sales domain schema",
        "owner": "data-eng-sp",
        "properties": {"team": "sales"},
        "created_at": 1700000000000,
        "created_by": "admin@acme.com",
        "updated_at": 1700003600000,
        "updated_by": "admin@acme.com",
        "schema_id": "schema-uuid-sales",
        "metastore_id": TENANT_ID,
    }
    payload.update(overrides)
    return payload


def make_raw_table(**overrides: Any) -> dict[str, Any]:
    """A realistic ``GET /api/2.1/unity-catalog/tables`` list-row (and single-get)
    payload."""
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
        "comment": "Order header rows",
        "owner": "data-eng-sp",
        "properties": {"delta.minReaderVersion": "1"},
        "table_id": "table-uuid-orders",
        "metastore_id": TENANT_ID,
        "created_at": 1700000000000,
        "created_by": "admin@acme.com",
        "updated_at": 1700003600000,
        "updated_by": "admin@acme.com",
    }
    payload.update(overrides)
    return payload


def data_product_ref(
    full_name: str = "prod.sales",
    *,
    native_key: str = "schema-uuid-sales",
    tenant_id: str = TENANT_ID,
    endpoint: str = ENDPOINT,
) -> IdentityRef:
    return IdentityRef(
        endpoint=endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=native_key,
        tenant_id=tenant_id,
        secondary_keys={"full_name": full_name},
    )


def dataset_ref(
    full_name: str = "prod.sales.orders",
    *,
    native_key: str = "table-uuid-orders",
    tenant_id: str = TENANT_ID,
    endpoint: str = ENDPOINT,
) -> IdentityRef:
    return IdentityRef(
        endpoint=endpoint,
        entity_type=EntityType.DATASET,
        native_key=native_key,
        tenant_id=tenant_id,
        secondary_keys={"full_name": full_name},
    )
