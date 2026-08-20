"""The secret-redaction structlog processor, and the bound connector logger."""

from __future__ import annotations

import json
import logging

import pytest
from pydantic import SecretBytes, SecretStr

from qlabs_catalog_sync_sdk.logging import REDACTED, get_connector_logger, redact_secrets


def test_redacts_known_secret_keys_at_the_top_level() -> None:
    event = {
        "event": "token issued",
        "token": "sk-abc123",
        "password": "hunter2",
        "client_secret": "cs-xyz",
        "api_key": "ak-123",
        "ok": True,
    }

    result = redact_secrets(None, "info", dict(event))

    assert result["token"] == REDACTED
    assert result["password"] == REDACTED
    assert result["client_secret"] == REDACTED
    assert result["api_key"] == REDACTED
    assert result["ok"] is True
    assert result["event"] == "token issued"


def test_redacts_secrets_nested_inside_dicts_and_lists() -> None:
    """Redaction that only works on top-level keys is not good enough."""
    event = {
        "endpoint": "qlik",
        "request": {
            "headers": {"Authorization": "Bearer abc.def.ghi", "Accept": "application/json"},
            "body": {"nested": {"password": "hunter2", "note": "keep me"}},
        },
        "batch": [{"api_key": "ak-1"}, {"note": "keep me too"}],
    }

    result = redact_secrets(None, "info", event)

    assert result["request"]["headers"]["Authorization"] == REDACTED
    assert result["request"]["headers"]["Accept"] == "application/json"
    assert result["request"]["body"]["nested"]["password"] == REDACTED
    assert result["request"]["body"]["nested"]["note"] == "keep me"
    assert result["batch"][0]["api_key"] == REDACTED
    assert result["batch"][1]["note"] == "keep me too"
    assert result["endpoint"] == "qlik"


def test_redacts_secretstr_and_secretbytes_values() -> None:
    event = {
        "endpoint": "qlik",
        "token_value": SecretStr("sk-abc123"),
        "nested": {"key_bytes": SecretBytes(b"raw-bytes-secret")},
    }

    result = redact_secrets(None, "info", event)

    assert result["token_value"] == REDACTED
    assert result["nested"]["key_bytes"] == REDACTED


def test_redacts_a_bearer_token_embedded_in_a_plain_string() -> None:
    event = {
        "endpoint": "qlik",
        "raw_header_line": "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "curl_repr": "curl -H 'Authorization: Basic dXNlcjpwYXNz' https://example.com",
        "note": "no credential here",
    }

    result = redact_secrets(None, "info", event)

    assert "eyJhbGciOiJIUzI1NiJ9" not in result["raw_header_line"]
    assert "Bearer" in result["raw_header_line"]
    assert REDACTED in result["raw_header_line"]
    assert "dXNlcjpwYXNz" not in result["curl_repr"]
    assert result["note"] == "no credential here"


def test_leaves_non_secret_context_untouched() -> None:
    event = {"endpoint": "qlik", "tenant": "acme", "entity_type": "dataset", "count": 3}

    result = redact_secrets(None, "info", dict(event))

    assert result == event


def test_bound_logger_redacts_and_keeps_bound_context(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="qlabs.connector.qlik")

    logger = get_connector_logger(endpoint="qlik", tenant="acme")
    logger.info(
        "wrote data product",
        headers={"Authorization": "Bearer secret-token"},
        client_secret="cs-xyz",
        neutral_id="dp-1",
    )

    assert len(caplog.records) == 1
    payload = json.loads(caplog.records[0].message)

    # Bound context survives the round trip through the processor chain.
    assert payload["endpoint"] == "qlik"
    assert payload["tenant"] == "acme"
    # Per-call context is preserved too.
    assert payload["neutral_id"] == "dp-1"
    assert payload["message"] == "wrote data product"
    # And secrets never made it into the rendered output.
    assert payload["headers"]["Authorization"] == REDACTED
    assert payload["client_secret"] == REDACTED
    assert "secret-token" not in caplog.text
    assert "cs-xyz" not in caplog.text


def test_bound_logger_scopes_context_to_the_bind_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two independently-bound loggers must not leak each other's context."""
    caplog.set_level(logging.INFO)

    qlik_logger = get_connector_logger(endpoint="qlik", tenant="acme")
    databricks_logger = get_connector_logger(endpoint="databricks", tenant="acme")

    qlik_logger.info("qlik event")
    databricks_logger.info("databricks event")

    payloads = [json.loads(record.message) for record in caplog.records]
    qlik_payload = next(p for p in payloads if p["message"] == "qlik event")
    databricks_payload = next(p for p in payloads if p["message"] == "databricks event")

    assert qlik_payload["endpoint"] == "qlik"
    assert databricks_payload["endpoint"] == "databricks"
