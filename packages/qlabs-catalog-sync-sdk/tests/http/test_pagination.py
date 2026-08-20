"""Pagination helpers: cursor style (Qlik's ``links.next.href``) and offset/token
style (Databricks' ``next_page_token`` / ``page_token``). Both must terminate,
both are async iterators, and neither fetches a page it will not yield.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from qlabs_catalog_sync_sdk.http import HttpEndpoint


async def test_cursor_pagination_walks_three_pages_and_stops(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    base_url: str,
) -> None:
    # Each page lives at its own path (rather than the same path with a different
    # query string) so route matching can't be ambiguous: respx matches a route
    # registered without an explicit `params=` filter against *any* query string,
    # so giving pages 2 and 3 their own path is what makes this test unambiguous
    # regardless of respx route registration order.
    page1 = respx_mock.get(f"{base_url}/things").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": 1}, {"id": 2}],
                "links": {"next": {"href": f"{base_url}/things/page2"}},
            },
        )
    )
    page2 = respx_mock.get(f"{base_url}/things/page2").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": 3}],
                "links": {"next": {"href": f"{base_url}/things/page3"}},
            },
        )
    )
    page3 = respx_mock.get(f"{base_url}/things/page3").mock(
        return_value=httpx.Response(200, json={"data": [{"id": 4}], "links": {}})
    )
    endpoint = make_endpoint()

    items = [item async for item in endpoint.paginate_cursor("GET", "/things")]

    assert items == [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
    assert page1.call_count == 1
    assert page2.call_count == 1
    assert page3.call_count == 1


async def test_cursor_pagination_never_fetches_a_page_it_will_not_yield(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    base_url: str,
) -> None:
    page1 = respx_mock.get(f"{base_url}/things").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": 1}, {"id": 2}],
                "links": {"next": {"href": f"{base_url}/things/page2"}},
            },
        )
    )
    page2 = respx_mock.get(f"{base_url}/things/page2").mock(
        return_value=httpx.Response(200, json={"data": [{"id": 3}], "links": {}})
    )
    endpoint = make_endpoint()

    iterator = endpoint.paginate_cursor("GET", "/things")
    first_item = await iterator.__anext__()

    assert first_item == {"id": 1}
    assert page1.call_count == 1
    assert page2.call_count == 0  # page 2 not yet yielded, so not yet fetched

    await iterator.aclose()


async def test_cursor_pagination_empty_first_page_yields_nothing(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    base_url: str,
) -> None:
    page1 = respx_mock.get(f"{base_url}/things").mock(
        return_value=httpx.Response(200, json={"data": [], "links": {}})
    )
    endpoint = make_endpoint()

    items = [item async for item in endpoint.paginate_cursor("GET", "/things")]

    assert items == []
    assert page1.call_count == 1


async def test_offset_pagination_walks_pages_and_stops_on_absent_token(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    base_url: str,
) -> None:
    route = respx_mock.get(f"{base_url}/schemas").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "schemas": [{"name": "a"}, {"name": "b"}],
                    "next_page_token": "tok-2",
                },
            ),
            httpx.Response(200, json={"schemas": [{"name": "c"}]}),
        ]
    )
    endpoint = make_endpoint()

    items = [
        item
        async for item in endpoint.paginate_offset(
            "GET", "/schemas", items_key="schemas", max_results=50
        )
    ]

    assert items == [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    assert route.call_count == 2

    first_call, second_call = route.calls
    assert first_call.request.url.params["max_results"] == "50"
    assert "page_token" not in first_call.request.url.params
    assert second_call.request.url.params["page_token"] == "tok-2"


async def test_offset_pagination_empty_first_page_yields_nothing(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    base_url: str,
) -> None:
    route = respx_mock.get(f"{base_url}/schemas").mock(
        return_value=httpx.Response(200, json={"schemas": []})
    )
    endpoint = make_endpoint()

    items = [
        item
        async for item in endpoint.paginate_offset("GET", "/schemas", items_key="schemas")
    ]

    assert items == []
    assert route.call_count == 1
