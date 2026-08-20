"""HttpEndpoint construction and plumbing: base URL, auth injection (both the
static ``(scheme, token)`` form and the ``AuthHeaderProvider`` protocol form),
default headers, and clean async close/context-manager behavior.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import httpx
import pytest

from qlabs_catalog_sync_sdk.http import HttpEndpoint


async def test_static_auth_sets_the_authorization_header(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    base_url: str,
) -> None:
    route = respx_mock.get(f"{base_url}/things").mock(return_value=httpx.Response(200, json={}))
    endpoint = make_endpoint(auth=("Bearer", "tok-123"))

    await endpoint.get("/things")

    assert route.calls.last.request.headers["Authorization"] == "Bearer tok-123"


async def test_provider_auth_sets_headers_asynchronously(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    base_url: str,
) -> None:
    class _Provider:
        async def get_headers(self) -> Mapping[str, str]:
            return {"Authorization": "Bearer from-provider", "X-Tenant": "acme"}

    route = respx_mock.get(f"{base_url}/things").mock(return_value=httpx.Response(200, json={}))
    endpoint = make_endpoint(auth=_Provider())

    await endpoint.get("/things")

    sent = route.calls.last.request.headers
    assert sent["Authorization"] == "Bearer from-provider"
    assert sent["X-Tenant"] == "acme"


async def test_no_auth_means_no_authorization_header(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    base_url: str,
) -> None:
    route = respx_mock.get(f"{base_url}/things").mock(return_value=httpx.Response(200, json={}))
    endpoint = make_endpoint()

    await endpoint.get("/things")

    assert "Authorization" not in route.calls.last.request.headers


async def test_default_headers_are_sent_on_every_request(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    base_url: str,
) -> None:
    route = respx_mock.get(f"{base_url}/things").mock(return_value=httpx.Response(200, json={}))
    endpoint = make_endpoint(headers={"Accept": "application/json", "X-Client": "qlabs-sdk"})

    await endpoint.get("/things")

    sent = route.calls.last.request.headers
    assert sent["Accept"] == "application/json"
    assert sent["X-Client"] == "qlabs-sdk"


async def test_aclose_is_idempotent(
    make_endpoint: Callable[..., HttpEndpoint],
) -> None:
    endpoint = make_endpoint()

    await endpoint.aclose()
    await endpoint.aclose()  # must not raise


async def test_async_context_manager_closes_on_exit(base_url: str) -> None:
    async with HttpEndpoint(base_url) as endpoint:
        assert isinstance(endpoint, HttpEndpoint)

    with pytest.raises(RuntimeError):
        # httpx raises once the underlying client is closed.
        await endpoint.get("/things")
