"""Renames, and the three different honest answers Snowflake's identity model forces.

T6.4 settled that a Snowflake object's ``native_key`` is its fully-qualified name, because
no ``INFORMATION_SCHEMA`` view the read path touches exposes an object id at all. A rename
therefore *moves the native key*, which is the opposite of the Databricks connector's
situation (Unity Catalog hands back a stable ``table_id``/``schema_id`` on every row, so
its renames are one plain checksum change under an unmoved key).

``ACCOUNT_USAGE`` does expose ids (RS-05 section 4.3: "Objects also have internal numeric
IDs in some ``ACCOUNT_USAGE`` views... useful for detecting renames"), so the change feed
can *detect* the rename regardless. What differs is how it may be *reported*, and that is
decided entirely by what ``manifest.py`` declares as an identity key:

* ``DATASET`` declares ``object_id``, so the id travels in the ref and the engine can
  follow the identity: one ``UPDATED``, and crucially **no** ``DELETED`` for the old name.
  Emitting one would orphan a live object at the target -- the exact false-orphan bug the
  Databricks connector keys on stable ids to avoid.
* ``DATA_PRODUCT`` declares only ``fully_qualified_name``/``listing_global_name``, so
  ``SCHEMA_ID`` may not travel in a ref. Reporting only the new name would leave the old
  one to rot silently in the engine's IdentityMap; the honest answer is to report both
  halves -- ``CREATED`` of the new FQN and ``DELETED`` of the old -- with the pairing
  logged. The id is still what made the pair detectable rather than half-missed.
* A **listing** renames for free: its local name may change while ``global_name`` -- the
  native key -- does not, so it is one plain ``UPDATED``.

Where a tenant returns no id for an object kind, detection degrades to "new FQN reported,
old FQN left in the census"; the last test in this file pins that degradation so it stays
a known behavior rather than a surprise.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.contract import ChangeKind, EntityType, Watermark
from qlabs_connector_snowflake.read import StatementClient

from ..conftest import ENDPOINT, listing_row
from .conftest import (
    NOW_1,
    NOW_2,
    NOW_3,
    StatementRouter,
    cursor,
    kinds_by_key,
    poll,
    schema_row,
    set_listings,
    set_now,
    set_schemata,
    set_tables,
    table_row,
)


async def test_a_table_rename_is_one_update_not_a_delete_plus_create(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1, NOW_2, NOW_3)
    set_tables(
        router,
        [table_row("ORDERS", table_id="1001")],
        [table_row("ORDERS_V2", table_id="1001")],
        [table_row("ORDERS_V2", table_id="1001")],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    assert [change.ref.native_key for change in first.changes] == ["SALES_DB.PUBLIC.ORDERS"]

    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert len(second.changes) == 1
    renamed = second.changes[0]
    assert renamed.kind is ChangeKind.UPDATED
    assert renamed.ref.native_key == "SALES_DB.PUBLIC.ORDERS_V2"
    assert renamed.ref.secondary_keys == {"object_id": "1001"}
    assert not any(change.is_delete for change in second.changes)
    # The old identity is gone from the census, so it can never later look like a delete.
    assert set(cursor(second)["objects"]) == {"SALES_DB.PUBLIC.ORDERS_V2"}

    third = await poll(client, EntityType.DATASET, second.next_watermark)
    assert third.is_empty


async def test_a_table_moved_to_another_schema_is_also_one_update(
    client: StatementClient, router: StatementRouter
) -> None:
    """A rename in Snowflake can move an object across schemas, changing two of the three
    FQN parts. The id does not care, so neither does the diff."""
    set_now(router, NOW_1, NOW_2)
    set_tables(
        router,
        [table_row("ORDERS", schema="PUBLIC", table_id="1001")],
        [table_row("ORDERS", schema="CURATED", table_id="1001")],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert kinds_by_key(second) == {"SALES_DB.CURATED.ORDERS": "updated"}


async def test_a_schema_rename_is_reported_as_both_halves(
    client: StatementClient, router: StatementRouter
) -> None:
    """No id key is declared for ``DATA_PRODUCT``, so the pair is the honest report: the
    new name is genuinely a new identity to the engine, and the old one genuinely no
    longer exists."""
    set_now(router, NOW_1, NOW_2)
    set_schemata(
        router,
        [schema_row("PUBLIC", schema_id="2001")],
        [schema_row("CURATED", schema_id="2001")],
    )
    set_listings(router, [], [])

    first = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )
    assert [change.ref.native_key for change in first.changes] == ["SALES_DB.PUBLIC"]

    second = await poll(client, EntityType.DATA_PRODUCT, first.next_watermark)

    assert kinds_by_key(second) == {
        "SALES_DB.CURATED": "created",
        "SALES_DB.PUBLIC": "deleted",
    }
    # SCHEMA_ID detected the pairing but must not leak into a ref -- manifest.py does not
    # declare it as a DATA_PRODUCT identity key.
    assert all(change.ref.secondary_keys == {} for change in second.changes)
    assert set(cursor(second)["objects"]) == {"SALES_DB.CURATED"}


async def test_a_listing_rename_is_one_update_because_the_global_name_holds(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1, NOW_2)
    set_schemata(router, [], [])
    set_listings(
        router,
        [listing_row(name="SALES_DAILY", global_name="GZTS1")],
        [listing_row(name="SALES_DAILY_V2", global_name="GZTS1")],
    )

    first = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )
    second = await poll(client, EntityType.DATA_PRODUCT, first.next_watermark)

    assert kinds_by_key(second) == {"GZTS1": "updated"}
    assert second.changes[0].ref.secondary_keys == {"listing_name": "SALES_DAILY_V2"}
    assert not any(change.is_delete for change in second.changes)
    assert first.changes[0].ref.native_key == "GZTS1"


async def test_a_reused_name_is_not_mistaken_for_a_rename(
    client: StatementClient, router: StatementRouter
) -> None:
    """If the old name is occupied by a *different* live object -- rename ORDERS to
    ORDERS_V2, then create a new ORDERS -- the old name has not vanished and must not be
    reported as deleted or folded into the rename."""
    set_now(router, NOW_1, NOW_2)
    set_tables(
        router,
        [table_row("ORDERS", table_id="1001")],
        [table_row("ORDERS_V2", table_id="1001"), table_row("ORDERS", table_id="2002")],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert kinds_by_key(second) == {
        "SALES_DB.PUBLIC.ORDERS_V2": "created",
        "SALES_DB.PUBLIC.ORDERS": "updated",
    }
    assert not any(change.is_delete for change in second.changes)


async def test_without_an_object_id_a_rename_degrades_to_a_bare_create(
    client: StatementClient, router: StatementRouter
) -> None:
    """The documented degradation: an object kind whose ``ACCOUNT_USAGE`` view returns no
    id cannot be correlated at all, so the new name is reported and the old one is left in
    the census. Pinned here so it stays a known limitation rather than a surprise."""
    set_now(router, NOW_1, NOW_2)
    set_tables(
        router,
        [table_row("ORDERS", table_id=None)],
        [table_row("ORDERS_V2", table_id=None)],
    )

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    assert first.changes[0].ref.secondary_keys == {}

    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert kinds_by_key(second) == {"SALES_DB.PUBLIC.ORDERS_V2": "created"}
    assert set(cursor(second)["objects"]) == {
        "SALES_DB.PUBLIC.ORDERS",
        "SALES_DB.PUBLIC.ORDERS_V2",
    }
