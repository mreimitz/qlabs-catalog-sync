"""``INFORMATION_SCHEMA`` is per-catalog, so a query is inherently scoped to one
catalog -- but reading tags for a data product with many tables must never mean one
statement per table. These tests assert the actual statement counts, not just that
the module claims to batch."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.sql_tags import read_catalog_tags, read_tags_for_catalogs

from .conftest import (
    ENDPOINT,
    SCHEMA_STMT_ID,
    TABLE_STMT_ID,
    statements_url,
    succeeded_response,
    table_tag_row,
)


async def test_two_hundred_tables_in_one_catalog_cost_exactly_two_statements(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    many_rows = [
        table_tag_row("prod", "sales", f"table_{i}", "domain", "commerce") for i in range(200)
    ]
    post_route = respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        side_effect=[
            httpx.Response(200, json=succeeded_response(SCHEMA_STMT_ID, rows=[])),
            httpx.Response(200, json=succeeded_response(TABLE_STMT_ID, rows=many_rows)),
        ]
    )
    http = make_http()

    index = await read_catalog_tags(
        http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
    )

    assert index is not None
    assert post_route.call_count == 2  # one SCHEMA_TAGS statement, one TABLE_TAGS statement
    assert len(index.for_table("prod.sales.table_199")) == 1
    assert len(index.table_tags) == 200


async def test_reading_across_several_catalogs_issues_one_pair_of_statements_each(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    # Six responses total: (schema statement, table statement) x 3 catalogs, in the
    # exact order read_catalog_tags issues them for each catalog in turn.
    responses = []
    for _ in range(3):
        responses.append(httpx.Response(200, json=succeeded_response(SCHEMA_STMT_ID, rows=[])))
        responses.append(httpx.Response(200, json=succeeded_response(TABLE_STMT_ID, rows=[])))
    post_route = respx_mock.post(statements_url()).mock(side_effect=responses)  # type: ignore[attr-defined]

    http = make_http()

    result = await read_tags_for_catalogs(
        http,
        sql_warehouse_id="wh-1",
        catalog_names=["prod", "staging", "dev"],
        endpoint=ENDPOINT,
    )

    assert result is not None
    assert set(result) == {"prod", "staging", "dev"}
    assert post_route.call_count == 6  # 2 statements per catalog, 3 catalogs
