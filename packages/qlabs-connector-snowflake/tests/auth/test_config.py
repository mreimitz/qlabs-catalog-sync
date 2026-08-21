"""SnowflakeConfig: field validation, identifier normalization, base_url derivation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qlabs_connector_snowflake.auth import SnowflakeConfig

from .conftest import build_config


def test_valid_config_round_trips_its_fields() -> None:
    config = build_config()

    assert config.organization == "acme"
    assert config.account == "primary"
    assert config.user == "svc_qlabs"
    assert config.role is None
    assert config.warehouse is None


@pytest.mark.parametrize("missing", ["organization", "account", "user", "private_key"])
def test_missing_required_field_is_rejected_clearly(
    missing: str, rsa_keypair, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ConnectorConfig is pydantic-settings BaseSettings: an unprefixed field name that
    # happens to match an ambient environment variable (`USER` is set in nearly every
    # Unix shell) would otherwise be silently filled from the environment instead of
    # actually being absent, so every candidate is cleared here regardless of which one
    # this parametrization is testing.
    for env_name in ("ORGANIZATION", "ACCOUNT", "USER", "PRIVATE_KEY"):
        monkeypatch.delenv(env_name, raising=False)
    values = {
        "organization": "acme",
        "account": "primary",
        "user": "svc_qlabs",
        "private_key": rsa_keypair.private_pem,
    }
    del values[missing]

    with pytest.raises(ValidationError) as exc_info:
        SnowflakeConfig(**values)

    assert missing in str(exc_info.value)


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_config(unexpected_field="nope")


@pytest.mark.parametrize("field", ["organization", "account", "user"])
def test_blank_identifier_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        build_config(**{field: "   "})


@pytest.mark.parametrize("field", ["role", "warehouse"])
def test_blank_optional_field_is_rejected_rather_than_silently_absent(field: str) -> None:
    with pytest.raises(ValidationError):
        build_config(**{field: "   "})


def test_role_and_warehouse_absent_by_default() -> None:
    config = build_config()

    assert config.role is None
    assert config.warehouse is None


def test_role_and_warehouse_present_are_observable() -> None:
    config = build_config(role="SYNC_ROLE", warehouse="SYNC_WH")

    assert config.role == "SYNC_ROLE"
    assert config.warehouse == "SYNC_WH"


def test_private_key_must_look_like_pem() -> None:
    with pytest.raises(ValidationError, match="PEM"):
        build_config(private_key="not-a-pem-key")


def test_blank_private_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_config(private_key="   ")


def test_base_url_is_derived_from_organization_and_account() -> None:
    config = build_config(organization="Acme", account="Primary")

    assert config.resolved_base_url == "https://acme-primary.snowflakecomputing.com"


def test_base_url_override_is_used_verbatim_when_given() -> None:
    config = build_config(base_url="https://custom.example.snowflakecomputing.com/")

    # Trailing slash is normalized away, matching the Databricks host validator's
    # equivalent behavior.
    assert config.resolved_base_url == "https://custom.example.snowflakecomputing.com"


def test_base_url_override_must_be_https() -> None:
    with pytest.raises(ValidationError):
        build_config(base_url="http://insecure.example.com")


def test_blank_base_url_override_is_rejected_rather_than_silently_derived() -> None:
    with pytest.raises(ValidationError):
        build_config(base_url="   ")


def test_account_identifier_is_upper_cased_org_hyphen_account() -> None:
    config = build_config(organization="acme", account="primary")

    assert config.account_identifier == "ACME-PRIMARY"


def test_account_identifier_upper_cases_mixed_case_input() -> None:
    config = build_config(organization="Acme", account="Primary")

    assert config.account_identifier == "ACME-PRIMARY"


def test_user_identifier_is_upper_cased() -> None:
    config = build_config(user="svc_qlabs")

    assert config.user_identifier == "SVC_QLABS"
