"""VCR contract test -- Databricks Statement Execution API behind the SQL tag read
(decision D6) (T8.5).

See ``conftest.py`` for what this directory pins and why every cassette is
hand-authored. Pins the fact that the Statement Execution API is asynchronous by
construction (every statement submitted with ``wait_timeout: "0s"``) and must be
polled: the ``SCHEMA_TAGS`` statement in this cassette genuinely goes
``PENDING`` -> ``RUNNING`` -> ``SUCCEEDED`` across two real polls before this test's
assertions can even run.
"""

from __future__ import annotations

import vcr

from qlabs_catalog_sync_sdk.config import ManualClock
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks import sql_tags

from .conftest import HOST


async def test_read_catalog_tags_pins_the_async_submit_and_poll_shape(
    databricks_contract_vcr: vcr.VCR,
) -> None:
    """``sql_tags.read_catalog_tags()`` called directly against a plain
    ``HttpEndpoint`` -- submits two statements (SCHEMA_TAGS, TABLE_TAGS), each
    asynchronous, each polled to a terminal state before any row is read."""
    clock = ManualClock()
    async with HttpEndpoint(HOST) as http:
        with databricks_contract_vcr.use_cassette("databricks_sql_tags_statement_execution.yaml"):
            index = await sql_tags.read_catalog_tags(
                http,
                sql_warehouse_id="contract-vcr-wh-1",
                catalog_name="prod",
                endpoint="databricks",
                schema_names=["sales"],
                clock=clock,
            )

    assert index is not None
    # Two real polls happened for the SCHEMA_TAGS statement (PENDING -> RUNNING is one
    # sleep, RUNNING -> SUCCEEDED is a second) plus one for TABLE_TAGS -- proving the
    # poll loop, not a single lucky response, is what produced this result.
    assert len(clock.sleep_calls) == 3

    schema_tags = {tag.key: tag.value for tag in index.for_schema("prod.sales")}
    assert schema_tags == {"team": "data-eng", "tier": "gold"}
    table_tags = {tag.key: tag.value for tag in index.for_table("prod.sales.orders")}
    assert table_tags == {"pii": "false"}
