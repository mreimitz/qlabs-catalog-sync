"""A native key present in the prior snapshot but absent from a fresh traversal is
reported as ``ChangeKind.DELETED`` (decision D4: the engine reports it as an orphan).

This falls out of the same checksum-snapshot diff at no extra API cost — it is not
required by T4.3's DoD, but it is nearly free given the design and directly serves a
binding decision, so it is covered here as a deliberate (documented) extra, kept
separate from the DoD-focused test files.
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
    TENANT_ID,
    catalog,
    mock_single_page,
    schema,
    table,
)


async def test_vanished_table_is_reported_as_deleted(respx_mock, http: HttpEndpoint) -> None:
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
        items=[table("main", "sales", "orders"), table("main", "sales", "line_items")],
    )

    first = await list_changed(
        http,
        EntityType.DATASET,
        Watermark.initial(ENDPOINT, EntityType.DATASET),
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
    )
    assert {c.ref.native_key for c in first.changes} == {
        "main.sales.orders",
        "main.sales.line_items",
    }

    # "line_items" is dropped from the table.
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[table("main", "sales", "orders")],
    )

    second = await list_changed(
        http, EntityType.DATASET, first.next_watermark, endpoint=ENDPOINT, tenant_id=TENANT_ID
    )

    assert len(second.changes) == 1
    deleted = second.changes[0]
    assert deleted.ref.native_key == "main.sales.line_items"
    assert deleted.kind is ChangeKind.DELETED
    assert deleted.is_delete is True


async def test_vanished_schema_is_reported_as_deleted(respx_mock, http: HttpEndpoint) -> None:
    mock_single_page(
        respx_mock, CATALOGS_PATH, params={}, items_key="catalogs", items=[catalog("main")]
    )
    mock_single_page(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[schema("main", "sales"), schema("main", "hr")],
    )
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[],
    )
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "hr"},
        items_key="tables",
        items=[],
    )

    first = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
    )
    assert {c.ref.native_key for c in first.changes} == {"main.sales", "main.hr"}

    # "hr" schema is dropped entirely.
    mock_single_page(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[schema("main", "sales")],
    )

    second = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        first.next_watermark,
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
    )

    assert len(second.changes) == 1
    assert second.changes[0].ref.native_key == "main.hr"
    assert second.changes[0].kind is ChangeKind.DELETED
