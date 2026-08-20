"""A bound logger emits sync context on every line and drops secrets, nested or not.

Uses ``structlog.testing.capture_logs`` with the exact processors this module wires in
``configure_logging`` (contextvars merge + the SDK's ``redact_secrets``) so the assertions run
against real processor behavior, not a mock of it — the same reuse-not-reimplementation the SDK
module (T1.7) documents for ``get_connector_logger``.
"""

from __future__ import annotations

import structlog

from qlabs_catalog_sync.observability import (
    REDACTION_TEST_PROCESSORS,
    bind_sync_context,
    get_logger,
)


def test_bound_context_appears_on_every_line_inside_the_block() -> None:
    with structlog.testing.capture_logs(processors=REDACTION_TEST_PROCESSORS) as entries:
        with bind_sync_context(pair="db_to_qlik", endpoint="qlik_acme", entity_type="dataset"):
            get_logger().info("read complete")
            get_logger().info("write complete")
        get_logger().info("outside the block")

    assert len(entries) == 3
    for entry in entries[:2]:
        assert entry["pair"] == "db_to_qlik"
        assert entry["endpoint"] == "qlik_acme"
        assert entry["entity_type"] == "dataset"
    assert "pair" not in entries[2]


def test_secret_placed_in_a_nested_structure_is_dropped() -> None:
    with (
        structlog.testing.capture_logs(processors=REDACTION_TEST_PROCESSORS) as entries,
        bind_sync_context(pair="db_to_qlik"),
    ):
        get_logger().info(
            "endpoint configured",
            config={
                "host": "acme.databricks.com",
                "auth": {"token": "sk-super-secret-value", "client_id": "abc-123"},
            },
        )

    (entry,) = entries
    assert entry["config"]["host"] == "acme.databricks.com"
    assert entry["config"]["auth"]["client_id"] == "abc-123"
    assert entry["config"]["auth"]["token"] != "sk-super-secret-value"
    assert "sk-super-secret-value" not in str(entry)


def test_bearer_token_embedded_in_a_plain_string_is_redacted() -> None:
    with structlog.testing.capture_logs(processors=REDACTION_TEST_PROCESSORS) as entries:
        get_logger().info("request failed", headers="Authorization: Bearer eyJhbGciOi.abc.def")

    (entry,) = entries
    assert "eyJhbGciOi" not in entry["headers"]
