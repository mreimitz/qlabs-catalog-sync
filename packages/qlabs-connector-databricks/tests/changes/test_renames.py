"""A Databricks rename (``new_name``) must read as exactly one change to the renamed
object's own stream — never as a delete of the old identity plus a create of a new one.

This is the consequence of keying the diff on the *stable* object id (``schema_id``/
``table_id``) instead of ``full_name``: the id survives a rename, so the object is still
present under the same key in a fresh traversal (no ``ChangeKind.DELETED``), while its
``full_name``/``name`` fields did change, so its checksum differs (one
``ChangeKind.UPSERT``). Keyed on ``full_name`` instead, the old name would vanish from
the fresh listing and register as an orphaned delete under decision D4 for an object
that is, in truth, still there — exactly the bug this file exists to catch if it ever
comes back.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.contract import ChangeKind, EntityType, Watermark
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.changes import list_changed

from .conftest import (
    CATALOGS_PATH,
    ENDPOINT,
    SCHEMAS_PATH,
    TABLES_PATH,
    catalog,
    default_schema_id,
    mock_single_page,
    schema,
    table,
)


async def test_schema_rename_is_one_upsert_not_a_delete_plus_create(
    respx_mock, http: HttpEndpoint
) -> None:
    stable_id = "sch-stable-1"
    mock_single_page(
        respx_mock, CATALOGS_PATH, params={}, items_key="catalogs", items=[catalog("main")]
    )
    mock_single_page(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[schema("main", "sales", schema_id=stable_id)],
    )
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[],
    )

    first = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
    )
    assert len(first.changes) == 1
    assert first.changes[0].ref.native_key == stable_id
    assert first.changes[0].ref.secondary_keys["full_name"] == "main.sales"

    # Renamed: same schema_id, new name -> new full_name. The old name's schema listing
    # is gone; the *same id* now appears under the new one.
    mock_single_page(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[schema("main", "sales_v2", schema_id=stable_id)],
    )
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales_v2"},
        items_key="tables",
        items=[],
    )

    second = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        first.next_watermark,
        endpoint=ENDPOINT,
    )

    # Exactly one change: an UPSERT to the *same* native key, not a DELETE of the old
    # name plus a fresh UPSERT under a new one.
    assert len(second.changes) == 1
    assert second.changes[0].kind is ChangeKind.UPSERT
    assert second.changes[0].ref.native_key == stable_id
    assert second.changes[0].ref.secondary_keys["full_name"] == "main.sales_v2"
    assert not any(c.kind is ChangeKind.DELETED for c in second.changes)

    # A subsequent poll against the (now stable, renamed) state finds nothing further.
    third = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        second.next_watermark,
        endpoint=ENDPOINT,
    )
    assert third.is_empty


async def test_table_rename_is_one_upsert_not_a_delete_plus_create(
    respx_mock, http: HttpEndpoint
) -> None:
    stable_id = "tbl-stable-1"
    mock_single_page(
        respx_mock, CATALOGS_PATH, params={}, items_key="catalogs", items=[catalog("main")]
    )
    mock_single_page(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[schema("main", "sales")],
    )
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[table("main", "sales", "orders", table_id=stable_id)],
    )

    first = await list_changed(
        http,
        EntityType.DATASET,
        Watermark.initial(ENDPOINT, EntityType.DATASET),
        endpoint=ENDPOINT,
    )
    assert len(first.changes) == 1
    assert first.changes[0].ref.native_key == stable_id
    assert first.changes[0].ref.secondary_keys["full_name"] == "main.sales.orders"

    # Renamed within the same schema: same table_id, new name -> new full_name.
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[table("main", "sales", "orders_v2", table_id=stable_id)],
    )

    second = await list_changed(
        http,
        EntityType.DATASET,
        first.next_watermark,
        endpoint=ENDPOINT,
    )

    assert len(second.changes) == 1
    assert second.changes[0].kind is ChangeKind.UPSERT
    assert second.changes[0].ref.native_key == stable_id
    assert second.changes[0].ref.secondary_keys["full_name"] == "main.sales.orders_v2"
    assert not any(c.kind is ChangeKind.DELETED for c in second.changes)


async def test_renaming_a_member_table_does_not_falsely_change_its_schema(
    respx_mock, http: HttpEndpoint
) -> None:
    """The DATA_PRODUCT stream's membership fingerprint is keyed on the member tables'
    stable ids (see ``changes.py``), not their full names — so renaming a table inside a
    schema is invisible to the schema's own checksum. Adding/removing a member *is*
    visible (``test_checksum_fallback.py``); a bare rename of an existing member is not,
    on purpose."""
    stable_table_id = "tbl-member-1"
    unchanged_schema = schema("main", "sales")

    mock_single_page(
        respx_mock, CATALOGS_PATH, params={}, items_key="catalogs", items=[catalog("main")]
    )
    mock_single_page(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[unchanged_schema],
    )
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[table("main", "sales", "orders", table_id=stable_table_id)],
    )

    first = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
    )
    assert {c.ref.native_key for c in first.changes} == {default_schema_id("main", "sales")}

    # The member table is renamed; the schema payload itself is byte-identical.
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[table("main", "sales", "orders_v2", table_id=stable_table_id)],
    )

    second = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        first.next_watermark,
        endpoint=ENDPOINT,
    )

    assert second.is_empty
