"""Paging, and telling the engine the truth about exhaustion.

The SQL REST API returns a large result set in ``resultSetMetaData.partitionInfo``
partitions, fetched by re-getting the same statement handle with ``?partition=N`` (RS-05
section 3.8). :class:`StatementClient` already drains those, and the change feed leans on
that completeness: a census, its delete detection and its "re-running finds nothing"
property are only sound if the scan actually saw every row the query matched.

So two behaviors, each proven directly:

* A multi-partition scan is **fully drained** -- every partition is fetched and every row
  in it becomes a candidate, not just the rows in the first response.
* A pathological partition count trips :data:`DEFAULT_MAX_PARTITIONS` and raises a typed,
  retryable error rather than paging on. Truncating instead would hand back a partial
  traversal that looks exactly like a complete one, and the very next poll would read the
  missing objects as deleted.

``has_more`` is always ``False`` here, and that is honest rather than a shortcut: one call
is one complete traversal, so there is genuinely no next page for the engine to ask for.
``is_exhausted`` is its inverse and must therefore always be ``True`` on a successful
return -- pinned below so a future change that starts truncating cannot quietly keep
claiming exhaustion.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, Watermark
from qlabs_catalog_sync_sdk.exceptions import TransientError
from qlabs_connector_snowflake.read import DEFAULT_MAX_PARTITIONS, StatementClient

from ..conftest import ENDPOINT, partition_response, statement_url
from .conftest import (
    ACCOUNT_USAGE_TABLES_COLUMNS,
    NOW_1,
    TABLES_SQL,
    StatementRouter,
    poll,
    result_response,
    set_now,
    table_row,
)


async def test_a_multi_partition_scan_is_fully_drained(
    client: StatementClient, router: StatementRouter, respx_mock: Any
) -> None:
    set_now(router, NOW_1)
    router.on(
        TABLES_SQL,
        httpx.Response(
            200,
            json=result_response(
                ACCOUNT_USAGE_TABLES_COLUMNS,
                [table_row("ORDERS", table_id="1")],
                partitions=3,
            ),
        ),
    )
    partitions = respx_mock.get(statement_url()).mock(
        side_effect=[
            httpx.Response(200, json=partition_response([table_row("CUSTOMERS", table_id="2")])),
            httpx.Response(200, json=partition_response([table_row("REGIONS", table_id="3")])),
        ]
    )

    result = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))

    assert partitions.call_count == 2
    assert {change.ref.native_key for change in result.changes} == {
        "SALES_DB.PUBLIC.ORDERS",
        "SALES_DB.PUBLIC.CUSTOMERS",
        "SALES_DB.PUBLIC.REGIONS",
    }
    assert result.has_more is False
    assert result.is_exhausted is True


async def test_a_runaway_partition_count_raises_instead_of_paging_forever(
    client: StatementClient, router: StatementRouter, respx_mock: Any
) -> None:
    """The defensive backstop, proved to fire *before* any partition fetch -- so a
    pathological result set costs one request, not thousands."""
    set_now(router, NOW_1)
    router.on(
        TABLES_SQL,
        httpx.Response(
            200,
            json=result_response(
                ACCOUNT_USAGE_TABLES_COLUMNS,
                [table_row("ORDERS")],
                partitions=DEFAULT_MAX_PARTITIONS + 1,
            ),
        ),
    )
    partitions = respx_mock.get(statement_url()).mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(TransientError, match="refusing to page further"):
        await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))

    assert partitions.call_count == 0


async def test_a_partial_traversal_never_produces_a_watermark(
    client: StatementClient, router: StatementRouter, respx_mock: Any
) -> None:
    """A scan that could not be completed must not leave behind a census that would make
    every unseen object look deleted on the next poll -- so it raises, and the engine keeps
    the watermark it already had."""
    set_now(router, NOW_1, NOW_1)
    router.on(
        TABLES_SQL,
        httpx.Response(
            200,
            json=result_response(
                ACCOUNT_USAGE_TABLES_COLUMNS,
                [table_row("ORDERS", table_id="1")],
                partitions=2,
            ),
        ),
    )
    respx_mock.get(statement_url()).mock(return_value=httpx.Response(500, json={"message": "boom"}))

    with pytest.raises(TransientError):
        await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
