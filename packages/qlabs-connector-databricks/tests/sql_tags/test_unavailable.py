"""Decision D6, the read half: without a SQL warehouse, tag reads are structurally
"unavailable" -- ``None``, not an empty :class:`CatalogTagIndex`, and no HTTP call is
made at all. This is the one distinction the whole module exists to protect (an empty
read would be indistinguishable from "this object genuinely has no tags", which would
make a sync silently *clear* tags in the target)."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.sql_tags import read_catalog_tags, read_tags_for_catalogs

from .conftest import BASE_URL, ENDPOINT


async def test_no_warehouse_returns_none_and_makes_no_http_call(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    catch_all = respx_mock.route(host="acme.cloud.databricks.com").mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json={})
    )
    http = make_http()

    result = await read_catalog_tags(
        http, sql_warehouse_id=None, catalog_name="prod", endpoint=ENDPOINT
    )

    assert result is None
    assert catch_all.call_count == 0


async def test_no_warehouse_is_unavailable_even_for_many_catalogs(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    catch_all = respx_mock.route(host="acme.cloud.databricks.com").mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json={})
    )
    http = make_http()

    result = await read_tags_for_catalogs(
        http,
        sql_warehouse_id=None,
        catalog_names=["prod", "staging", "dev", "sandbox"],
        endpoint=ENDPOINT,
    )

    assert result is None
    assert catch_all.call_count == 0


async def test_a_bad_identifier_does_not_change_the_unavailable_verdict(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    """Even a malformed catalog name never reaches validation when there is no
    warehouse -- the ``sql_warehouse_id is None`` short-circuit comes first, so
    "unavailable" is decided before anything else about the call is inspected."""
    catch_all = respx_mock.route(host="acme.cloud.databricks.com").mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json={})
    )
    http = make_http()

    result = await read_catalog_tags(
        http, sql_warehouse_id=None, catalog_name="not a valid identifier!", endpoint=ENDPOINT
    )

    assert result is None
    assert catch_all.call_count == 0


async def test_with_a_warehouse_the_result_is_a_real_index_not_none(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    """The contrast case: with a warehouse configured, the same call returns a real
    (possibly empty) :class:`CatalogTagIndex`, never ``None`` -- proving the two
    branches are structurally distinct, not just differently populated."""
    respx_mock.post(f"{BASE_URL}/api/2.0/sql/statements").mock(
        side_effect=[
            httpx.Response(
                200, json={"statement_id": "s1", "status": {"state": "SUCCEEDED"}, "result": {}}
            ),
            httpx.Response(
                200, json={"statement_id": "t1", "status": {"state": "SUCCEEDED"}, "result": {}}
            ),
        ]
    )
    http = make_http()

    result = await read_catalog_tags(
        http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
    )

    assert result is not None
    assert result.for_schema("prod.sales") == []
    assert result.for_table("prod.sales.orders") == []
