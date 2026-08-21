"""build_oauth_provider / fetch_bearer_token: the actual OAuth M2M wire shape against
Databricks' token endpoint, and that the resulting token reaches an outgoing call as a
bearer header. No live workspace is used anywhere here — the token endpoint and a
downstream Unity Catalog call are both respx-mocked httpx routes.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_connector_databricks.auth import build_oauth_provider, fetch_bearer_token

from .conftest import CATALOGS_URL, TOKEN_URL, build_config


async def test_token_request_matches_the_databricks_oauth_m2m_shape(respx_mock, transport):
    route = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    provider = build_oauth_provider(build_config(), transport=transport)

    token = await fetch_bearer_token(provider)

    assert token == "tok-1"
    request = route.calls.last.request
    assert request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded")
    auth_header = request.headers.get("authorization", "")
    assert auth_header.startswith("Basic ")
    form = parse_qs(request.content.decode())
    assert form == {"grant_type": ["client_credentials"], "scope": ["all-apis"]}
    # Credentials travel as HTTP Basic, never as body fields.
    assert "client_id" not in form
    assert "client_secret" not in form


async def test_fetched_token_reaches_a_downstream_call_as_a_bearer_header(respx_mock, transport):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-xyz", "expires_in": 3600})
    )
    catalogs_route = respx_mock.get(CATALOGS_URL).mock(
        return_value=httpx.Response(200, json={"catalogs": []})
    )
    provider = build_oauth_provider(build_config(), transport=transport)

    token = await fetch_bearer_token(provider)
    # Exactly what the connector's WorkspaceClient wiring does with the token: use it
    # to authenticate a real REST call.
    response = await transport.get(CATALOGS_URL, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    sent = catalogs_route.calls.last.request
    assert sent.headers["authorization"] == "Bearer tok-xyz"


async def test_invalid_credentials_raise_auth_error(respx_mock, transport):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    provider = build_oauth_provider(build_config(), transport=transport)

    with pytest.raises(AuthError):
        await fetch_bearer_token(provider)


async def test_client_secret_never_appears_in_the_auth_error(respx_mock, transport):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    provider = build_oauth_provider(
        build_config(client_secret="MARKER-OAUTH-SECRET-9f3c"), transport=transport
    )

    with pytest.raises(AuthError) as exc_info:
        await fetch_bearer_token(provider)

    assert "MARKER-OAUTH-SECRET-9f3c" not in str(exc_info.value)
    assert "MARKER-OAUTH-SECRET-9f3c" not in repr(exc_info.value)
