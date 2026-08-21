"""With a warehouse configured: schema and table tags are read into neutral
:class:`Tag` values, and a key-only tag (SQL ``NULL``) is kept distinct from a tag
explicitly set to an empty string."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import Tag
from qlabs_connector_databricks.sql_tags import read_catalog_tags

from .conftest import (
    ENDPOINT,
    SCHEMA_STMT_ID,
    TABLE_STMT_ID,
    schema_tag_row,
    statements_url,
    succeeded_response,
    table_tag_row,
)


def _mock_two_statements(
    respx_mock: object, *, schema_rows: list[list[object]], table_rows: list[list[object]]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        side_effect=[
            httpx.Response(200, json=succeeded_response(SCHEMA_STMT_ID, rows=schema_rows)),
            httpx.Response(200, json=succeeded_response(TABLE_STMT_ID, rows=table_rows)),
        ]
    )


async def test_schema_tags_are_read_into_neutral_tags(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    _mock_two_statements(
        respx_mock,
        schema_rows=[
            schema_tag_row("prod", "sales", "domain", "commerce"),
            schema_tag_row("prod", "sales", "pii", "false"),
        ],
        table_rows=[],
    )
    http = make_http()

    index = await read_catalog_tags(
        http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
    )

    assert index is not None
    tags = index.for_schema("prod.sales")
    assert Tag(key="domain", value="commerce") in tags
    assert Tag(key="pii", value="false") in tags
    assert len(tags) == 2


async def test_table_tags_are_read_into_neutral_tags(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    _mock_two_statements(
        respx_mock,
        schema_rows=[],
        table_rows=[
            table_tag_row("prod", "sales", "orders", "domain", "commerce"),
            table_tag_row("prod", "sales", "orders", "tier", "gold"),
        ],
    )
    http = make_http()

    index = await read_catalog_tags(
        http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
    )

    assert index is not None
    tags = index.for_table("prod.sales.orders")
    assert Tag(key="domain", value="commerce") in tags
    assert Tag(key="tier", value="gold") in tags
    assert len(tags) == 2


async def test_a_key_only_tag_differs_from_an_empty_string_value(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    """SQL ``NULL`` (key-only tag) must decode to ``Tag.value is None``; SQL ``''``
    (explicitly empty) must decode to ``Tag.value == ""`` -- never collapsed."""
    _mock_two_statements(
        respx_mock,
        schema_rows=[
            schema_tag_row("prod", "sales", "no_value", None),
            schema_tag_row("prod", "sales", "empty_value", ""),
        ],
        table_rows=[],
    )
    http = make_http()

    index = await read_catalog_tags(
        http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
    )

    assert index is not None
    tags = {tag.key: tag for tag in index.for_schema("prod.sales")}
    assert tags["no_value"].value is None
    assert tags["empty_value"].value == ""
    assert tags["no_value"] != tags["empty_value"]


async def test_tag_keys_are_never_case_folded(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    _mock_two_statements(
        respx_mock,
        schema_rows=[
            schema_tag_row("prod", "sales", "Domain", "commerce"),
            schema_tag_row("prod", "sales", "domain", "other"),
        ],
        table_rows=[],
    )
    http = make_http()

    index = await read_catalog_tags(
        http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
    )

    assert index is not None
    keys = {tag.key for tag in index.for_schema("prod.sales")}
    assert keys == {"Domain", "domain"}


async def test_an_object_with_no_tags_reads_as_an_empty_list_not_none(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    """Distinct from the whole-index "unavailable" case (test_unavailable.py): once a
    :class:`CatalogTagIndex` exists, a specific object simply having no tags is a real,
    positive answer -- ``[]`` -- never confused with the index itself being absent."""
    _mock_two_statements(respx_mock, schema_rows=[], table_rows=[])
    http = make_http()

    index = await read_catalog_tags(
        http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
    )

    assert index is not None
    assert index.for_schema("prod.unknown_schema") == []
    assert index.for_table("prod.unknown_schema.unknown_table") == []
