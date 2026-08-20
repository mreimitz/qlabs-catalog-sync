"""The DoD, proved rather than claimed: a re-run with the returned ``next_watermark``
against *unchanged* data yields no candidates.

Each test polls once (an initial scan), then polls again with the watermark the first
call returned, against byte-identical mock responses. Because the fallback compares
exact checksums rather than a timestamp threshold, this holds regardless of what
``updated_at`` says on the second pass — proven directly in
``test_checksum_fallback.py`` by leaving ``updated_at`` untouched on a real content
change and observing the candidate anyway; here the mirror case (nothing at all
changed) must therefore produce nothing.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.contract import EntityType, Watermark
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.changes import list_changed

from .conftest import (
    CATALOGS_PATH,
    ENDPOINT,
    SCHEMAS_PATH,
    TABLES_PATH,
    catalog,
    mock_single_page,
    schema,
    table,
)


async def test_rerun_on_unchanged_schemas_yields_nothing(respx_mock, http: HttpEndpoint) -> None:
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
        items=[table("main", "sales", "orders")],
    )

    first = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
    )
    assert len(first.changes) == 1  # sanity: the first pass did find the schema

    second = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        first.next_watermark,
        endpoint=ENDPOINT,
    )

    assert second.is_empty
    assert second.has_more is False
    # The snapshot content is unchanged too — re-running again would still find nothing.
    assert second.next_watermark.cursor == first.next_watermark.cursor


async def test_rerun_on_unchanged_tables_yields_nothing(respx_mock, http: HttpEndpoint) -> None:
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
    )
    assert len(first.changes) == 2

    second = await list_changed(
        http,
        EntityType.DATASET,
        first.next_watermark,
        endpoint=ENDPOINT,
    )

    assert second.is_empty
    assert second.has_more is False


async def test_third_run_after_a_real_change_is_quiet_again(
    respx_mock, http: HttpEndpoint
) -> None:
    """Three polls: quiet, changed, quiet again — the snapshot tracks forward, it does
    not just remember the very first observation forever."""
    mock_single_page(
        respx_mock, CATALOGS_PATH, params={}, items_key="catalogs", items=[catalog("main")]
    )
    mock_single_page(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[schema("main", "sales", comment="v1")],
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

    # Second poll: the comment actually changed.
    mock_single_page(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[schema("main", "sales", comment="v2")],
    )
    second = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        first.next_watermark,
        endpoint=ENDPOINT,
    )
    assert len(second.changes) == 1

    # Third poll: unchanged relative to the *second* snapshot.
    mock_single_page(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[schema("main", "sales", comment="v2")],
    )
    third = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        second.next_watermark,
        endpoint=ENDPOINT,
    )
    assert third.is_empty
