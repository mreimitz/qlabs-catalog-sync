"""HTTP-layer failures against the Statement Execution API map onto the SDK's typed
exceptions, and a retryable failure is actually retried by the shared ``HttpEndpoint``
before this module ever sees it -- mirrors ``tests/changes/test_errors.py``'s
equivalent coverage for the REST listing endpoints."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import AuthError, TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.sql_tags import read_catalog_tags

from .conftest import ENDPOINT, SCHEMA_STMT_ID, TABLE_STMT_ID, statements_url, succeeded_response


async def test_401_raises_auth_error(respx_mock: object, http: HttpEndpoint) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(401, json={"message": "invalid token"})
    )

    with pytest.raises(AuthError):
        await read_catalog_tags(
            http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
        )


async def test_403_also_raises_auth_error(respx_mock: object, http: HttpEndpoint) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )

    with pytest.raises(AuthError):
        await read_catalog_tags(
            http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
        )


async def test_429_is_retried_then_succeeds(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    route = respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"}),
            httpx.Response(200, json=succeeded_response(SCHEMA_STMT_ID, rows=[])),
            httpx.Response(200, json=succeeded_response(TABLE_STMT_ID, rows=[])),
        ]
    )
    http = make_http()

    index = await read_catalog_tags(
        http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
    )

    assert index is not None
    # First call to the SCHEMA_TAGS statement got a 429 and was retried by
    # HttpEndpoint itself (never by this module); then the TABLE_TAGS statement
    # succeeded on its first try. Three calls total, not a raised error.
    assert route.call_count == 3


async def test_a_persistent_5xx_exhausts_retries_and_raises_transient_error(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(503, json={"message": "unavailable"})
    )

    with pytest.raises(TransientError):
        await read_catalog_tags(
            http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
        )


async def test_a_transport_failure_raises_transient_error(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        side_effect=httpx.ConnectError("connection reset")
    )

    with pytest.raises(TransientError):
        await read_catalog_tags(
            http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
        )
