"""The Statement Execution API is asynchronous by nature: a statement starts
``PENDING``/``RUNNING`` and must be polled to a terminal state. Proven here with a
``ManualClock`` so no real time passes, and a bounded wait that behaves as specified
when the statement never finishes."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.config import ManualClock
from qlabs_catalog_sync_sdk.exceptions import TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.sql_tags import read_catalog_tags

from .conftest import (
    ENDPOINT,
    SCHEMA_STMT_ID,
    TABLE_STMT_ID,
    pending_response,
    running_response,
    schema_tag_row,
    statement_url,
    statements_url,
    succeeded_response,
)


async def test_pending_then_succeeded_is_polled_and_yields_rows(
    respx_mock: object, make_http: Callable[..., HttpEndpoint], manual_clock: ManualClock
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        side_effect=[
            httpx.Response(200, json=pending_response(SCHEMA_STMT_ID)),
            httpx.Response(200, json=succeeded_response(TABLE_STMT_ID, rows=[])),
        ]
    )
    poll_route = respx_mock.get(statement_url(SCHEMA_STMT_ID)).mock(  # type: ignore[attr-defined]
        side_effect=[
            httpx.Response(200, json=running_response(SCHEMA_STMT_ID)),
            httpx.Response(
                200,
                json=succeeded_response(
                    SCHEMA_STMT_ID,
                    rows=[schema_tag_row("prod", "sales", "domain", "commerce")],
                ),
            ),
        ]
    )
    http = make_http()

    index = await read_catalog_tags(
        http,
        sql_warehouse_id="wh-1",
        catalog_name="prod",
        endpoint=ENDPOINT,
        clock=manual_clock,
        poll_interval_seconds=5.0,
    )

    assert index is not None
    assert index.for_schema("prod.sales") != []
    # Polled: two GETs (PENDING -> RUNNING -> ... ends at the second, SUCCEEDED,
    # response), never assuming the first POST response carried the rows.
    assert poll_route.call_count == 2
    # No real time passed: the clock only ever "moved" through recorded sleep calls.
    assert manual_clock.sleep_calls == [5.0, 5.0]


async def test_a_bounded_wait_that_expires_raises_transient_error(
    respx_mock: object, make_http: Callable[..., HttpEndpoint], manual_clock: ManualClock
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=pending_response(SCHEMA_STMT_ID))
    )
    poll_route = respx_mock.get(statement_url(SCHEMA_STMT_ID)).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=pending_response(SCHEMA_STMT_ID))
    )
    http = make_http()

    with pytest.raises(TransientError):
        await read_catalog_tags(
            http,
            sql_warehouse_id="wh-1",
            catalog_name="prod",
            endpoint=ENDPOINT,
            clock=manual_clock,
            poll_interval_seconds=1.0,
            max_poll_attempts=3,
        )

    # Exactly max_poll_attempts polls were made before giving up -- not fewer (would
    # mean giving up too early) and not more (would mean the bound was not honored).
    assert poll_route.call_count == 3
    assert manual_clock.sleep_calls == [1.0, 1.0, 1.0]


async def test_a_statement_that_is_already_terminal_on_first_poll_is_not_polled(
    respx_mock: object, make_http: Callable[..., HttpEndpoint], manual_clock: ManualClock
) -> None:
    """If the initial (fully-async, ``wait_timeout: "0s"``) POST response is somehow
    already terminal, no GET poll happens at all -- the loop condition is checked
    before any wait."""
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        side_effect=[
            httpx.Response(200, json=succeeded_response(SCHEMA_STMT_ID, rows=[])),
            httpx.Response(200, json=succeeded_response(TABLE_STMT_ID, rows=[])),
        ]
    )
    http = make_http()

    index = await read_catalog_tags(
        http,
        sql_warehouse_id="wh-1",
        catalog_name="prod",
        endpoint=ENDPOINT,
        clock=manual_clock,
    )

    assert index is not None
    assert manual_clock.sleep_calls == []
