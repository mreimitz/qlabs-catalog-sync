"""Paging defensively.

Two behaviors, each proven directly:

* A listing that spans multiple pages is fully drained — the request count matches the
  number of pages, not one.
* A listing whose pagination never terminates (every response carries a fresh
  ``next_page_token``, the pathological case Databricks' own docs call out — "a page may
  contain zero results while still providing a next_page_token") does not hang: the
  connector's own page-count cap raises a typed, retryable error after a bounded number
  of requests. Without that cap this test would hang instead of failing fast — which is
  exactly why it exists as a test, not just a claim.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, Watermark
from qlabs_catalog_sync_sdk.exceptions import TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.changes import list_changed

from .conftest import (
    CATALOGS_PATH,
    ENDPOINT,
    SCHEMAS_PATH,
    TABLES_PATH,
    catalog,
    default_schema_id,
    mock_infinite_list,
    mock_list,
    mock_single_page,
    schema,
)


async def test_multi_page_catalog_listing_is_fully_drained(respx_mock, http: HttpEndpoint) -> None:
    route = mock_list(
        respx_mock,
        CATALOGS_PATH,
        params={},
        items_key="catalogs",
        pages=[[catalog("main")], [catalog("other")]],
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
        SCHEMAS_PATH,
        params={"catalog_name": "other"},
        items_key="schemas",
        items=[],
    )
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[],
    )

    result = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
    )

    assert route.call_count == 2
    assert {c.ref.native_key for c in result.changes} == {default_schema_id("main", "sales")}
    assert result.has_more is False


async def test_runaway_pagination_raises_instead_of_hanging(respx_mock, http: HttpEndpoint) -> None:
    mock_single_page(
        respx_mock, CATALOGS_PATH, params={}, items_key="catalogs", items=[catalog("main")]
    )
    route = mock_infinite_list(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        item=schema("main", "sales"),
    )
    # Each yielded "sales" schema triggers its own (unrelated) membership listing —
    # mock it so the test isolates the schemas-listing runaway specifically.
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[],
    )

    with pytest.raises(TransientError, match="did not terminate pagination"):
        await list_changed(
            http,
            EntityType.DATA_PRODUCT,
            Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
            endpoint=ENDPOINT,
            max_pages_per_listing=3,
        )

    # Exactly the capped number of requests were made — not one, not unbounded.
    assert route.call_count == 3


async def test_runaway_pagination_on_tables_also_raises(respx_mock, http: HttpEndpoint) -> None:
    """The cap applies independently to every individual listing call, including the
    innermost one (tables-of-a-schema), not only the outermost (catalogs)."""
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
    route = mock_infinite_list(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        item={
            "full_name": "main.sales.orders",
            "name": "orders",
            "table_id": "tbl-orders",
            "metastore_id": "ms-1",
        },
    )

    with pytest.raises(TransientError, match="did not terminate pagination"):
        await list_changed(
            http,
            EntityType.DATASET,
            Watermark.initial(ENDPOINT, EntityType.DATASET),
            endpoint=ENDPOINT,
            max_pages_per_listing=2,
        )

    assert route.call_count == 2
