"""HTTP failures map onto the SDK's typed exceptions -- never a per-connector hierarchy --
and a retryable failure is retried by the shared ``HttpEndpoint`` before this module ever
sees it."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import (
    AuthError,
    CapabilityError,
    NotFound,
    TransientError,
)
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_snowflake.read import StatementClient, read_dataset

from .conftest import (
    COLUMNS_COLUMNS,
    TABLES_COLUMNS,
    TAG_REFERENCE_COLUMNS,
    StatementRouter,
    column_row,
    dataset_ref,
    result_response,
    statements_url,
    table_row,
)


@pytest.mark.parametrize("status", [401, 403])
async def test_an_authentication_failure_raises_auth_error(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    status: int,
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(status, json={"message": "not authorized", "code": "390100"})
    )

    with pytest.raises(AuthError):
        await read_dataset(make_client(http), dataset_ref())


async def test_a_404_raises_not_found(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(404, json={"message": "not found"})
    )

    with pytest.raises(NotFound):
        await read_dataset(make_client(http), dataset_ref())


async def test_a_failed_statement_raises_capability_error(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    """Snowflake reports a statement that could not be compiled or executed as a 4xx with
    an error body; the honest reading is "this request was rejected as invalid", not
    "retry it unchanged"."""
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            422,
            json={
                "code": "002003",
                "message": "SQL compilation error: Object 'X' does not exist.",
                "sqlState": "42S02",
            },
        )
    )

    with pytest.raises(CapabilityError, match="SQL compilation error"):
        await read_dataset(make_client(http), dataset_ref())


async def test_a_429_is_retried_by_the_shared_endpoint_then_succeeds(
    respx_mock: object,
    make_http: Callable[..., HttpEndpoint],
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.on(
        "INFORMATION_SCHEMA.TABLES",
        httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"}),
        httpx.Response(200, json=result_response(TABLES_COLUMNS, [table_row()])),
    )
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [column_row()])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]
    http = make_http()

    dataset = await read_dataset(make_client(http), dataset_ref())

    assert dataset.name == "ORDERS"
    # The retry happened inside HttpEndpoint -- this module never retried anything.
    assert len(router.statements_matching("INFORMATION_SCHEMA.TABLES")) == 2


async def test_a_persistent_5xx_exhausts_retries_and_raises_transient_error(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(503, json={"message": "service unavailable"})
    )

    with pytest.raises(TransientError):
        await read_dataset(make_client(http), dataset_ref())


async def test_a_transport_failure_raises_transient_error(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        side_effect=httpx.ConnectError("connection reset")
    )

    with pytest.raises(TransientError):
        await read_dataset(make_client(http), dataset_ref())


async def test_a_timeout_raises_transient_error(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        side_effect=httpx.ReadTimeout("timed out")
    )

    with pytest.raises(TransientError):
        await read_dataset(make_client(http), dataset_ref())


async def test_a_failure_carries_the_endpoint_and_entity_type_for_the_engine(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    """The engine reacts to structure, not message text -- so the structure must be there."""
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(401, json={"message": "expired token"})
    )

    with pytest.raises(AuthError) as caught:
        await read_dataset(make_client(http), dataset_ref())

    assert caught.value.endpoint == "snowflake"
    assert caught.value.entity_type == "dataset"
    assert caught.value.retryable is False
