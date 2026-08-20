"""``Connector.healthcheck()`` — real behavior against respx-mocked HTTP, not mocks of
the connector itself.

Covers: a good response reports healthy; an unauthorized response (both at the API call
and at the OAuth2 token exchange itself) reports a non-healthy status carrying a reason
and is built from a real ``AuthError``; a 429 with ``Retry-After`` is retried by
``HttpEndpoint`` and then succeeds; exhausted 429/5xx retries and a transport failure
report ``DEGRADED`` (retryable, still scheduled) rather than ``UNHEALTHY``; and calling
``healthcheck()`` before ``setup()`` fails loudly instead of silently.
"""

from __future__ import annotations

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import HealthState
from qlabs_connector_qlik import Connector

from .conftest import SPACE_ID, TENANT_BASE_URL

SPACE_URL = f"{TENANT_BASE_URL}/api/v1/spaces/{SPACE_ID}"


async def test_healthy_when_the_space_call_succeeds(
    respx_mock: object, mock_token, connector: Connector
) -> None:
    mock_token()
    respx_mock.get(SPACE_URL).mock(return_value=httpx.Response(200, json={"id": SPACE_ID}))

    status = await connector.healthcheck()

    assert status.is_healthy
    assert status.state is HealthState.HEALTHY
    assert status.endpoint == "qlik"
    assert status.details["status_code"] == 200


async def test_401_from_the_api_call_is_unhealthy_with_a_reason(
    respx_mock: object, mock_token, connector: Connector
) -> None:
    mock_token()
    respx_mock.get(SPACE_URL).mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))

    status = await connector.healthcheck()

    assert status.state is HealthState.UNHEALTHY
    assert status.should_quarantine
    assert status.reason is not None
    assert "401" in status.reason


async def test_403_from_the_api_call_is_unhealthy_with_a_reason(
    respx_mock: object, mock_token, connector: Connector
) -> None:
    mock_token()
    respx_mock.get(SPACE_URL).mock(return_value=httpx.Response(403, json={"error": "forbidden"}))

    status = await connector.healthcheck()

    assert status.state is HealthState.UNHEALTHY
    assert status.reason is not None
    assert "403" in status.reason


async def test_failed_token_exchange_is_unhealthy_with_a_reason(
    respx_mock: object, mock_token, connector: Connector
) -> None:
    # The credentials themselves are rejected before any API call is attempted — the
    # SDK's OAuth2ClientCredentialsProvider raises AuthError directly.
    mock_token(status_code=401)

    status = await connector.healthcheck()

    assert status.state is HealthState.UNHEALTHY
    assert status.should_quarantine
    assert status.reason is not None


async def test_404_for_the_configured_space_is_unhealthy(
    respx_mock: object, mock_token, connector: Connector
) -> None:
    mock_token()
    respx_mock.get(SPACE_URL).mock(return_value=httpx.Response(404, json={"error": "not found"}))

    status = await connector.healthcheck()

    assert status.state is HealthState.UNHEALTHY
    assert status.reason is not None


async def test_429_with_retry_after_is_retried_and_then_healthy(
    respx_mock: object, mock_token, connector: Connector
) -> None:
    mock_token()
    route = respx_mock.get(SPACE_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"id": SPACE_ID}),
        ]
    )

    status = await connector.healthcheck()

    assert status.is_healthy
    assert route.call_count == 2


async def test_exhausted_5xx_retries_report_degraded_not_unhealthy(
    respx_mock: object, mock_token, make_connector
) -> None:
    mock_token()
    respx_mock.get(SPACE_URL).mock(return_value=httpx.Response(503))
    connector = make_connector(max_attempts=2, backoff_base_seconds=0.01, backoff_max_seconds=0.02)

    status = await connector.healthcheck()

    assert status.state is HealthState.DEGRADED
    assert not status.should_quarantine
    assert status.reason is not None
    assert "503" in status.reason


async def test_transport_failure_reports_degraded(
    respx_mock: object, mock_token, make_connector
) -> None:
    mock_token()
    respx_mock.get(SPACE_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    connector = make_connector(max_attempts=2, backoff_base_seconds=0.01, backoff_max_seconds=0.02)

    status = await connector.healthcheck()

    assert status.state is HealthState.DEGRADED
    assert status.reason is not None


async def test_healthcheck_before_setup_raises() -> None:
    connector = Connector()
    with pytest.raises(RuntimeError, match="setup"):
        await connector.healthcheck()
