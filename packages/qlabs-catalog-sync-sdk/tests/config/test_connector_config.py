"""ConnectorConfig: per-endpoint env loading, isolation, validation, secret hiding."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from qlabs_catalog_sync_sdk.config import ConnectorConfig


class DummyConfig(ConnectorConfig):
    """A stand-in connector config: one plain field, one secret, one with a default."""

    base_url: str
    api_key: SecretStr
    timeout_seconds: int = 30


def test_loads_from_env_with_endpoint_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EP1__BASE_URL", "https://ep1.example.com")
    monkeypatch.setenv("EP1__API_KEY", "sekret-1")

    config = DummyConfig.for_endpoint("ep1")

    assert config.base_url == "https://ep1.example.com"
    assert config.api_key.get_secret_value() == "sekret-1"
    assert config.timeout_seconds == 30  # default applies when the env var is absent


def test_endpoint_prefixes_are_case_insensitive_and_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QLIK_ACME__BASE_URL", "https://acme.qlikcloud.com")
    monkeypatch.setenv("QLIK_ACME__API_KEY", "sekret")

    # "qlik-acme" (hyphen, lowercase) must resolve to the same env vars as
    # QLIK_ACME__ once normalized.
    config = DummyConfig.for_endpoint("qlik-acme")

    assert config.base_url == "https://acme.qlikcloud.com"


def test_two_endpoints_are_configured_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two Qlik-shaped endpoints in one process must not see each other's env vars."""
    monkeypatch.setenv("QLIK_PROD__BASE_URL", "https://prod.qlikcloud.com")
    monkeypatch.setenv("QLIK_PROD__API_KEY", "prod-key")
    monkeypatch.setenv("QLIK_DEV__BASE_URL", "https://dev.qlikcloud.com")
    monkeypatch.setenv("QLIK_DEV__API_KEY", "dev-key")
    monkeypatch.setenv("QLIK_DEV__TIMEOUT_SECONDS", "5")

    prod = DummyConfig.for_endpoint("qlik_prod")
    dev = DummyConfig.for_endpoint("qlik_dev")

    assert prod.base_url == "https://prod.qlikcloud.com"
    assert prod.api_key.get_secret_value() == "prod-key"
    assert prod.timeout_seconds == 30

    assert dev.base_url == "https://dev.qlikcloud.com"
    assert dev.api_key.get_secret_value() == "dev-key"
    assert dev.timeout_seconds == 5


def test_missing_required_field_raises_clear_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EP1__BASE_URL", "https://ep1.example.com")
    # api_key deliberately left unset.

    with pytest.raises(ValidationError) as exc_info:
        DummyConfig.for_endpoint("ep1")

    message = str(exc_info.value)
    assert "api_key" in message
    assert "Field required" in message


def test_explicit_overrides_win_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EP1__BASE_URL", "https://from-env.example.com")
    monkeypatch.setenv("EP1__API_KEY", "from-env")

    config = DummyConfig.for_endpoint("ep1", base_url="https://override.example.com")

    assert config.base_url == "https://override.example.com"
    assert config.api_key.get_secret_value() == "from-env"


def test_unknown_field_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EP1__BASE_URL", "https://ep1.example.com")
    monkeypatch.setenv("EP1__API_KEY", "sekret")

    with pytest.raises(ValidationError):
        DummyConfig.for_endpoint("ep1", unexpected_field="boom")


def test_secret_field_never_leaks_through_repr_str_or_dump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EP1__BASE_URL", "https://ep1.example.com")
    monkeypatch.setenv("EP1__API_KEY", "super-sensitive-value")

    config = DummyConfig.for_endpoint("ep1")
    secret = "super-sensitive-value"

    assert secret not in repr(config)
    assert secret not in str(config)
    assert secret not in str(config.model_dump())
    assert secret not in config.model_dump_json()

    # The real value is still retrievable on purpose — it is hidden, not lost.
    assert config.api_key.get_secret_value() == secret
