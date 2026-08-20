"""``catalog_name`` is interpolated into the SQL text -- it cannot be a bind
parameter, since SQL has no placeholder syntax for an identifier in a ``FROM``
clause -- so it is validated before any HTTP call instead. ``schema_names``, which
*is* a value, goes through the Statement Execution API's own ``parameters`` array."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.sql_tags import IdentifierError, read_catalog_tags

from .conftest import ENDPOINT, SCHEMA_STMT_ID, TABLE_STMT_ID, statements_url, succeeded_response


@pytest.mark.parametrize(
    "bad_catalog_name",
    [
        "prod; DROP TABLE users",
        "prod`.`other_schema",
        "prod.sales",  # a dot is not a valid single-catalog identifier here
        "prod-sales",
        "prod sales",
        "",
    ],
)
async def test_an_unsafe_catalog_name_is_rejected_before_any_http_call(
    respx_mock: object, make_http: Callable[..., HttpEndpoint], bad_catalog_name: str
) -> None:
    catch_all = respx_mock.route(host="acme.cloud.databricks.com").mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json={})
    )
    http = make_http()

    with pytest.raises(IdentifierError):
        await read_catalog_tags(
            http, sql_warehouse_id="wh-1", catalog_name=bad_catalog_name, endpoint=ENDPOINT
        )

    assert catch_all.call_count == 0


async def test_a_safe_catalog_name_with_underscores_and_digits_is_accepted(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
        side_effect=[
            httpx.Response(200, json=succeeded_response(SCHEMA_STMT_ID, rows=[])),
            httpx.Response(200, json=succeeded_response(TABLE_STMT_ID, rows=[])),
        ]
    )
    http = make_http()

    index = await read_catalog_tags(
        http, sql_warehouse_id="wh-1", catalog_name="prod_catalog_2", endpoint=ENDPOINT
    )

    assert index is not None


async def test_schema_names_are_passed_as_bind_parameters_not_interpolated(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    """A schema name containing characters that would be unsafe if concatenated
    (a quote) must still work correctly, because it travels as a parameter *value*,
    never spliced into the SQL text."""
    route = respx_mock.post(statements_url()).mock(  # type: ignore[attr-defined]
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
        schema_names=["sales", "o'brien_schema"],
    )

    assert index is not None
    first_call_body = json.loads(route.calls[0].request.content)
    assert "WHERE schema_name IN (:s0, :s1)" in first_call_body["statement"]
    assert first_call_body["parameters"] == [
        {"name": "s0", "value": "sales", "type": "STRING"},
        {"name": "s1", "value": "o'brien_schema", "type": "STRING"},
    ]
    # The raw schema name text never appears spliced into the SQL string itself.
    assert "o'brien_schema" not in first_call_body["statement"]
