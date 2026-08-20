"""The Qlik client secret must never leak: not in ``repr()``/``str()`` of the config, the
connector, or the auth wiring, and not in the message of any exception this connector
raises — including the unhealthy ``HealthStatus.reason`` built from a failed auth
attempt."""

from __future__ import annotations

import httpx

from qlabs_connector_qlik import Connector
from qlabs_connector_qlik.config import QlikConfig

from .conftest import SPACE_ID, TENANT_BASE_URL

SPACE_URL = f"{TENANT_BASE_URL}/api/v1/spaces/{SPACE_ID}"
SECRET = "s3cr3t-value"


def test_secret_is_absent_from_config_repr_and_str(qlik_config: QlikConfig) -> None:
    assert SECRET not in repr(qlik_config)
    assert SECRET not in str(qlik_config)
    assert SECRET not in repr(qlik_config.client_secret)
    assert SECRET not in str(qlik_config.client_secret)


def test_secret_is_absent_from_config_model_dump(qlik_config: QlikConfig) -> None:
    dumped = qlik_config.model_dump()
    assert SECRET not in repr(dumped)
    dumped_json = qlik_config.model_dump_json()
    assert SECRET not in dumped_json


async def test_secret_is_absent_from_connector_and_provider_repr(connector: Connector) -> None:
    assert SECRET not in repr(connector)
    assert SECRET not in str(connector)
    provider = connector._oauth_provider
    assert provider is not None
    assert SECRET not in repr(provider)
    assert SECRET not in str(provider)


async def test_secret_is_absent_from_a_failed_auth_exception_message(
    respx_mock: object, mock_token, connector: Connector
) -> None:
    mock_token(status_code=401)

    status = await connector.healthcheck()

    assert status.reason is not None
    assert SECRET not in status.reason


async def test_secret_is_never_sent_anywhere_but_the_json_token_request_body(
    respx_mock: object, mock_token, connector: Connector
) -> None:
    token_route = mock_token()
    api_route = respx_mock.get(SPACE_URL).mock(
        return_value=httpx.Response(200, json={"id": SPACE_ID})
    )

    await connector.healthcheck()

    token_request = token_route.calls.last.request
    # The one place the raw secret legitimately appears: the JSON body of the token
    # exchange itself (Qlik's documented client-credentials shape, RS-02 section 3.2).
    assert SECRET.encode() in token_request.content
    # It must never appear in that request's own headers, nor anywhere in the follow-up
    # API call (which must carry only the bearer token, never the client secret).
    assert SECRET not in str(token_request.headers)
    api_request = api_route.calls.last.request
    assert SECRET not in str(api_request.headers)
    assert SECRET.encode() not in api_request.content
