"""The SQL REST API client: the synchronous 200, the asynchronous 202 + poll, paged
partitions, and the request body/parameters RS-05 section 3.8 specifies."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.config import ManualClock
from qlabs_catalog_sync_sdk.exceptions import ConnectorError, TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_snowflake.read import (
    DEFAULT_STATEMENT_TIMEOUT_SECONDS,
    StatementClient,
    StatementResult,
)

from .conftest import (
    STATEMENT_HANDLE,
    accepted_response,
    partition_response,
    result_response,
    statement_url,
    statements_url,
)


async def test_a_synchronous_200_result_is_returned_directly(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    route = respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=result_response(("A", "B"), [["1", "x"], ["2", "y"]]))
    )
    client = make_client(http)

    result = await client.execute("SELECT A, B FROM T")

    assert result.columns == ("A", "B")
    assert result.rows == (("1", "x"), ("2", "y"))
    assert result.mappings() == [{"A": "1", "B": "x"}, {"A": "2", "B": "y"}]
    assert route.call_count == 1


async def test_an_empty_result_set_yields_no_rows_and_no_first_mapping(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=result_response(("A",), []))
    )

    result = await make_client(http).execute("SELECT A FROM T")

    assert result.rows == ()
    assert result.first_mapping() is None


async def test_an_asynchronous_202_is_polled_until_it_completes(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    manual_clock: ManualClock,
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(202, json=accepted_response())
    )
    poll = respx_mock.get(statement_url()).mock(  # type: ignore[attr-defined]
        side_effect=[
            httpx.Response(202, json=accepted_response()),
            httpx.Response(200, json=result_response(("A",), [["done"]])),
        ]
    )

    result = await make_client(http).execute("SELECT A FROM T")

    assert result.mappings() == [{"A": "done"}]
    # Polled twice: the first GET was still 202. The POST response is never assumed to
    # carry the rows.
    assert poll.call_count == 2
    # No real time passed; the waits back off exponentially from the poll interval.
    assert manual_clock.sleep_calls == [1.0, 2.0]


async def test_the_poll_backoff_is_capped(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    manual_clock: ManualClock,
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(202, json=accepted_response())
    )
    respx_mock.get(statement_url()).mock(  # type: ignore[attr-defined]
        side_effect=[httpx.Response(202, json=accepted_response())] * 4
        + [httpx.Response(200, json=result_response(("A",), []))]
    )

    await make_client(http, max_poll_interval_seconds=4.0).execute("SELECT A FROM T")

    assert manual_clock.sleep_calls == [1.0, 2.0, 4.0, 4.0, 4.0]


async def test_a_statement_that_never_completes_raises_transient_error(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    manual_clock: ManualClock,
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(202, json=accepted_response())
    )
    poll = respx_mock.get(statement_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(202, json=accepted_response())
    )

    with pytest.raises(TransientError, match="poll attempts"):
        await make_client(http, max_poll_attempts=3).execute("SELECT A FROM T")

    # Exactly the bound: not fewer (giving up early), not more (bound not honored).
    assert poll.call_count == 3
    assert len(manual_clock.sleep_calls) == 3


async def test_a_202_with_no_statement_handle_fails_loudly(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(202, json={"message": "in progress"})
    )

    with pytest.raises(ConnectorError, match="statementHandle"):
        await make_client(http).execute("SELECT A FROM T")


async def test_a_paged_result_set_is_fully_collected(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=result_response(("A",), [["p0"]], partitions=3))
    )
    partitions = respx_mock.get(statement_url()).mock(  # type: ignore[attr-defined]
        side_effect=[
            httpx.Response(200, json=partition_response([["p1"]])),
            httpx.Response(200, json=partition_response([["p2"]])),
        ]
    )

    result = await make_client(http).execute("SELECT A FROM T")

    assert [row["A"] for row in result.mappings()] == ["p0", "p1", "p2"]
    assert partitions.call_count == 2
    # Each continuation names the partition it wants, not a cursor.
    assert partitions.calls[0].request.url.params["partition"] == "1"
    assert partitions.calls[1].request.url.params["partition"] == "2"


async def test_a_single_partition_result_makes_no_extra_request(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=result_response(("A",), [["only"]]))
    )
    partitions = respx_mock.get(statement_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=partition_response([]))
    )

    await make_client(http).execute("SELECT A FROM T")

    assert partitions.call_count == 0


async def test_a_runaway_partition_count_is_bounded_defensively(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=result_response(("A",), [], partitions=50))
    )

    with pytest.raises(TransientError, match="partitions"):
        await make_client(http, max_partitions=5).execute("SELECT A FROM T")


async def test_the_request_body_carries_the_statement_timeout_and_request_id(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    route = respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=result_response(("A",), []))
    )

    await make_client(http).execute("SELECT A FROM T")

    request = route.calls[0].request
    body = json.loads(request.content)
    assert body["statement"] == "SELECT A FROM T"
    assert body["timeout"] == DEFAULT_STATEMENT_TIMEOUT_SECONDS
    # RS-05 4.4: requestId makes a resubmission idempotent server-side.
    assert request.url.params["requestId"] == "req-0001"


async def test_the_configured_role_and_warehouse_are_sent_when_present(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    route = respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=result_response(("A",), []))
    )

    await make_client(http, role="QLABS_SYNC", warehouse="QLABS_WH").execute(
        "SELECT A FROM T", database="SALES_DB", schema="PUBLIC"
    )

    body = json.loads(route.calls[0].request.content)
    assert body["role"] == "QLABS_SYNC"
    assert body["warehouse"] == "QLABS_WH"
    assert body["database"] == "SALES_DB"
    assert body["schema"] == "PUBLIC"


async def test_an_unconfigured_role_and_warehouse_are_omitted_not_nulled(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    """Metadata queries are served by the cloud-services layer, so a deployment with no
    warehouse still reads -- sending an explicit ``null`` would be a different request."""
    route = respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=result_response(("A",), []))
    )

    await make_client(http).execute("SELECT A FROM T")

    body = json.loads(route.calls[0].request.content)
    assert "role" not in body
    assert "warehouse" not in body
    assert "database" not in body


async def test_bind_values_travel_in_the_bindings_object(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    route = respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=result_response(("A",), []))
    )

    await make_client(http).execute("SELECT A FROM T WHERE B = ?", bind_values=("x",))

    body = json.loads(route.calls[0].request.content)
    assert body["bindings"] == {"1": {"type": "TEXT", "value": "x"}}


async def test_no_bindings_key_is_sent_when_there_are_no_values(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    route = respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=result_response(("A",), []))
    )

    await make_client(http).execute("SHOW LISTINGS")

    assert "bindings" not in json.loads(route.calls[0].request.content)


async def test_a_non_json_body_fails_with_a_typed_error(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, text="<html>gateway</html>")
    )

    with pytest.raises(ConnectorError, match="non-JSON"):
        await make_client(http).execute("SELECT A FROM T")


async def test_a_json_array_body_fails_with_a_typed_error(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=[1, 2, 3])
    )

    with pytest.raises(ConnectorError, match="response shape"):
        await make_client(http).execute("SELECT A FROM T")


def test_a_row_narrower_than_the_column_descriptors_fails_loudly() -> None:
    """A short row means the assumed response shape is wrong; an IndexError or a silently
    truncated row would both hide that."""
    result = StatementResult(columns=("A", "B", "C"), rows=(("1", "2"),))

    with pytest.raises(ConnectorError, match="column"):
        result.mappings()


def test_a_row_wider_than_the_column_descriptors_is_truncated_not_rejected() -> None:
    """Extra trailing values are a forward-compatible server addition, not a broken
    contract -- the declared columns are still all present and correct."""
    result = StatementResult(columns=("A",), rows=(("1", "extra"),))

    assert result.mappings() == [{"A": "1"}]


async def test_the_statement_handle_is_carried_on_the_result(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=result_response(("A",), []))
    )

    result = await make_client(http).execute("SELECT A FROM T")

    assert result.statement_handle == STATEMENT_HANDLE
