"""From an ``initial`` watermark, every schema/table currently visible is a candidate.

No prior snapshot exists yet, so :func:`changes._decode_snapshot` returns an empty map
and every observed native key is, by definition, a change from "unknown" to "known" —
this is what makes ``ChangeKind.UPSERT`` (never ``CREATED``) the honest default: we
cannot tell whether the object is brand new or simply never synced before.
"""

from __future__ import annotations

import json

from qlabs_catalog_sync_sdk.contract import ChangeKind, EntityType, Watermark, WatermarkKind
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.changes import list_changed

from .conftest import (
    CATALOGS_PATH,
    ENDPOINT,
    METASTORE_ID,
    SCHEMAS_PATH,
    TABLES_PATH,
    catalog,
    default_schema_id,
    default_table_id,
    mock_single_page,
    schema,
    table,
)


async def test_initial_watermark_returns_every_schema(respx_mock, http: HttpEndpoint) -> None:
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

    result = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
    )

    assert {c.ref.native_key for c in result.changes} == {
        default_schema_id("main", "sales"),
        default_schema_id("main", "hr"),
    }
    assert {c.ref.secondary_keys["full_name"] for c in result.changes} == {"main.sales", "main.hr"}
    assert all(c.kind is ChangeKind.UPSERT for c in result.changes)
    assert all(c.ref.entity_type is EntityType.DATA_PRODUCT for c in result.changes)
    assert all(c.ref.tenant_id == METASTORE_ID for c in result.changes)
    assert result.has_more is False
    assert result.next_watermark.kind is WatermarkKind.CURSOR
    assert result.next_watermark.endpoint == ENDPOINT
    assert result.next_watermark.entity_type is EntityType.DATA_PRODUCT

    snapshot = json.loads(result.next_watermark.cursor or "{}")
    assert set(snapshot["objects"]) == {
        default_schema_id("main", "sales"),
        default_schema_id("main", "hr"),
    }


async def test_initial_watermark_returns_every_table(respx_mock, http: HttpEndpoint) -> None:
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

    result = await list_changed(
        http,
        EntityType.DATASET,
        Watermark.initial(ENDPOINT, EntityType.DATASET),
        endpoint=ENDPOINT,
    )

    assert {c.ref.native_key for c in result.changes} == {
        default_table_id("main", "sales", "orders"),
        default_table_id("main", "sales", "line_items"),
    }
    assert {c.ref.secondary_keys["full_name"] for c in result.changes} == {
        "main.sales.orders",
        "main.sales.line_items",
    }
    assert all(c.kind is ChangeKind.UPSERT for c in result.changes)
    assert all(c.ref.entity_type is EntityType.DATASET for c in result.changes)
    assert all(c.ref.tenant_id == METASTORE_ID for c in result.changes)
    assert result.next_watermark.entity_type is EntityType.DATASET
    assert result.has_more is False


async def test_empty_workspace_returns_no_candidates(respx_mock, http: HttpEndpoint) -> None:
    mock_single_page(respx_mock, CATALOGS_PATH, params={}, items_key="catalogs", items=[])

    result = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
    )

    assert result.is_empty
    assert result.has_more is False
