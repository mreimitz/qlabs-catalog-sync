"""From an ``initial`` watermark, every object currently visible is a candidate.

There is no census and no high-water mark yet, so the scan runs unbounded
(``WHERE DELETED IS NULL``, no bind values) and every row it returns is a change from
"unknown" to "known". That is what makes ``ChangeKind.UPSERT`` -- never ``CREATED`` -- the
honest kind here: the connector cannot tell a brand-new object apart from one that has
simply never been synced before. Only once the census exists does ``CREATED`` become a
claim this connector is entitled to make (``test_idempotency.py`` covers that side).

The initial scan being *complete* is load-bearing, not incidental: every later poll is
incremental, so the census it maintains is only a true account census if the very first
scan saw everything. Delete detection, rename detection and the ``CREATED``-vs-``UPSERT``
distinction all rest on that.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.contract import ChangeKind, EntityType, Watermark, WatermarkKind

from ..conftest import ENDPOINT, TENANT_ID, listing_row
from .conftest import (
    NOW_1,
    SCHEMATA_SQL,
    TABLES_SQL,
    StatementClient,
    StatementRouter,
    cursor,
    high_water,
    instant,
    poll,
    schema_row,
    set_listings,
    set_now,
    set_schemata,
    set_tables,
    table_row,
)


async def test_initial_watermark_returns_every_table(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1)
    set_tables(router, [table_row("ORDERS", table_id="1"), table_row("CUSTOMERS", table_id="2")])

    result = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))

    assert {change.ref.native_key for change in result.changes} == {
        "SALES_DB.PUBLIC.ORDERS",
        "SALES_DB.PUBLIC.CUSTOMERS",
    }
    assert all(change.kind is ChangeKind.UPSERT for change in result.changes)
    assert all(change.ref.entity_type is EntityType.DATASET for change in result.changes)
    assert all(change.ref.tenant_id == TENANT_ID for change in result.changes)
    # `object_id` is a declared DATASET identity key (manifest.py), so it travels.
    assert {change.ref.secondary_keys["object_id"] for change in result.changes} == {"1", "2"}
    assert result.has_more is False
    assert result.is_exhausted is True
    assert result.next_watermark.kind is WatermarkKind.CURSOR
    assert result.next_watermark.endpoint == ENDPOINT
    assert result.next_watermark.entity_type is EntityType.DATASET
    assert set(cursor(result)["objects"]) == {
        "SALES_DB.PUBLIC.ORDERS",
        "SALES_DB.PUBLIC.CUSTOMERS",
    }


async def test_initial_scan_is_unbounded_and_excludes_dropped_objects(
    client: StatementClient, router: StatementRouter
) -> None:
    """No prior watermark means no lower bound to scan from -- and no reason to look at
    dropped objects, which were gone before this connector ever reported them present."""
    set_now(router, NOW_1)
    set_tables(router, [table_row("ORDERS")])

    await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))

    body = router.body_for(TABLES_SQL)
    assert "DELETED IS NULL" in body["statement"]
    assert "TO_TIMESTAMP_TZ" not in body["statement"]
    assert "bindings" not in body


async def test_initial_watermark_returns_schemas_and_listings_on_one_stream(
    client: StatementClient, router: StatementRouter
) -> None:
    """``DATA_PRODUCT`` carries both native shapes (manifest.py), so one poll of that
    stream scans ACCOUNT_USAGE.SCHEMATA *and* SHOW LISTINGS."""
    set_now(router, NOW_1)
    set_schemata(
        router, [schema_row("PUBLIC", schema_id="10"), schema_row("STAGING", schema_id="11")]
    )
    set_listings(router, [listing_row(name="SALES_DAILY", global_name="GZTS1")])

    result = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )

    assert {change.ref.native_key for change in result.changes} == {
        "SALES_DB.PUBLIC",
        "SALES_DB.STAGING",
        "GZTS1",
    }
    assert all(change.kind is ChangeKind.UPSERT for change in result.changes)
    assert all(change.ref.entity_type is EntityType.DATA_PRODUCT for change in result.changes)
    # No id key is declared for DATA_PRODUCT, so SCHEMA_ID must not leak into a ref.
    schemas = [c for c in result.changes if c.ref.native_key.startswith("SALES_DB.")]
    assert all(change.ref.secondary_keys == {} for change in schemas)
    # A listing's ref carries the local name, exactly as build_listing_data_product does.
    listing = next(c for c in result.changes if c.ref.native_key == "GZTS1")
    assert listing.ref.secondary_keys == {"listing_name": "SALES_DAILY"}
    assert listing.display_name == "SALES_DAILY"

    payload = cursor(result)
    assert set(payload["objects"]) == {"SALES_DB.PUBLIC", "SALES_DB.STAGING"}
    assert set(payload["listings"]) == {"GZTS1"}


async def test_the_initial_scan_still_proposes_a_held_back_watermark(
    client: StatementClient, router: StatementRouter
) -> None:
    """Even with nothing to report, the first poll must leave behind a watermark that is
    already held back -- otherwise the very first incremental poll would start from an
    instant ACCOUNT_USAGE had not caught up to."""
    set_now(router, NOW_1)
    set_tables(router, [])

    result = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))

    assert result.is_empty
    assert result.has_more is False
    assert high_water(result) < instant(NOW_1)


async def test_an_empty_account_returns_no_candidates(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1)
    set_schemata(router, [])
    set_listings(router, [])

    result = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )

    assert result.is_empty
    assert result.is_exhausted is True
    assert cursor(result)["objects"] == {}
    assert cursor(result)["listings"] == {}
    assert SCHEMATA_SQL in router.body_for(SCHEMATA_SQL)["statement"]
