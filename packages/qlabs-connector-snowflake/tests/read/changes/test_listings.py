"""The listing shape of ``DATA_PRODUCT``, which shares a stream with schemas.

``manifest.py`` gives ``DATA_PRODUCT`` two native shapes and two mutually exclusive
identity schemes, so one poll of that stream has to scan two entirely different surfaces
and return refs that :func:`~qlabs_connector_snowflake.read.data_product_shape` will later
route correctly. That routing is the subtle part: a schema ref is recognised by its
two-part native key, a listing ref by its ``listing_name``/``listing_global_name``
secondary key. A listing whose global name happened to contain exactly one dot would be
misread as a schema if the change feed ever emitted it without one of those keys, so the
"no local name" case gets its own test rather than being assumed away.

Creates and updates for listings are covered here; deletes are in ``test_deletes.py``
(they come from the census, not from any delete record Snowflake offers), renames in
``test_renames.py``, and the permission degradation in ``test_errors.py``.
"""

from __future__ import annotations

import httpx

from qlabs_catalog_sync_sdk.contract import EntityType, Watermark
from qlabs_connector_snowflake.read import DataProductShape, StatementClient, data_product_shape

from ..conftest import ENDPOINT, listing_row
from .conftest import (
    LISTINGS_SQL,
    NOW_1,
    NOW_2,
    StatementRouter,
    kinds_by_key,
    poll,
    schema_row,
    set_listings,
    set_now,
    set_schemata,
)


async def test_a_listing_added_after_the_baseline_is_a_create_not_an_upsert(
    client: StatementClient, router: StatementRouter
) -> None:
    """Once the census exists, ``CREATED`` is a claim this connector is entitled to make:
    ``SHOW LISTINGS`` is always complete, so a listing that was not there last time is
    genuinely new."""
    set_now(router, NOW_1, NOW_2)
    set_schemata(router, [], [])
    set_listings(
        router,
        [listing_row(name="SALES_DAILY", global_name="GZTS1")],
        [
            listing_row(name="SALES_DAILY", global_name="GZTS1"),
            listing_row(name="SALES_WEEKLY", global_name="GZTS2"),
        ],
    )

    first = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )
    assert kinds_by_key(first) == {"GZTS1": "upsert"}

    second = await poll(client, EntityType.DATA_PRODUCT, first.next_watermark)

    assert kinds_by_key(second) == {"GZTS2": "created"}


async def test_a_listing_ref_routes_back_to_the_listing_shape(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1)
    set_schemata(router, [schema_row("PUBLIC")])
    set_listings(router, [listing_row(name="SALES_DAILY", global_name="GZTS1")])

    result = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )

    shapes = {change.ref.native_key: data_product_shape(change.ref) for change in result.changes}
    assert shapes == {
        "SALES_DB.PUBLIC": DataProductShape.SCHEMA,
        "GZTS1": DataProductShape.LISTING,
    }


async def test_a_listing_with_no_local_name_still_routes_as_a_listing(
    client: StatementClient, router: StatementRouter
) -> None:
    """Without a local name there is no ``listing_name`` key to carry, so the ref falls
    back to ``listing_global_name`` -- which is what keeps ``data_product_shape`` from
    mistaking a dotted global name for a schema's ``DATABASE.SCHEMA``."""
    set_now(router, NOW_1)
    set_schemata(router, [])
    router.on(
        LISTINGS_SQL,
        httpx.Response(
            200,
            json={
                "resultSetMetaData": {"rowType": [{"name": "global_name"}, {"name": "title"}]},
                "data": [["ORG.LISTING", "Daily sales"]],
            },
        ),
    )

    result = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )

    assert len(result.changes) == 1
    ref = result.changes[0].ref
    assert ref.native_key == "ORG.LISTING"
    assert ref.secondary_keys == {"listing_global_name": "ORG.LISTING"}
    assert data_product_shape(ref) is DataProductShape.LISTING


async def test_a_listings_publish_state_change_is_a_candidate(
    client: StatementClient, router: StatementRouter
) -> None:
    """RS-05 section 4.4: "a sync that manages listings must model draft vs published
    state, not just field values". Unpublishing moves ``state``, which moves the row's
    checksum, which makes it a candidate the engine can act on."""
    set_now(router, NOW_1, NOW_2)
    set_schemata(router, [], [])
    set_listings(
        router,
        [listing_row(global_name="GZTS1", state="PUBLISHED")],
        [listing_row(global_name="GZTS1", state="UNPUBLISHED")],
    )

    first = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )
    second = await poll(client, EntityType.DATA_PRODUCT, first.next_watermark)

    assert kinds_by_key(second) == {"GZTS1": "updated"}


async def test_schemas_and_listings_land_on_one_result_without_colliding(
    client: StatementClient, router: StatementRouter
) -> None:
    """One stream, two surfaces, one census each -- and the SDK's own
    ``ListChangedResult`` validator confirming every ref belongs to the stream."""
    set_now(router, NOW_1)
    set_schemata(
        router, [schema_row("PUBLIC", schema_id="10"), schema_row("STAGING", schema_id="11")]
    )
    set_listings(
        router,
        [
            listing_row(name="SALES_DAILY", global_name="GZTS1"),
            listing_row(name="SALES_WEEKLY", global_name="GZTS2"),
        ],
    )

    result = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )

    assert len(result.changes) == 4
    assert all(change.ref.entity_type is EntityType.DATA_PRODUCT for change in result.changes)
    assert result.is_exhausted is True
