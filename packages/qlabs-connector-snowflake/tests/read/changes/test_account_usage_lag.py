"""The central correctness property of T6.3: ``ACCOUNT_USAGE`` lags, and no change may
fall between two polls because of it.

RS-05 section 1.4 documents ``SNOWFLAKE.ACCOUNT_USAGE`` as account-wide but latent --
"typically up to ~2 hours for many views", some worse. A poll that advanced its watermark
to "now" would silently lose every change that had not yet propagated into the view when
the query ran: those rows were not there to be seen, and the next poll, starting at that
"now", would never look back for them. Nothing would ever report the loss.

``read.py`` closes that two ways, and this file pins both:

1. the proposed high-water mark is ``snowflake_now - safety_margin``, never "now";
2. the next poll's lower bound is that mark minus ``rescan_overlap``, so the windows
   overlap rather than abut.

The consequence worth stating as a test in its own right is the *composition* of the two:
a change made at any instant the previous poll could not yet see is still inside the next
poll's window. ``test_a_change_inside_the_lag_window_is_still_in_range_next_poll`` asserts
exactly that, in the arithmetic rather than in prose.

The filtering itself happens inside Snowflake, so these tests assert on what this
connector *asks for* -- the bind values on the wire -- which is the only part of the
contract this side owns.
"""

from __future__ import annotations

from datetime import timedelta

from qlabs_catalog_sync_sdk.contract import EntityType, Watermark
from qlabs_connector_snowflake.read import (
    ACCOUNT_USAGE_LAG,
    DEFAULT_RESCAN_OVERLAP,
    DEFAULT_WATERMARK_SAFETY_MARGIN,
    StatementClient,
)

from ..conftest import ENDPOINT
from .conftest import (
    NOW_1,
    NOW_2,
    NOW_3,
    TABLES_SQL,
    StatementRouter,
    bodies_matching,
    high_water,
    instant,
    lower_bound_of,
    poll,
    set_now,
    set_tables,
    table_row,
)


def test_the_margin_defaults_to_the_documented_lag() -> None:
    """The margin is not a number picked to make a test pass: it is the lag RS-05
    documents, named once so a tenant that measures a different one has a single place to
    change (and so this file can assert against the same constant the code uses)."""
    assert DEFAULT_WATERMARK_SAFETY_MARGIN == ACCOUNT_USAGE_LAG
    assert timedelta(hours=2) <= ACCOUNT_USAGE_LAG


async def test_the_proposed_watermark_is_held_back_by_the_margin(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1)
    set_tables(router, [table_row("ORDERS")])

    result = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))

    assert high_water(result) == instant(NOW_1) - DEFAULT_WATERMARK_SAFETY_MARGIN


async def test_the_next_poll_re_scans_an_overlap_below_the_high_water_mark(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1, NOW_2)
    set_tables(router, [table_row("ORDERS")], [table_row("ORDERS")])

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    await poll(client, EntityType.DATASET, first.next_watermark)

    second_scan = bodies_matching(router, TABLES_SQL)[1]
    assert lower_bound_of(second_scan) == high_water(first) - DEFAULT_RESCAN_OVERLAP
    assert "TO_TIMESTAMP_TZ(?)" in second_scan["statement"]
    assert "LAST_ALTERED IS NULL" in second_scan["statement"]


async def test_a_change_inside_the_lag_window_is_still_in_range_next_poll(
    client: StatementClient, router: StatementRouter
) -> None:
    """The composition of margin and overlap, stated as the guarantee it exists for.

    A change made one hour before the first poll's "now" is inside the window
    ``ACCOUNT_USAGE`` may not have propagated yet, so the first poll is allowed to miss it.
    The second poll must still cover it -- and does, because the first poll refused to
    claim coverage past ``now - margin``.
    """
    changed_at = instant(NOW_1) - timedelta(hours=1)
    set_now(router, NOW_1, NOW_2)
    set_tables(router, [], [table_row("ORDERS", last_altered=changed_at.isoformat())])

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    assert first.is_empty  # not propagated yet -- correctly invisible

    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert lower_bound_of(bodies_matching(router, TABLES_SQL)[1]) < changed_at
    assert {change.ref.native_key for change in second.changes} == {"SALES_DB.PUBLIC.ORDERS"}


async def test_successive_windows_overlap_rather_than_abut(
    client: StatementClient, router: StatementRouter
) -> None:
    """Three polls: each lower bound sits strictly below the previous poll's, by exactly
    the overlap. Abutting windows would leave a boundary instant for a change to fall
    through; overlapping ones cannot."""
    set_now(router, NOW_1, NOW_2, NOW_3)
    set_tables(router, [], [], [])

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)
    third = await poll(client, EntityType.DATASET, second.next_watermark)

    scans = bodies_matching(router, TABLES_SQL)
    assert "bindings" not in scans[0]
    assert lower_bound_of(scans[1]) == high_water(first) - DEFAULT_RESCAN_OVERLAP
    assert lower_bound_of(scans[2]) == high_water(second) - DEFAULT_RESCAN_OVERLAP
    assert lower_bound_of(scans[1]) < high_water(first)
    assert lower_bound_of(scans[2]) < high_water(second)
    assert high_water(third) > high_water(second) > high_water(first)


async def test_the_margin_and_overlap_are_overridable(
    client: StatementClient, router: StatementRouter
) -> None:
    """A deployment that measures its own ``ACCOUNT_USAGE`` latency can widen or narrow
    both. They are keyword arguments with module-constant defaults rather than config
    fields only because T6.3 may not edit ``auth.py``'s ``SnowflakeConfig``."""
    set_now(router, NOW_1, NOW_2)
    set_tables(router, [], [])
    margin = timedelta(hours=6)
    overlap = timedelta(hours=1)

    first = await poll(
        client,
        EntityType.DATASET,
        Watermark.initial(ENDPOINT, EntityType.DATASET),
        safety_margin=margin,
        rescan_overlap=overlap,
    )
    assert high_water(first) == instant(NOW_1) - margin

    await poll(
        client,
        EntityType.DATASET,
        first.next_watermark,
        safety_margin=margin,
        rescan_overlap=overlap,
    )
    assert lower_bound_of(bodies_matching(router, TABLES_SQL)[1]) == high_water(first) - overlap


async def test_now_comes_from_snowflake_not_from_this_host(
    client: StatementClient, router: StatementRouter
) -> None:
    """The margin is measured against the clock that stamped ``LAST_ALTERED``, so host
    clock skew cannot quietly eat into it."""
    set_now(router, NOW_1)
    set_tables(router, [])

    result = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))

    assert result.next_watermark.observed_at == instant(NOW_1)
    assert high_water(result) == instant(NOW_1) - DEFAULT_WATERMARK_SAFETY_MARGIN


async def test_an_unreadable_snowflake_clock_falls_back_instead_of_failing_the_poll(
    client: StatementClient, router: StatementRouter, manual_clock: object
) -> None:
    """The timestamp encoding is TENANT-UNVERIFIED, so a value this connector cannot parse
    must not cost the whole cycle -- it falls back to the injected clock, which is at worst
    seconds away from Snowflake's."""
    set_now(router, "not-a-timestamp")
    set_tables(router, [table_row("ORDERS")])

    result = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))

    assert len(result.changes) == 1
    # ManualClock's epoch, held back by the margin -- proof the fallback, not NOW_1, was used.
    assert high_water(result) == instant("2024-01-01T00:00:00+00:00") - ACCOUNT_USAGE_LAG
