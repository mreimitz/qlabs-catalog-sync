"""The client secret never leaks through the connector's own surfaces: its repr, the
token provider it builds internally, or an auth failure raised out of setup()."""

from __future__ import annotations

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_connector_databricks import Connector

from .conftest import TOKEN_URL, RecordingWorkspaceClientFactory, build_config, build_ctx

MARKER = "MARKER-CONNECTOR-SECRET-4d7e"


async def test_secret_never_appears_in_the_connector_or_its_token_provider_repr(
    respx_mock, transport
):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    connector = Connector(
        workspace_client_factory=RecordingWorkspaceClientFactory(), transport=transport
    )
    config = build_config(client_secret=MARKER)

    await connector.setup(build_ctx(config))

    assert MARKER not in repr(connector)
    assert MARKER not in str(connector)
    assert connector._token_provider is not None
    assert MARKER not in repr(connector._token_provider)
    assert MARKER not in str(connector._token_provider)


async def test_secret_never_appears_in_a_setup_auth_failure(respx_mock, transport):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    connector = Connector(
        workspace_client_factory=RecordingWorkspaceClientFactory(), transport=transport
    )
    config = build_config(client_secret=MARKER)

    with pytest.raises(AuthError) as exc_info:
        await connector.setup(build_ctx(config))

    assert MARKER not in str(exc_info.value)
    assert MARKER not in repr(exc_info.value)
