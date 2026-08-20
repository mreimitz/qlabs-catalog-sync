"""``Connector.setup()`` builds an ``HttpEndpoint`` wired to the SDK's OAuth2
client-credentials provider: a token is fetched on first use, the bearer header reaches
every API call, the token is reused while valid, and refreshed before it expires — never
reactively after a 401.

All of the caching/refresh mechanics themselves are the SDK's (T1.5) and are already
covered by ``qlabs_catalog_sync_sdk/tests/auth/test_oauth2_client_credentials.py``; these
tests prove that this connector's ``setup()`` wires it up correctly, not the provider's
own internals.
"""

from __future__ import annotations

import httpx

from qlabs_catalog_sync_sdk.config import ManualClock
from qlabs_connector_qlik import Connector

from .conftest import SPACE_ID, TENANT_BASE_URL

SPACE_URL = f"{TENANT_BASE_URL}/api/v1/spaces/{SPACE_ID}"


async def test_connector_instantiates_and_is_named_qlik() -> None:
    connector = Connector()
    assert connector.name == "qlik"
    assert Connector.ConfigModel.__name__ == "QlikConfig"


async def test_setup_wires_http_but_makes_no_request(
    respx_mock: object, connector: Connector
) -> None:
    # No respx route is registered at all. If setup() eagerly hit the network, respx
    # would raise "no matching route" before this assertion is ever reached.
    assert connector.http is not None


async def test_token_is_fetched_and_bearer_header_reaches_the_api_call(
    respx_mock: object, mock_token, connector: Connector
) -> None:
    mock_token()
    api_route = respx_mock.get(SPACE_URL).mock(
        return_value=httpx.Response(200, json={"id": SPACE_ID})
    )
    assert connector.http is not None

    response = await connector.http.get(f"/api/v1/spaces/{SPACE_ID}")

    assert response.status_code == 200
    assert api_route.calls.last.request.headers["authorization"] == "Bearer tok-1"


async def test_token_is_reused_across_calls(
    respx_mock: object, mock_token, connector: Connector
) -> None:
    token_route = mock_token()
    respx_mock.get(SPACE_URL).mock(return_value=httpx.Response(200, json={"id": SPACE_ID}))
    assert connector.http is not None

    await connector.http.get(f"/api/v1/spaces/{SPACE_ID}")
    await connector.http.get(f"/api/v1/spaces/{SPACE_ID}")

    assert token_route.call_count == 1


async def test_token_is_refreshed_before_it_expires(
    respx_mock: object, connector: Connector, clock: ManualClock
) -> None:
    token_route = respx_mock.post(f"{TENANT_BASE_URL}/oauth/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600}),
        ]
    )
    api_route = respx_mock.get(SPACE_URL).mock(
        return_value=httpx.Response(200, json={"id": SPACE_ID})
    )
    assert connector.http is not None

    await connector.http.get(f"/api/v1/spaces/{SPACE_ID}")
    assert token_route.call_count == 1
    assert api_route.calls.last.request.headers["authorization"] == "Bearer tok-1"

    # Within the default 60s refresh margin, before the token would actually expire —
    # the provider refreshes proactively rather than waiting for a 401.
    clock.advance(3600 - 30)
    await connector.http.get(f"/api/v1/spaces/{SPACE_ID}")

    assert token_route.call_count == 2
    assert api_route.calls.last.request.headers["authorization"] == "Bearer tok-2"
