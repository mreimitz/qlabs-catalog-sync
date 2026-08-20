"""EndpointConfig: shape, independence across endpoints of the same connector, and the
end-to-end handoff into the SDK's ConnectorConfig.for_endpoint.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from qlabs_catalog_sync.config import EndpointConfig, EnvironmentSecretBackend
from qlabs_catalog_sync_sdk.config import ConnectorConfig


def test_connector_is_required() -> None:
    with pytest.raises(ValidationError):
        EndpointConfig()  # type: ignore[call-arg]


def test_defaults_are_empty_settings_and_secrets() -> None:
    endpoint = EndpointConfig(connector="databricks")
    assert endpoint.settings == {}
    assert endpoint.secrets == {}


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EndpointConfig(connector="qlik", not_a_real_field="boom")  # type: ignore[call-arg]


def test_model_dump_never_contains_secret_material(monkeypatch: pytest.MonkeyPatch) -> None:
    # secrets holds *references* (here, an env var suffix), never the resolved value —
    # so there is nothing sensitive for model_dump/model_dump_json to leak.
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", "super-sensitive-value")
    endpoint = EndpointConfig(
        connector="qlik",
        settings={"base_url": "https://acme.us.qlikcloud.com"},
        secrets={"client_secret": "client_secret"},
    )

    dumped = str(endpoint.model_dump())
    dumped_json = endpoint.model_dump_json()

    assert "super-sensitive-value" not in dumped
    assert "super-sensitive-value" not in dumped_json
    assert "super-sensitive-value" not in repr(endpoint)
    assert "super-sensitive-value" not in str(endpoint)


def test_two_qlik_endpoints_are_configured_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two Qlik tenants in one process must not see each other's settings or secrets."""
    monkeypatch.setenv("QLIK_PROD__CLIENT_SECRET", "prod-secret")
    monkeypatch.setenv("QLIK_DEV__CLIENT_SECRET", "dev-secret")

    prod = EndpointConfig(
        connector="qlik",
        settings={"base_url": "https://prod.us.qlikcloud.com"},
        secrets={"client_secret": "client_secret"},
    )
    dev = EndpointConfig(
        connector="qlik",
        settings={"base_url": "https://dev.us.qlikcloud.com"},
        secrets={"client_secret": "client_secret"},
    )

    backend = EnvironmentSecretBackend()
    resolved_prod = prod.resolve("qlik_prod", backend)
    resolved_dev = dev.resolve("qlik_dev", backend)

    assert resolved_prod["base_url"] == "https://prod.us.qlikcloud.com"
    assert resolved_prod["client_secret"].get_secret_value() == "prod-secret"
    assert resolved_dev["base_url"] == "https://dev.us.qlikcloud.com"
    assert resolved_dev["client_secret"].get_secret_value() == "dev-secret"


class _DummyQlikConnectorConfig(ConnectorConfig):
    """A stand-in connector ConfigModel, shaped like a real one would be (T1.7)."""

    base_url: str
    client_secret: SecretStr


def test_resolved_endpoint_feeds_connector_config_for_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of EndpointConfig.resolve: its output is exactly what
    ConnectorConfig.for_endpoint(endpoint_key, **values) wants as explicit overrides,
    with nothing further to glue together.
    """
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", "sekret")
    endpoint = EndpointConfig(
        connector="qlik",
        settings={"base_url": "https://acme.us.qlikcloud.com"},
        secrets={"client_secret": "client_secret"},
    )

    overrides = endpoint.resolve("qlik_acme", EnvironmentSecretBackend())
    config = _DummyQlikConnectorConfig.for_endpoint("qlik_acme", **overrides)

    assert config.base_url == "https://acme.us.qlikcloud.com"
    assert config.client_secret.get_secret_value() == "sekret"
