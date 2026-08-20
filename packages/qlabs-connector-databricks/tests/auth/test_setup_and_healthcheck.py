"""Connector.setup() + Connector.healthcheck(): OAuth M2M authentication, building the
databricks-sdk WorkspaceClient (via an injected fake — no live workspace is used
anywhere in this suite), and mapping outcomes onto HealthStatus.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from databricks.sdk.errors.platform import InternalError, TooManyRequests, Unauthenticated

from qlabs_catalog_sync_sdk.config import ManualClock
from qlabs_catalog_sync_sdk.contract import HealthState
from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_connector_databricks import Connector

from .conftest import TOKEN_URL, RecordingWorkspaceClientFactory, build_ctx


class _AuthClockAdapter:
    """Adapts a :class:`ManualClock` (which also has ``async sleep``) to the OAuth
    provider's narrower ``Clock`` protocol (``.now()`` only), so one fake clock can
    drive both the test's own timeline and the token cache's refresh-before-expiry
    check deterministically."""

    def __init__(self, clock: ManualClock) -> None:
        self._clock = clock

    def now(self) -> datetime:
        return self._clock.now()


def _ok_token_route(respx_mock, *, token: str = "tok-1", expires_in: int = 3600):
    return respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": token, "expires_in": expires_in})
    )


def test_connector_instantiates_with_no_args_and_its_name_is_databricks():
    connector = Connector()

    assert connector.name == "databricks"
    assert Connector.name == "databricks"


async def test_healthcheck_before_setup_raises():
    connector = Connector()

    with pytest.raises(RuntimeError):
        await connector.healthcheck()


async def test_setup_authenticates_and_builds_the_workspace_client(respx_mock, transport):
    route = _ok_token_route(respx_mock, token="tok-setup")
    factory = RecordingWorkspaceClientFactory()
    connector = Connector(workspace_client_factory=factory, transport=transport)

    await connector.setup(build_ctx())

    assert route.call_count == 1
    assert factory.calls == [("https://acme.cloud.databricks.com", "tok-setup")]


async def test_setup_propagates_auth_error_for_invalid_credentials(respx_mock, transport):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    connector = Connector(
        workspace_client_factory=RecordingWorkspaceClientFactory(), transport=transport
    )

    with pytest.raises(AuthError):
        await connector.setup(build_ctx())


async def test_healthcheck_reports_healthy_on_a_good_probe(respx_mock, transport):
    _ok_token_route(respx_mock)
    factory = RecordingWorkspaceClientFactory()
    connector = Connector(workspace_client_factory=factory, transport=transport)
    await connector.setup(build_ctx())

    status = await connector.healthcheck()

    assert status.is_healthy
    assert status.state is HealthState.HEALTHY
    assert status.endpoint == "databricks"
    assert status.details["probe"] == "unity_catalog.catalogs.list"
    # The probe actually ran against the fake client's catalogs API.
    assert factory.last_client is not None
    assert factory.last_client.catalogs.calls == [1]


async def test_healthcheck_maps_401_to_unhealthy_with_a_reason(respx_mock, transport):
    _ok_token_route(respx_mock)
    factory = RecordingWorkspaceClientFactory(error=Unauthenticated("token expired"))
    connector = Connector(workspace_client_factory=factory, transport=transport)
    await connector.setup(build_ctx())

    status = await connector.healthcheck()

    assert status.state is HealthState.UNHEALTHY
    assert status.should_quarantine
    assert status.reason and "token expired" in status.reason


async def test_healthcheck_maps_429_to_degraded_with_a_reason(respx_mock, transport):
    _ok_token_route(respx_mock)
    error = TooManyRequests("slow down", retry_after_secs=5)
    factory = RecordingWorkspaceClientFactory(error=error)
    connector = Connector(workspace_client_factory=factory, transport=transport)
    await connector.setup(build_ctx())

    status = await connector.healthcheck()

    assert status.state is HealthState.DEGRADED
    assert not status.should_quarantine
    assert status.reason and "slow down" in status.reason


async def test_healthcheck_maps_5xx_to_degraded_with_a_reason(respx_mock, transport):
    _ok_token_route(respx_mock)
    factory = RecordingWorkspaceClientFactory(error=InternalError("workspace is unwell"))
    connector = Connector(workspace_client_factory=factory, transport=transport)
    await connector.setup(build_ctx())

    status = await connector.healthcheck()

    assert status.state is HealthState.DEGRADED
    assert status.reason and "workspace is unwell" in status.reason


async def test_healthcheck_maps_a_transport_failure_to_degraded(respx_mock, transport):
    _ok_token_route(respx_mock)
    factory = RecordingWorkspaceClientFactory(error=ConnectionError("connection reset"))
    connector = Connector(workspace_client_factory=factory, transport=transport)
    await connector.setup(build_ctx())

    status = await connector.healthcheck()

    assert status.state is HealthState.DEGRADED
    assert status.reason and "connection reset" in status.reason


async def test_healthcheck_reuses_a_still_fresh_cached_token_without_a_new_fetch(
    respx_mock, transport
):
    """healthcheck() rebuilds the WorkspaceClient every call, but that must be cheap
    when the token has not expired: proves the OAuth provider's own refresh-before-
    expiry cache — not a second HTTP round trip — is what makes that affordable."""
    clock = ManualClock()
    route = _ok_token_route(respx_mock, token="tok-first", expires_in=3600)
    factory = RecordingWorkspaceClientFactory()
    connector = Connector(
        workspace_client_factory=factory, transport=transport, clock=_AuthClockAdapter(clock)
    )
    await connector.setup(build_ctx())
    assert route.call_count == 1

    clock.advance(10.0)
    status = await connector.healthcheck()

    assert status.is_healthy
    assert route.call_count == 1  # cached token reused, no new token request
    assert factory.calls[-1] == ("https://acme.cloud.databricks.com", "tok-first")


async def test_healthcheck_fetches_a_fresh_token_once_the_cache_is_near_expiry(
    respx_mock, transport
):
    clock = ManualClock()
    route = respx_mock.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-first", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-second", "expires_in": 3600}),
        ]
    )
    factory = RecordingWorkspaceClientFactory()
    connector = Connector(
        workspace_client_factory=factory, transport=transport, clock=_AuthClockAdapter(clock)
    )
    await connector.setup(build_ctx())
    assert route.call_count == 1

    clock.advance(3600 - 30)  # inside the default 60s refresh margin
    status = await connector.healthcheck()

    assert status.is_healthy
    assert route.call_count == 2
    assert factory.calls[-1] == ("https://acme.cloud.databricks.com", "tok-second")


async def test_close_releases_an_owned_transport_but_leaves_an_injected_one_open(transport):
    owning_connector = Connector()
    owned_transport = owning_connector._transport
    await owning_connector.close()
    assert owned_transport.is_closed

    injected_connector = Connector(transport=transport)
    await injected_connector.close()
    assert not transport.is_closed
