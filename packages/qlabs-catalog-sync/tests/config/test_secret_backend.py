"""SecretBackend: the environment default, swappability, and missing-secret behavior."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from qlabs_catalog_sync.config import (
    EndpointConfig,
    EnvironmentSecretBackend,
    SecretBackend,
    SecretNotFoundError,
)


def test_environment_backend_resolves_prefixed_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", "super-secret-value")

    backend = EnvironmentSecretBackend()
    secret = backend.get_secret(endpoint="qlik_acme", key="client_secret")

    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == "super-secret-value"


def test_environment_backend_normalizes_endpoint_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # "qlik-acme" (hyphen, lowercase) must resolve to the same variable as QLIK_ACME__...,
    # matching the SDK's ConnectorConfig.for_endpoint normalization exactly.
    monkeypatch.setenv("QLIK_ACME__API_KEY", "prod-key")

    backend = EnvironmentSecretBackend()
    secret = backend.get_secret(endpoint="qlik-acme", key="api_key")

    assert secret.get_secret_value() == "prod-key"


def test_environment_backend_missing_secret_raises_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABRICKS_PROD__TOKEN", raising=False)

    backend = EnvironmentSecretBackend()

    with pytest.raises(SecretNotFoundError) as exc_info:
        backend.get_secret(endpoint="databricks_prod", key="token")

    message = str(exc_info.value)
    assert "databricks_prod" in message
    assert "token" in message
    assert "DATABRICKS_PROD__TOKEN" in message
    assert exc_info.value.endpoint == "databricks_prod"
    assert exc_info.value.key == "token"
    assert exc_info.value.backend == "environment"


class DictSecretBackend:
    """A minimal, deliberately non-environment SecretBackend for testing swappability.

    Stands in for a future Vault / cloud-secret-manager backend: same protocol, entirely
    different storage.
    """

    def __init__(self, values: dict[tuple[str, str], str]) -> None:
        self._values = values

    def get_secret(self, *, endpoint: str, key: str) -> SecretStr:
        try:
            return SecretStr(self._values[(endpoint, key)])
        except KeyError:
            raise SecretNotFoundError(endpoint=endpoint, key=key, backend="dict") from None


def test_secret_backend_is_swappable() -> None:
    fake_backend: SecretBackend = DictSecretBackend({("qlik_acme", "client_secret"): "from-vault"})
    assert isinstance(fake_backend, SecretBackend)  # structural conformance to the Protocol

    endpoint = EndpointConfig(
        connector="qlik",
        settings={"base_url": "https://acme.us.qlikcloud.com"},
        secrets={"client_secret": "client_secret"},
    )

    resolved = endpoint.resolve("qlik_acme", fake_backend)

    assert resolved["base_url"] == "https://acme.us.qlikcloud.com"
    assert isinstance(resolved["client_secret"], SecretStr)
    assert resolved["client_secret"].get_secret_value() == "from-vault"


def test_swapped_backend_missing_secret_also_fails_clearly() -> None:
    empty_backend = DictSecretBackend({})
    endpoint = EndpointConfig(connector="qlik", secrets={"client_secret": "client_secret"})

    with pytest.raises(SecretNotFoundError) as exc_info:
        endpoint.resolve("qlik_acme", empty_backend)

    assert exc_info.value.endpoint == "qlik_acme"
    assert exc_info.value.key == "client_secret"


def test_resolved_secret_never_leaks_through_repr_or_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", "super-sensitive-value")
    endpoint = EndpointConfig(connector="qlik", secrets={"client_secret": "client_secret"})

    resolved = endpoint.resolve("qlik_acme", EnvironmentSecretBackend())

    assert "super-sensitive-value" not in repr(resolved)
    assert "super-sensitive-value" not in str(resolved)
    assert resolved["client_secret"].get_secret_value() == "super-sensitive-value"
