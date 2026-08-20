"""DatabricksConfig: field validation, sql_warehouse_id presence/absence, secrecy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qlabs_connector_databricks.config import DatabricksConfig

from .conftest import build_config


def test_valid_config_round_trips_its_fields() -> None:
    config = build_config()

    assert config.host == "https://acme.cloud.databricks.com"
    assert config.client_id == "sp-client-abc"
    assert config.client_secret.get_secret_value() == "sp-secret-value"
    assert config.token_url == "https://acme.cloud.databricks.com/oidc/v1/token"


@pytest.mark.parametrize("missing", ["host", "client_id", "client_secret"])
def test_missing_required_field_is_rejected_clearly(missing: str) -> None:
    values = {
        "host": "https://acme.cloud.databricks.com",
        "client_id": "sp-client-abc",
        "client_secret": "sp-secret-value",
    }
    del values[missing]

    with pytest.raises(ValidationError) as exc_info:
        DatabricksConfig(**values)

    # pydantic-settings names the missing field in the error, so the rejection is
    # legible rather than a bare "invalid config".
    assert missing in str(exc_info.value)


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_config(unexpected_field="nope")


@pytest.mark.parametrize(
    "host", ["", "  ", "http://acme.cloud.databricks.com", "acme.databricks.com"]
)
def test_host_must_be_an_https_url(host: str) -> None:
    with pytest.raises(ValidationError):
        build_config(host=host)


def test_host_trailing_slash_is_normalized_away() -> None:
    config = build_config(host="https://acme.cloud.databricks.com/")
    assert config.host == "https://acme.cloud.databricks.com"


def test_sql_warehouse_id_absent_by_default() -> None:
    config = build_config()

    assert config.sql_warehouse_id is None
    assert config.has_sql_warehouse is False


def test_sql_warehouse_id_present_is_observable() -> None:
    config = build_config(sql_warehouse_id="warehouse-123")

    assert config.sql_warehouse_id == "warehouse-123"
    assert config.has_sql_warehouse is True


def test_blank_sql_warehouse_id_is_rejected_rather_than_silently_absent() -> None:
    with pytest.raises(ValidationError):
        build_config(sql_warehouse_id="   ")


def test_client_secret_never_appears_in_repr_or_str() -> None:
    config = build_config(client_secret="MARKER-CONFIG-SECRET-1a2b")

    assert "MARKER-CONFIG-SECRET-1a2b" not in repr(config)
    assert "MARKER-CONFIG-SECRET-1a2b" not in str(config)
    assert "MARKER-CONFIG-SECRET-1a2b" not in repr(config.model_dump())
