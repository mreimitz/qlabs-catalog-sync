"""EngineConfig: loading from a file and from env, cross-referential validation, the
v1 direction guardrails, and end-to-end secret resolution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from qlabs_catalog_sync.config import EngineConfig, EnvironmentSecretBackend, SecretNotFoundError

REALISTIC_CONFIG: dict[str, object] = {
    "endpoints": {
        "databricks_prod": {
            "connector": "databricks",
            "settings": {
                "host": "https://adb-1234567890.18.azuredatabricks.net",
                "warehouse_id": "abcd1234efgh5678",
            },
            "secrets": {"token": "token"},
        },
        "qlik_acme": {
            "connector": "qlik",
            "settings": {"base_url": "https://acme.us.qlikcloud.com"},
            "secrets": {"client_id": "client_id", "client_secret": "client_secret"},
        },
    },
    "pairs": [
        {
            "name": "databricks_prod_to_qlik_acme_sales",
            "source": "databricks_prod",
            "target": "qlik_acme",
            "catalog_schema_patterns": ["prod_catalog.sales_*", "prod_catalog.finance"],
            "target_space": "Data Products - Sales",
            "entity_types": ["data_product", "dataset"],
            "cadence_seconds": 900,
            "manual_edit_policy": {
                "default": "source_wins",
                "per_field": {"data_product.description": "preserve_local"},
            },
            "activation_opt_in": False,
        }
    ],
}


def _set_realistic_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_PROD__TOKEN", "dbx-token")
    monkeypatch.setenv("QLIK_ACME__CLIENT_ID", "acme-client-id")
    monkeypatch.setenv("QLIK_ACME__CLIENT_SECRET", "acme-client-secret")


def test_complete_valid_config_loads_from_a_realistic_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_realistic_secrets(monkeypatch)
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps(REALISTIC_CONFIG))

    config = EngineConfig.load(config_file=config_path)

    assert set(config.endpoints) == {"databricks_prod", "qlik_acme"}
    assert config.endpoints["qlik_acme"].connector == "qlik"
    assert len(config.pairs) == 1
    pair = config.pairs[0]
    assert pair.name == "databricks_prod_to_qlik_acme_sales"
    assert pair.activation_opt_in is False
    assert pair.matches("prod_catalog", "sales_eu") is True
    assert pair.matches("prod_catalog", "hr") is False

    credentials = config.resolve_credentials()
    assert credentials["databricks_prod"]["token"].get_secret_value() == "dbx-token"
    assert credentials["qlik_acme"]["client_secret"].get_secret_value() == "acme-client-secret"


def test_complete_valid_config_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_realistic_secrets(monkeypatch)
    monkeypatch.setenv("ENDPOINTS", json.dumps(REALISTIC_CONFIG["endpoints"]))
    monkeypatch.setenv("PAIRS", json.dumps(REALISTIC_CONFIG["pairs"]))

    config = EngineConfig.load()

    assert set(config.endpoints) == {"databricks_prod", "qlik_acme"}
    assert len(config.pairs) == 1
    assert config.pairs[0].activation_opt_in is False


def test_file_values_take_precedence_over_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_realistic_secrets(monkeypatch)
    monkeypatch.setenv("ENDPOINTS", json.dumps({}))
    monkeypatch.setenv("PAIRS", json.dumps([]))

    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps(REALISTIC_CONFIG))

    config = EngineConfig.load(config_file=config_path)

    # The file supplied both fields, so the (empty) env values are never consulted.
    assert set(config.endpoints) == {"databricks_prod", "qlik_acme"}
    assert len(config.pairs) == 1


def test_pair_naming_a_nonexistent_source_endpoint_fails_clearly() -> None:
    with pytest.raises(ValidationError) as exc_info:
        EngineConfig(
            endpoints={"qlik_acme": {"connector": "qlik"}},
            pairs=[
                {
                    "name": "broken_pair",
                    "source": "does_not_exist",
                    "target": "qlik_acme",
                    "catalog_schema_patterns": ["prod.sales"],
                    "target_space": "Sales",
                    "entity_types": ["data_product"],
                }
            ],
        )

    message = str(exc_info.value)
    assert "broken_pair" in message
    assert "does_not_exist" in message
    assert "source" in message


def test_pair_naming_a_nonexistent_target_endpoint_fails_clearly() -> None:
    with pytest.raises(ValidationError) as exc_info:
        EngineConfig(
            endpoints={"databricks_prod": {"connector": "databricks"}},
            pairs=[
                {
                    "name": "broken_pair",
                    "source": "databricks_prod",
                    "target": "does_not_exist",
                    "catalog_schema_patterns": ["prod.sales"],
                    "target_space": "Sales",
                    "entity_types": ["data_product"],
                }
            ],
        )

    message = str(exc_info.value)
    assert "broken_pair" in message
    assert "does_not_exist" in message
    assert "target" in message


def test_qlik_as_source_is_rejected() -> None:
    """v1 is upstream-only: Qlik can never be a sync source."""
    with pytest.raises(ValidationError) as exc_info:
        EngineConfig(
            endpoints={
                "qlik_acme": {"connector": "qlik"},
                "qlik_other": {"connector": "qlik"},
            },
            pairs=[
                {
                    "name": "backwards_pair",
                    "source": "qlik_acme",
                    "target": "qlik_other",
                    "catalog_schema_patterns": ["prod.sales"],
                    "target_space": "Sales",
                    "entity_types": ["data_product"],
                }
            ],
        )

    message = str(exc_info.value)
    assert "backwards_pair" in message
    assert "qlik_acme" in message
    assert "upstream-only" in message


def test_non_qlik_target_is_rejected() -> None:
    """Qlik is the only write target in v1; a source-shaped target is a config error."""
    with pytest.raises(ValidationError) as exc_info:
        EngineConfig(
            endpoints={
                "databricks_prod": {"connector": "databricks"},
                "databricks_other": {"connector": "databricks"},
            },
            pairs=[
                {
                    "name": "wrong_target_pair",
                    "source": "databricks_prod",
                    "target": "databricks_other",
                    "catalog_schema_patterns": ["prod.sales"],
                    "target_space": "Sales",
                    "entity_types": ["data_product"],
                }
            ],
        )

    message = str(exc_info.value)
    assert "wrong_target_pair" in message
    assert "databricks_other" in message
    assert "write target" in message


def test_duplicate_pair_names_are_rejected() -> None:
    pair_kwargs = {
        "source": "databricks_prod",
        "target": "qlik_acme",
        "catalog_schema_patterns": ["prod.sales"],
        "target_space": "Sales",
        "entity_types": ["data_product"],
    }
    with pytest.raises(ValidationError) as exc_info:
        EngineConfig(
            endpoints={
                "databricks_prod": {"connector": "databricks"},
                "qlik_acme": {"connector": "qlik"},
            },
            pairs=[
                {"name": "same_name", **pair_kwargs},
                {"name": "same_name", **pair_kwargs},
            ],
        )

    assert "duplicate pair name" in str(exc_info.value)


def test_valid_direction_passes() -> None:
    config = EngineConfig(
        endpoints={
            "databricks_prod": {"connector": "databricks"},
            "qlik_acme": {"connector": "qlik"},
        },
        pairs=[
            {
                "name": "good_pair",
                "source": "databricks_prod",
                "target": "qlik_acme",
                "catalog_schema_patterns": ["prod.sales"],
                "target_space": "Sales",
                "entity_types": ["data_product"],
            }
        ],
    )
    assert config.pairs[0].name == "good_pair"


def test_resolve_credentials_missing_secret_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QLIK_ACME__CLIENT_SECRET", raising=False)
    config = EngineConfig(
        endpoints={
            "qlik_acme": {"connector": "qlik", "secrets": {"client_secret": "client_secret"}}
        }
    )

    with pytest.raises(SecretNotFoundError) as exc_info:
        config.resolve_credentials()

    assert exc_info.value.endpoint == "qlik_acme"
    assert exc_info.value.key == "client_secret"


def test_resolve_credentials_accepts_a_swapped_backend() -> None:
    class StaticBackend:
        def get_secret(self, *, endpoint: str, key: str) -> SecretStr:
            return SecretStr(f"{endpoint}:{key}:resolved")

    config = EngineConfig(
        endpoints={
            "qlik_acme": {"connector": "qlik", "secrets": {"client_secret": "client_secret"}},
        }
    )

    credentials = config.resolve_credentials(StaticBackend())

    assert (
        credentials["qlik_acme"]["client_secret"].get_secret_value()
        == "qlik_acme:client_secret:resolved"
    )


def test_environment_backend_is_the_default() -> None:
    backend = EnvironmentSecretBackend()
    assert callable(backend.get_secret)
