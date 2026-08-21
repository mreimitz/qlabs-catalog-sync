"""Connector.setup() + Connector.healthcheck(): key-pair JWT authentication and the
cheap GET /api/v2/databases probe. No live Snowflake tenant is used anywhere in this
suite — every request is intercepted by respx.
"""

from __future__ import annotations

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import HealthState
from qlabs_connector_snowflake import Connector

from .conftest import DATABASES_URL, build_config, build_ctx


def test_connector_instantiates_with_no_args_and_its_name_is_snowflake() -> None:
    connector = Connector()

    assert connector.name == "snowflake"
    assert Connector.name == "snowflake"


async def test_healthcheck_before_setup_raises() -> None:
    connector = Connector()

    with pytest.raises(RuntimeError, match="setup"):
        await connector.healthcheck()


async def test_setup_succeeds_with_a_valid_key(rsa_keypair, clock) -> None:
    config = build_config(private_key=rsa_keypair.private_pem)
    connector = Connector(clock=clock)

    await connector.setup(build_ctx(config))  # must not raise


async def test_setup_sends_no_request_key_pair_jwt_is_minted_locally(
    rsa_keypair, respx_mock, clock
) -> None:
    """Unlike an OAuth2/token-exchange provider, key-pair JWT auth needs no network
    round trip to obtain a token — the assertion itself *is* the bearer token."""
    config = build_config(private_key=rsa_keypair.private_pem)
    connector = Connector(clock=clock)

    await connector.setup(build_ctx(config))

    assert respx_mock.calls.call_count == 0


async def test_setup_propagates_auth_error_for_a_malformed_key(clock) -> None:
    config = build_config(
        private_key="-----BEGIN PRIVATE KEY-----\nnot-real\n-----END PRIVATE KEY-----"
    )
    connector = Connector(clock=clock)

    from qlabs_catalog_sync_sdk.exceptions import AuthError

    with pytest.raises(AuthError):
        await connector.setup(build_ctx(config))


async def test_healthcheck_reports_healthy_on_a_good_probe(respx_mock, rsa_keypair, clock) -> None:
    route = respx_mock.get(DATABASES_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"name": "SALES"}]})
    )
    config = build_config(private_key=rsa_keypair.private_pem)
    connector = Connector(clock=clock)
    await connector.setup(build_ctx(config))

    status = await connector.healthcheck()

    assert status.is_healthy
    assert status.state is HealthState.HEALTHY
    assert status.endpoint == "snowflake"
    assert route.call_count == 1
    sent = route.calls.last.request
    assert sent.url.params["showLimit"] == "1"


async def test_healthcheck_sends_the_keypair_jwt_headers(respx_mock, rsa_keypair, clock) -> None:
    route = respx_mock.get(DATABASES_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    config = build_config(
        organization="acme",
        account="primary",
        user="svc_qlabs",
        private_key=rsa_keypair.private_pem,
    )
    connector = Connector(clock=clock)
    await connector.setup(build_ctx(config))

    await connector.healthcheck()

    sent = route.calls.last.request
    assert sent.headers["authorization"].startswith("Bearer ")
    assert sent.headers["x-snowflake-authorization-token-type"] == "KEYPAIR_JWT"


async def test_healthcheck_maps_401_to_unhealthy_with_a_reason(
    respx_mock, rsa_keypair, clock
) -> None:
    respx_mock.get(DATABASES_URL).mock(
        return_value=httpx.Response(401, json={"message": "JWT token is invalid."})
    )
    config = build_config(private_key=rsa_keypair.private_pem)
    connector = Connector(clock=clock)
    await connector.setup(build_ctx(config))

    status = await connector.healthcheck()

    assert status.state is HealthState.UNHEALTHY
    assert status.should_quarantine
    assert status.reason and "JWT token is invalid" in status.reason


async def test_healthcheck_maps_403_to_unhealthy_with_a_reason(
    respx_mock, rsa_keypair, clock
) -> None:
    respx_mock.get(DATABASES_URL).mock(
        return_value=httpx.Response(403, json={"message": "insufficient privileges"})
    )
    config = build_config(private_key=rsa_keypair.private_pem)
    connector = Connector(clock=clock)
    await connector.setup(build_ctx(config))

    status = await connector.healthcheck()

    assert status.state is HealthState.UNHEALTHY


async def test_healthcheck_maps_429_to_degraded_with_a_reason(
    respx_mock, rsa_keypair, clock
) -> None:
    respx_mock.get(DATABASES_URL).mock(
        return_value=httpx.Response(429, json={"message": "rate limited"})
    )
    config = build_config(private_key=rsa_keypair.private_pem)
    connector = Connector(clock=clock)
    await connector.setup(build_ctx(config))

    status = await connector.healthcheck()

    assert status.state is HealthState.DEGRADED
    assert not status.should_quarantine
    assert status.reason and "rate limited" in status.reason


async def test_healthcheck_maps_5xx_to_degraded_with_a_reason(
    respx_mock, rsa_keypair, clock
) -> None:
    respx_mock.get(DATABASES_URL).mock(
        return_value=httpx.Response(503, json={"message": "service unavailable"})
    )
    config = build_config(private_key=rsa_keypair.private_pem)
    connector = Connector(clock=clock)
    await connector.setup(build_ctx(config))

    status = await connector.healthcheck()

    assert status.state is HealthState.DEGRADED
    assert status.reason and "service unavailable" in status.reason


async def test_healthcheck_maps_a_transport_failure_to_degraded(
    respx_mock, rsa_keypair, clock
) -> None:
    respx_mock.get(DATABASES_URL).mock(side_effect=httpx.ConnectError("connection reset"))
    config = build_config(private_key=rsa_keypair.private_pem)
    connector = Connector(clock=clock)
    await connector.setup(build_ctx(config))

    status = await connector.healthcheck()

    assert status.state is HealthState.DEGRADED
    assert status.reason and "connection reset" in status.reason


async def test_close_releases_the_http_endpoint(respx_mock, rsa_keypair, clock) -> None:
    config = build_config(private_key=rsa_keypair.private_pem)
    connector = Connector(clock=clock)
    await connector.setup(build_ctx(config))

    await connector.close()

    assert connector._http is None


async def test_close_before_setup_is_a_safe_no_op() -> None:
    connector = Connector()

    await connector.close()  # must not raise
