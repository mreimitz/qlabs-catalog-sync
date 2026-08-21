"""The DoD, proved rather than claimed: polling again with the returned watermark against
unchanged data yields nothing, and polling twice from the *same* watermark yields the same
answer both times.

Two distinct properties, both of which the engine relies on:

* **Restart safety.** The engine commits ``next_watermark`` in the same transaction as the
  work it derived from ``changes``. If a crash loses that transaction it re-polls from the
  *old* watermark, and must get the same candidates back -- otherwise a crash could quietly
  change what gets synced.
* **Overlap is free, not merely harmless.** The re-scan overlap deliberately re-reads rows
  the previous poll already saw. Those rows come back through the timestamp filter, so
  something has to stop them being re-reported: the per-object checksum in the census does,
  and this file is where that is visible. The contract only requires re-delivery to be
  harmless; making it silent is strictly better and costs nothing.

Because the decision is exact-equality between two checksums Snowflake's own responses
produced, none of this depends on this host's clock -- there is no inequality anywhere in
the *decision*, only in the query's window.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.contract import EntityType, Watermark
from qlabs_connector_snowflake.read import StatementClient

from ..conftest import ENDPOINT, listing_row
from .conftest import (
    NOW_1,
    NOW_2,
    NOW_3,
    StatementRouter,
    cursor,
    poll,
    schema_row,
    set_listings,
    set_now,
    set_schemata,
    set_tables,
    table_row,
)


async def test_a_rerun_on_unchanged_tables_yields_nothing(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1, NOW_2)
    set_tables(
        router,
        [table_row("ORDERS", table_id="1"), table_row("CUSTOMERS", table_id="2")],
        [table_row("ORDERS", table_id="1"), table_row("CUSTOMERS", table_id="2")],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    assert len(first.changes) == 2

    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert second.is_empty
    assert second.has_more is False
    assert cursor(second)["objects"] == cursor(first)["objects"]


async def test_a_rerun_on_unchanged_schemas_and_listings_yields_nothing(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1, NOW_2)
    set_schemata(router, [schema_row("PUBLIC")], [schema_row("PUBLIC")])
    set_listings(
        router,
        [listing_row(name="SALES_DAILY", global_name="GZTS1")],
        [listing_row(name="SALES_DAILY", global_name="GZTS1")],
    )

    first = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )
    assert len(first.changes) == 2

    second = await poll(client, EntityType.DATA_PRODUCT, first.next_watermark)

    assert second.is_empty


async def test_polling_twice_from_the_same_watermark_gives_the_same_answer(
    client: StatementClient, router: StatementRouter
) -> None:
    """The crash-and-retry case: the engine lost the transaction that would have committed
    the new watermark, so it polls again from the old one."""
    set_now(router, NOW_1, NOW_2)
    set_tables(router, [table_row("ORDERS", table_id="1")])

    since = Watermark.initial(ENDPOINT, EntityType.DATASET)
    first = await poll(client, EntityType.DATASET, since)
    replay = await poll(client, EntityType.DATASET, since)

    assert [(c.ref, c.kind) for c in replay.changes] == [(c.ref, c.kind) for c in first.changes]
    assert cursor(replay)["objects"] == cursor(first)["objects"]


async def test_a_row_re_read_through_the_overlap_is_not_re_reported(
    client: StatementClient, router: StatementRouter
) -> None:
    """The rows the overlap re-reads are exactly the ones the previous poll already
    reported. The checksum census is what keeps that from turning into a duplicate storm --
    without it, every poll would re-announce everything changed in the last few hours."""
    set_now(router, NOW_1, NOW_2, NOW_3)
    row = table_row("ORDERS", table_id="1", last_altered="2026-08-21T08:00:00+00:00")
    set_tables(router, [row], [row], [row])

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)
    third = await poll(client, EntityType.DATASET, second.next_watermark)

    assert len(first.changes) == 1
    assert second.is_empty
    assert third.is_empty


async def test_the_census_tracks_forward_rather_than_remembering_only_the_first_poll(
    client: StatementClient, router: StatementRouter
) -> None:
    """Quiet, changed, quiet again -- the third poll must compare against the *second*
    observation, not the first."""
    set_now(router, NOW_1, NOW_2, NOW_3)
    set_tables(
        router,
        [table_row("ORDERS", table_id="1", comment="v1")],
        [table_row("ORDERS", table_id="1", comment="v2")],
        [table_row("ORDERS", table_id="1", comment="v2")],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)
    third = await poll(client, EntityType.DATASET, second.next_watermark)

    assert len(first.changes) == 1
    assert len(second.changes) == 1
    assert third.is_empty


async def test_an_unreadable_cursor_falls_back_to_a_full_scan_instead_of_failing(
    client: StatementClient, router: StatementRouter
) -> None:
    """A state-store artifact this module cannot parse must cost one wider scan, not the
    whole cycle. Everything is re-offered as ``UPSERT`` -- the honest kind when the
    baseline is unknown -- and the engine finds most of it unchanged."""
    set_now(router, NOW_1)
    set_tables(router, [table_row("ORDERS", table_id="1")])
    corrupt = Watermark.from_cursor(ENDPOINT, EntityType.DATASET, "{not json at all")

    result = await poll(client, EntityType.DATASET, corrupt)

    assert [change.kind.value for change in result.changes] == ["upsert"]
    assert "bindings" not in router.body_for("ACCOUNT_USAGE.TABLES")
