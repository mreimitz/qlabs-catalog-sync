"""A statement reaching a terminal state other than ``SUCCEEDED`` is not an HTTP-layer
problem (``HttpEndpoint`` already retried anything retryable at that layer) -- it is
the query itself failing, being canceled, or unexpectedly already closed. A
permission-shaped ``error_code`` maps to ``AuthError``; everything else maps to
``TransientError`` as the same honest "this attempt failed" fallback used throughout
this connector."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import AuthError, TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.sql_tags import read_catalog_tags

from .conftest import ENDPOINT, SCHEMA_STMT_ID, failed_response, statements_url


async def test_a_permission_denied_failure_raises_auth_error(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            200,
            json=failed_response(SCHEMA_STMT_ID, error_code="PERMISSION_DENIED"),
        )
    )
    http = make_http()

    with pytest.raises(AuthError):
        await read_catalog_tags(
            http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
        )


async def test_an_unauthenticated_failure_also_raises_auth_error(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            200,
            json=failed_response(SCHEMA_STMT_ID, error_code="UNAUTHENTICATED"),
        )
    )
    http = make_http()

    with pytest.raises(AuthError):
        await read_catalog_tags(
            http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
        )


async def test_a_generic_query_failure_raises_transient_error(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            200,
            json=failed_response(SCHEMA_STMT_ID, error_code="RESOURCE_LIMIT_EXCEEDED"),
        )
    )
    http = make_http()

    with pytest.raises(TransientError):
        await read_catalog_tags(
            http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
        )


async def test_a_canceled_statement_raises_transient_error(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=failed_response(SCHEMA_STMT_ID, state="CANCELED"))
    )
    http = make_http()

    with pytest.raises(TransientError):
        await read_catalog_tags(
            http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
        )


async def test_an_already_closed_statement_raises_transient_error(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=failed_response(SCHEMA_STMT_ID, state="CLOSED"))
    )
    http = make_http()

    with pytest.raises(TransientError):
        await read_catalog_tags(
            http, sql_warehouse_id="wh-1", catalog_name="prod", endpoint=ENDPOINT
        )
