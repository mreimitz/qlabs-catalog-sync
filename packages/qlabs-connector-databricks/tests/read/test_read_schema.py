"""read_schema: a UC schema read into a DataProduct with its tables/views as member
Datasets, paging defensively over /schemas and /tables, honoring selector patterns."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import AuthError, NotFound, TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.read import SCHEMAS_PATH, TABLES_PATH, read_schema

from .conftest import BASE_URL, data_product_ref, make_raw_schema, make_raw_table


async def test_reads_the_schema_and_its_tables_as_member_datasets(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.get(f"{BASE_URL}{SCHEMAS_PATH}").mock(
        return_value=httpx.Response(200, json={"schemas": [make_raw_schema()]})
    )
    respx_mock.get(f"{BASE_URL}{TABLES_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "tables": [
                    make_raw_table(name="orders", full_name="prod.sales.orders", table_id="t1"),
                    make_raw_table(
                        name="orders_view",
                        full_name="prod.sales.orders_view",
                        table_id="t2",
                        table_type="VIEW",
                    ),
                ]
            },
        )
    )
    http = make_http()

    result = await read_schema(http, data_product_ref(), catalog_schema_patterns=["prod.sales*"])

    assert result.data_product.name == "sales"
    assert [d.name for d in result.datasets] == ["orders", "orders_view"]
    # dataset_refs deliberately stays empty -- see SchemaRead's docstring.
    assert result.data_product.dataset_refs == []


async def test_finds_the_schema_across_multiple_pages_and_stops_paging(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    route = respx_mock.get(f"{BASE_URL}{SCHEMAS_PATH}").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "schemas": [make_raw_schema(name="marketing", full_name="prod.marketing")],
                    "next_page_token": "page-2",
                },
            ),
            httpx.Response(200, json={"schemas": [make_raw_schema()]}),  # "sales" is here
        ]
    )
    respx_mock.get(f"{BASE_URL}{TABLES_PATH}").mock(
        return_value=httpx.Response(200, json={"tables": []})
    )
    http = make_http()

    result = await read_schema(http, data_product_ref(), catalog_schema_patterns=["prod.*"])

    assert result.data_product.name == "sales"
    assert route.call_count == 2  # stopped once found; did not assume one page either


async def test_walks_multiple_pages_of_tables(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.get(f"{BASE_URL}{SCHEMAS_PATH}").mock(
        return_value=httpx.Response(200, json={"schemas": [make_raw_schema()]})
    )
    tables_route = respx_mock.get(f"{BASE_URL}{TABLES_PATH}").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "tables": [make_raw_table(name="orders", table_id="t1")],
                    "next_page_token": "page-2",
                },
            ),
            httpx.Response(
                200, json={"tables": [make_raw_table(name="line_items", table_id="t2")]}
            ),
        ]
    )
    http = make_http()

    result = await read_schema(http, data_product_ref(), catalog_schema_patterns=["prod.sales"])

    assert [d.name for d in result.datasets] == ["orders", "line_items"]
    assert tables_route.call_count == 2


async def test_ref_outside_selector_patterns_raises_not_found(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    schemas_route = respx_mock.get(f"{BASE_URL}{SCHEMAS_PATH}")
    http = make_http()

    with pytest.raises(NotFound):
        await read_schema(http, data_product_ref(), catalog_schema_patterns=["staging.*"])

    # No request was even made: excluded before any I/O.
    assert schemas_route.call_count == 0


async def test_schema_absent_from_the_listing_raises_not_found(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.get(f"{BASE_URL}{SCHEMAS_PATH}").mock(
        return_value=httpx.Response(
            200, json={"schemas": [make_raw_schema(name="marketing", full_name="prod.marketing")]}
        )
    )
    http = make_http()

    with pytest.raises(NotFound):
        await read_schema(http, data_product_ref(), catalog_schema_patterns=["prod.*"])


async def test_404_raises_not_found(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.get(f"{BASE_URL}{SCHEMAS_PATH}").mock(return_value=httpx.Response(404, json={}))
    http = make_http()

    with pytest.raises(NotFound):
        await read_schema(http, data_product_ref(), catalog_schema_patterns=["prod.sales"])


async def test_401_raises_auth_error(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.get(f"{BASE_URL}{SCHEMAS_PATH}").mock(return_value=httpx.Response(401, json={}))
    http = make_http()

    with pytest.raises(AuthError):
        await read_schema(http, data_product_ref(), catalog_schema_patterns=["prod.sales"])


async def test_runaway_pagination_fails_fast_instead_of_hanging(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    # A pathological server that always claims there is another page: without a
    # defensive cap this would loop forever instead of failing a test.
    respx_mock.get(f"{BASE_URL}{SCHEMAS_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "schemas": [make_raw_schema(name="marketing", full_name="prod.marketing")],
                "next_page_token": "always-more",
            },
        )
    )
    http = make_http()

    with pytest.raises(TransientError, match="did not terminate"):
        await read_schema(
            http,
            data_product_ref(),
            catalog_schema_patterns=["prod.*"],
            max_items=3,
        )
