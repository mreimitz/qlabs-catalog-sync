"""``configure_logging`` end to end: real JSON lines land on the injected stream.

Unlike ``test_redaction_and_context_emit.py`` (which exercises the context/redaction
processors directly via ``capture_logs``), this drives the *actual* production pipeline —
``structlog.configure`` plus the stdlib ``logging`` bridge and JSON renderer — and parses what
comes out the other end, proving the whole thing is wired together correctly, not just its
pieces.
"""

from __future__ import annotations

import io
import json
import logging

from qlabs_catalog_sync.observability import bind_sync_context, configure_logging, get_logger


def test_emits_one_json_object_per_line_with_level_timestamp_context_and_no_secret() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)

    with bind_sync_context(pair="db_to_qlik", endpoint="qlik_acme"):
        get_logger("sync.loop").info("cycle complete", records_read=12, api_key="sk-do-not-leak")

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert record["event"] == "cycle complete"
    assert record["level"] == "info"
    assert "timestamp" in record
    assert record["pair"] == "db_to_qlik"
    assert record["endpoint"] == "qlik_acme"
    assert record["records_read"] == 12
    assert record["api_key"] != "sk-do-not-leak"
    assert "sk-do-not-leak" not in lines[0]


def test_respects_the_configured_level() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream, level=logging.WARNING)

    get_logger("sync.loop").info("should be suppressed")
    get_logger("sync.loop").warning("should appear")

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "should appear"


def test_calling_configure_logging_twice_does_not_duplicate_handlers() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    configure_logging(stream=stream)

    get_logger("sync.loop").info("single line please")

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
