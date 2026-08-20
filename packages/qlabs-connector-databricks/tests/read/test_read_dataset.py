"""read_dataset: one table/view read by full name, no paging -- and the error
translation shared with read_schema."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import AuthError, NotFound
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import AssetType
from qlabs_connector_databricks.read import TABLES_PATH, read_dataset

from .conftest import BASE_URL, data_product_ref, dataset_ref, make_raw_table


async def test_reads_one_table_by_full_name(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    route = respx_mock.get(f"{BASE_URL}{TABLES_PATH}/prod.sales.orders").mock(
        return_value=httpx.Response(200, json=make_raw_table())
    )
    http = make_http()

    dataset = await read_dataset(http, dataset_ref())

    assert dataset.name == "orders"
    assert dataset.asset_type is AssetType.TABLE
    assert route.call_count == 1


async def test_reads_one_view_by_full_name(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    raw = make_raw_table(
        name="orders_view", full_name="prod.sales.orders_view", table_type="VIEW"
    )
    respx_mock.get(f"{BASE_URL}{TABLES_PATH}/prod.sales.orders_view").mock(
        return_value=httpx.Response(200, json=raw)
    )
    http = make_http()

    dataset = await read_dataset(
        http, dataset_ref(full_name="prod.sales.orders_view", native_key="table-uuid-orders-view")
    )

    assert dataset.asset_type is AssetType.VIEW


async def test_404_raises_not_found(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.get(f"{BASE_URL}{TABLES_PATH}/prod.sales.orders").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    http = make_http()

    with pytest.raises(NotFound):
        await read_dataset(http, dataset_ref())


async def test_401_raises_auth_error(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.get(f"{BASE_URL}{TABLES_PATH}/prod.sales.orders").mock(
        return_value=httpx.Response(401, json={"message": "invalid token"})
    )
    http = make_http()

    with pytest.raises(AuthError):
        await read_dataset(http, dataset_ref())


async def test_wrong_ref_entity_type_raises_value_error(
    make_http: Callable[..., HttpEndpoint],
) -> None:
    http = make_http()

    with pytest.raises(ValueError, match="DATASET"):
        await read_dataset(http, data_product_ref())
