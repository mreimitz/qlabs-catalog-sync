"""Where the timestamp cannot be trusted, the checksum decides.

``LAST_ALTERED`` is what *bounds* the query -- it has to be, because scanning the whole
account on every poll is not a viable change feed and because the safety margin is defined
in terms of it. But it is never what *decides* a candidate. Every row the query returns is
compared against the checksum the census recorded for it, so:

* a content change that did not move ``LAST_ALTERED`` is still caught, as long as the row
  came back at all (through the re-scan overlap, or through the ``LAST_ALTERED IS NULL``
  disjunct below);
* an object kind whose ``LAST_ALTERED`` an account never populates is not silently
  invisible: those rows bypass the timestamp filter entirely and are decided purely by
  checksum, which is the only signal available for them;
* a **listing** is decided by checksum alone from the start -- ``SHOW LISTINGS`` exposes no
  modified-time to filter on, so there is nothing else it could use.

The filtering itself happens inside Snowflake and is not what these tests exercise: the
mock returns the row, and the question under test is what this connector *does* with a row
whose timestamp says nothing useful.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.contract import ChangeKind, EntityType, Watermark
from qlabs_connector_snowflake.read import StatementClient

from ..conftest import ENDPOINT, listing_row
from .conftest import (
    ALTERED,
    NOW_1,
    NOW_2,
    TABLES_SQL,
    StatementRouter,
    bodies_matching,
    kinds_by_key,
    poll,
    set_listings,
    set_now,
    set_schemata,
    set_tables,
    table_row,
)


async def test_a_content_change_with_an_unmoved_timestamp_is_still_caught(
    client: StatementClient, router: StatementRouter
) -> None:
    """The scenario a pure timestamp-threshold design would miss: the comment changed and
    ``LAST_ALTERED`` is byte-identical to the previous poll's."""
    set_now(router, NOW_1, NOW_2)
    set_tables(
        router,
        [table_row("ORDERS", table_id="1", comment="raw orders", last_altered=ALTERED)],
        [table_row("ORDERS", table_id="1", comment="curated orders", last_altered=ALTERED)],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert kinds_by_key(second) == {"SALES_DB.PUBLIC.ORDERS": "updated"}


async def test_rows_without_a_timestamp_bypass_the_time_filter_entirely(
    client: StatementClient, router: StatementRouter
) -> None:
    """``LAST_ALTERED IS NULL`` is a disjunct of the incremental ``WHERE`` on purpose: a
    row a timestamp predicate would exclude forever must still be able to be seen to
    change. Its arrival in the result set is then decided by checksum like any other."""
    set_now(router, NOW_1, NOW_2)
    set_tables(
        router,
        [table_row("EVENTS", table_id="7", comment="v1", last_altered=None)],
        [table_row("EVENTS", table_id="7", comment="v2", last_altered=None)],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert kinds_by_key(second) == {"SALES_DB.PUBLIC.EVENTS": "updated"}
    assert first.changes[0].last_modified_at is None


async def test_the_incremental_statement_carries_the_null_timestamp_disjunct(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1, NOW_2)
    set_tables(router, [], [])

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    await poll(client, EntityType.DATASET, first.next_watermark)

    incremental = bodies_matching(router, TABLES_SQL)[1]["statement"]
    assert "DELETED >= TO_TIMESTAMP_TZ(?)" in incremental
    assert "LAST_ALTERED >= TO_TIMESTAMP_TZ(?)" in incremental
    assert "LAST_ALTERED IS NULL" in incremental


async def test_an_owner_change_alone_moves_the_checksum(
    client: StatementClient, router: StatementRouter
) -> None:
    """Ownership is best-effort metadata (project guardrail), but it is metadata the sync
    carries, so a change to it has to register as a candidate."""
    set_now(router, NOW_1, NOW_2)
    set_tables(
        router,
        [table_row("ORDERS", table_id="1", owner="SALES_ENGINEER")],
        [table_row("ORDERS", table_id="1", owner="PLATFORM_ENGINEER")],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert kinds_by_key(second) == {"SALES_DB.PUBLIC.ORDERS": "updated"}


async def test_a_listing_is_decided_by_checksum_because_it_has_no_timestamp_to_filter_on(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1, NOW_2)
    set_schemata(router, [], [])
    set_listings(
        router,
        [listing_row(global_name="GZTS1", subtitle="Daily sales by region")],
        [listing_row(global_name="GZTS1", subtitle="Daily sales by region and channel")],
    )

    first = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )
    second = await poll(client, EntityType.DATA_PRODUCT, first.next_watermark)

    assert [change.kind for change in second.changes] == [ChangeKind.UPDATED]
    assert second.changes[0].ref.native_key == "GZTS1"
