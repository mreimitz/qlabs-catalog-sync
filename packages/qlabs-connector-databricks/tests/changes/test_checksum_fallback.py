"""When ``updated_at`` misses a change, the checksum comparison still catches it.

Two concrete scenarios named in the task, each proven as a test:

* A table's content changed (its comment) but ``updated_at`` was left exactly as it was
  on the previous poll — plausible if the source system's clock/audit-field update is
  inconsistent, or simply to prove the fallback does not silently trust ``updated_at``.
* A table is added under a schema. The *schema's own* fields (comment/owner/properties/
  ``updated_at``) never change — UC does not bump a schema's timestamp when a child
  table is created — yet the schema is the neutral data product (decision D1) whose
  dataset membership just changed, and that must be visible to the engine.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.contract import ChangeKind, EntityType, Watermark
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.changes import list_changed

from .conftest import (
    CATALOGS_PATH,
    DEFAULT_TS,
    ENDPOINT,
    SCHEMAS_PATH,
    TABLES_PATH,
    catalog,
    default_schema_id,
    default_table_id,
    mock_single_page,
    schema,
    table,
)


async def test_content_change_with_unmoved_updated_at_is_still_caught(
    respx_mock, http: HttpEndpoint
) -> None:
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
        items=[table("main", "sales", "orders", comment="raw orders", updated_at=DEFAULT_TS)],
    )

    first = await list_changed(
        http,
        EntityType.DATASET,
        Watermark.initial(ENDPOINT, EntityType.DATASET),
        endpoint=ENDPOINT,
    )
    expected = {default_table_id("main", "sales", "orders")}
    assert {c.ref.native_key for c in first.changes} == expected

    # The comment changed but `updated_at` is byte-identical to the prior poll — the
    # scenario a pure updated_at-threshold design would miss.
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[table("main", "sales", "orders", comment="curated orders", updated_at=DEFAULT_TS)],
    )

    second = await list_changed(http, EntityType.DATASET, first.next_watermark, endpoint=ENDPOINT)

    assert {c.ref.native_key for c in second.changes} == expected
    assert second.changes[0].kind is ChangeKind.UPSERT


async def test_new_table_under_a_schema_is_caught_as_a_data_product_change(
    respx_mock, http: HttpEndpoint
) -> None:
    """A table's arrival never touches the schema's own comment/owner/properties/
    updated_at — only the membership fingerprint changes."""
    unchanged_schema = schema("main", "sales", comment="sales domain", updated_at=DEFAULT_TS)

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
        items=[],
    )

    first = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
    )
    assert {c.ref.native_key for c in first.changes} == {default_schema_id("main", "sales")}

    # A table now exists under "sales". The schema payload itself (registered above) is
    # untouched — same comment, same updated_at — only membership changed.
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[table("main", "sales", "orders")],
    )

    second = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        first.next_watermark,
        endpoint=ENDPOINT,
    )

    assert {c.ref.native_key for c in second.changes} == {default_schema_id("main", "sales")}
    assert second.changes[0].kind is ChangeKind.UPSERT


async def test_removing_a_table_from_a_schema_is_also_a_data_product_change(
    respx_mock, http: HttpEndpoint
) -> None:
    mock_single_page(
        respx_mock, CATALOGS_PATH, params={}, items_key="catalogs", items=[catalog("main")]
    )
    mock_single_page(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[schema("main", "sales", updated_at=DEFAULT_TS)],
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
    assert {c.ref.native_key for c in first.changes} == {default_schema_id("main", "sales")}

    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[],
    )

    second = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        first.next_watermark,
        endpoint=ENDPOINT,
    )

    assert {c.ref.native_key for c in second.changes} == {default_schema_id("main", "sales")}
