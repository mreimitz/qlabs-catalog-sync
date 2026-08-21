"""Deletes, and the difference in provenance between the two surfaces that produce them.

* **Structural objects** (tables/views, schemas) get a real, server-side delete record:
  ``SNOWFLAKE.ACCOUNT_USAGE`` keeps dropped objects with a ``DELETED`` timestamp (RS-05
  section 1.4), which is the single biggest reason the change feed reads that surface
  instead of the fresher, per-database ``INFORMATION_SCHEMA`` -- the latter simply has no
  record that anything was ever dropped.
* **Listings** have no such record. ``SHOW LISTINGS`` returns what exists; a listing that
  has disappeared from a complete listing is inferred to be gone from the census instead.
  That is a weaker signal and is documented as such rather than presented as equivalent.

Decision D4 / the v1 guardrail is that the engine never deletes in Qlik and reports these
as orphans -- which is a reason for the connector to report them accurately, not a licence
to under-report. A ``ChangeRef`` for a vanished object still needs a complete, valid
``IdentityRef``, and for a listing there is no fresh row left to build one from, so the
census has to have carried its secondary keys forward; these tests check that, not just
the native key.

The reporting must also happen exactly **once**. The deliberate re-scan overlap means the
dropped row comes back on the next poll or two, and a connector that re-reported it every
time would turn one delete into a recurring orphan in the run report.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.contract import ChangeKind, EntityType, Watermark
from qlabs_connector_snowflake.read import StatementClient

from ..conftest import ENDPOINT, TENANT_ID, listing_row
from .conftest import (
    ALTERED,
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

DROPPED_AT = "2026-08-21T10:30:00+00:00"


async def test_a_dropped_table_is_reported_from_the_deleted_column(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1, NOW_2)
    set_tables(
        router,
        [table_row("ORDERS", table_id="1"), table_row("LINE_ITEMS", table_id="2")],
        [
            table_row("ORDERS", table_id="1"),
            table_row("LINE_ITEMS", table_id="2", deleted=DROPPED_AT),
        ],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    assert len(first.changes) == 2

    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert len(second.changes) == 1
    deleted = second.changes[0]
    assert deleted.ref.native_key == "SALES_DB.PUBLIC.LINE_ITEMS"
    assert deleted.ref.entity_type is EntityType.DATASET
    assert deleted.ref.tenant_id == TENANT_ID
    assert deleted.ref.secondary_keys == {"object_id": "2"}
    assert deleted.kind is ChangeKind.DELETED
    assert deleted.is_delete is True
    assert deleted.last_modified_at is not None
    assert deleted.last_modified_at.isoformat() == DROPPED_AT
    assert "SALES_DB.PUBLIC.LINE_ITEMS" not in cursor(second)["objects"]


async def test_a_dropped_table_is_reported_once_not_on_every_overlapping_poll(
    client: StatementClient, router: StatementRouter
) -> None:
    """The re-scan overlap deliberately re-reads the window the drop falls in. Once the
    object has left the census there is nothing left to report, so the third poll is
    quiet even though the same dropped row comes back."""
    set_now(router, NOW_1, NOW_2, NOW_3)
    set_tables(
        router,
        [table_row("ORDERS", table_id="1")],
        [table_row("ORDERS", table_id="1", deleted=DROPPED_AT)],
        [table_row("ORDERS", table_id="1", deleted=DROPPED_AT)],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)
    third = await poll(client, EntityType.DATASET, second.next_watermark)

    assert [change.kind for change in second.changes] == [ChangeKind.DELETED]
    assert third.is_empty


async def test_an_object_this_connector_never_reported_is_not_reported_as_deleted(
    client: StatementClient, router: StatementRouter
) -> None:
    """A table created and dropped entirely between two polls was never announced as
    present. Announcing its deletion would be noise about something the engine has no
    record of -- not honesty."""
    set_now(router, NOW_1, NOW_2)
    set_tables(
        router,
        [table_row("ORDERS", table_id="1")],
        [
            table_row("ORDERS", table_id="1"),
            table_row("SCRATCH", table_id="99", deleted=DROPPED_AT),
        ],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert second.is_empty


async def test_a_dropped_schema_is_reported_from_the_deleted_column(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1, NOW_2)
    set_schemata(
        router,
        [schema_row("PUBLIC", schema_id="10"), schema_row("STAGING", schema_id="11")],
        [
            schema_row("PUBLIC", schema_id="10"),
            schema_row("STAGING", schema_id="11", deleted=DROPPED_AT),
        ],
    )
    set_listings(router, [], [])

    first = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )
    assert len(first.changes) == 2

    second = await poll(client, EntityType.DATA_PRODUCT, first.next_watermark)

    assert len(second.changes) == 1
    deleted = second.changes[0]
    assert deleted.ref.native_key == "SALES_DB.STAGING"
    assert deleted.ref.entity_type is EntityType.DATA_PRODUCT
    assert deleted.kind is ChangeKind.DELETED


async def test_a_listing_that_vanished_is_reported_as_deleted_from_the_census(
    client: StatementClient, router: StatementRouter
) -> None:
    """No ``DELETED`` column exists on ``SHOW LISTINGS``, so a complete listing that no
    longer names a known listing is the only evidence available -- and the census is the
    only place its local name still exists to build a ref from."""
    set_now(router, NOW_1, NOW_2)
    set_schemata(router, [], [])
    set_listings(
        router,
        [
            listing_row(name="SALES_DAILY", global_name="GZTS1"),
            listing_row(name="SALES_WEEKLY", global_name="GZTS2"),
        ],
        [listing_row(name="SALES_DAILY", global_name="GZTS1")],
    )

    first = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )
    assert {change.ref.native_key for change in first.changes} == {"GZTS1", "GZTS2"}

    second = await poll(client, EntityType.DATA_PRODUCT, first.next_watermark)

    assert len(second.changes) == 1
    deleted = second.changes[0]
    assert deleted.ref.native_key == "GZTS2"
    assert deleted.kind is ChangeKind.DELETED
    # Rebuilt entirely from what the census remembered -- there is no fresh row.
    assert deleted.ref.secondary_keys == {"listing_name": "SALES_WEEKLY"}
    assert deleted.display_name == "SALES_WEEKLY"
    assert "GZTS2" not in cursor(second)["listings"]


async def test_an_unparseable_deleted_value_still_counts_as_a_delete(
    client: StatementClient, router: StatementRouter
) -> None:
    """The timestamp encoding is TENANT-UNVERIFIED. A drop marker this connector cannot
    parse still means the object is gone; treating it as "still alive" would silently lose
    a real delete, which is the one direction this feed must never fail in."""
    set_now(router, NOW_1, NOW_2)
    set_tables(
        router,
        [table_row("ORDERS", table_id="1", last_altered=ALTERED)],
        [table_row("ORDERS", table_id="1", deleted="dropped-at-some-point")],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert [change.kind for change in second.changes] == [ChangeKind.DELETED]
    assert second.changes[0].last_modified_at is None
