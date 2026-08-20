"""A ``SUCCEEDED`` result can span multiple ``INLINE`` chunks (large catalogs, many
tag rows). This module follows ``result.next_chunk_internal_link`` until it is absent,
bounded defensively so a malformed/looping continuation link cannot hang the read."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.sql_tags import read_catalog_tags

from .conftest import (
    BASE_URL,
    ENDPOINT,
    SCHEMA_STMT_ID,
    TABLE_STMT_ID,
    chunk_response,
    schema_tag_row,
    statements_url,
    succeeded_response,
)

_CHUNK_URL = f"{BASE_URL}/api/2.0/sql/statements/{SCHEMA_STMT_ID}/result/chunks/1"


async def test_a_multi_chunk_result_is_fully_collected(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        side_effect=[
            httpx.Response(
                200,
                json=succeeded_response(
                    SCHEMA_STMT_ID,
                    rows=[schema_tag_row("prod", "sales", "domain", "commerce")],
                    next_chunk_internal_link="/api/2.0/sql/statements/"
                    f"{SCHEMA_STMT_ID}/result/chunks/1",
                ),
            ),
            httpx.Response(200, json=succeeded_response(TABLE_STMT_ID, rows=[])),
        ]
    )
    chunk_route = respx_mock.get(_CHUNK_URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            200, json=chunk_response(rows=[schema_tag_row("prod", "sales", "pii", "false")])
        )
    )
    http = make_http()

    index = await read_catalog_tags(
        http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
    )

    assert index is not None
    tags = {tag.key for tag in index.for_schema("prod.sales")}
    assert tags == {"domain", "pii"}
    assert chunk_route.call_count == 1


async def test_a_runaway_chunk_chain_is_bounded_defensively(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    """A continuation link that never terminates must not hang the connector forever
    -- mirrors ``read.py``'s ``_paginate_defensively`` bound on REST pagination."""
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            200,
            json=succeeded_response(
                SCHEMA_STMT_ID,
                rows=[],
                next_chunk_internal_link=_CHUNK_URL.removeprefix(BASE_URL),
            ),
        )
    )
    respx_mock.get(_CHUNK_URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            200,
            json=chunk_response(
                rows=[], next_chunk_internal_link=_CHUNK_URL.removeprefix(BASE_URL)
            ),
        )
    )
    http = make_http()

    with pytest.raises(TransientError):
        await read_catalog_tags(
            http,
            sql_warehouse_id="wh-1",
            catalog_name="prod",
            endpoint=ENDPOINT,
            max_result_chunks=5,
        )
