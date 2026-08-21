"""iter_matching_schemas: bulk schema discovery across catalogs, honoring selector
patterns, paging defensively."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.read import SCHEMAS_PATH, iter_matching_schemas

from .conftest import BASE_URL, ENDPOINT, make_raw_schema


async def test_includes_matches_and_excludes_near_misses_across_catalogs(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    route = respx_mock.get(f"{BASE_URL}{SCHEMAS_PATH}").mock(
        side_effect=[
            # catalog "prod": one matching schema, one near-miss (wrong shape).
            httpx.Response(
                200,
                json={
                    "schemas": [
                        make_raw_schema(name="sales_eu", full_name="prod.sales_eu"),
                        make_raw_schema(name="marketing", full_name="prod.marketing"),
                    ]
                },
            ),
            # catalog "staging": would match the schema-glob but wrong catalog.
            httpx.Response(
                200,
                json={"schemas": [make_raw_schema(name="sales_x", full_name="staging.sales_x")]},
            ),
        ]
    )
    http = make_http()

    results = [
        raw
        async for raw in iter_matching_schemas(
            http,
            catalog_names=["prod", "staging"],
            catalog_schema_patterns=["prod.sales_*"],
            endpoint=ENDPOINT,
        )
    ]

    assert [raw["full_name"] for raw in results] == ["prod.sales_eu"]
    assert route.call_count == 2  # one list call per catalog


async def test_deduplicates_repeated_catalog_names(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    route = respx_mock.get(f"{BASE_URL}{SCHEMAS_PATH}").mock(
        return_value=httpx.Response(200, json={"schemas": [make_raw_schema()]})
    )
    http = make_http()

    results = [
        raw
        async for raw in iter_matching_schemas(
            http,
            catalog_names=["prod", "prod"],
            catalog_schema_patterns=["prod.*"],
            endpoint=ENDPOINT,
        )
    ]

    assert len(results) == 1
    assert route.call_count == 1


async def test_walks_multiple_pages_within_one_catalog(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    route = respx_mock.get(f"{BASE_URL}{SCHEMAS_PATH}").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "schemas": [make_raw_schema(name="sales_eu", full_name="prod.sales_eu")],
                    "next_page_token": "page-2",
                },
            ),
            httpx.Response(
                200, json={"schemas": [make_raw_schema(name="sales_us", full_name="prod.sales_us")]}
            ),
        ]
    )
    http = make_http()

    results = [
        raw
        async for raw in iter_matching_schemas(
            http,
            catalog_names=["prod"],
            catalog_schema_patterns=["prod.sales_*"],
            endpoint=ENDPOINT,
        )
    ]

    assert [r["name"] for r in results] == ["sales_eu", "sales_us"]
    assert route.call_count == 2


async def test_runaway_pagination_fails_fast_instead_of_hanging(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
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
        async for _ in iter_matching_schemas(
            http,
            catalog_names=["prod"],
            catalog_schema_patterns=["prod.*"],
            endpoint=ENDPOINT,
            max_items=3,
        ):
            pass
