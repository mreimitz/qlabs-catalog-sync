"""Shared fixtures and Statement Execution API response builders for the
``sql_tags.py`` tests (T4.7).

Mirrors the pattern already established by ``tests/read/conftest.py`` and
``tests/changes/conftest.py``: an :class:`HttpEndpoint` factory wired for fast,
deterministic tests (no real sleeping, small retry counts), plus builders shaped like
real Databricks Statement Execution API responses (RS-01 section 3.1) so each test file
stays focused on the behavior it proves rather than JSON scaffolding.

Two statement ids (``SCHEMA_STMT_ID``/``TABLE_STMT_ID``) are used by default across the
whole suite so a test can register distinct ``GET .../statements/{id}`` routes for the
``SCHEMA_TAGS`` poll and the ``TABLE_TAGS`` poll without depending on call ordering
between them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from qlabs_catalog_sync_sdk.config import ManualClock
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.sql_tags import STATEMENTS_PATH

BASE_URL = "https://acme.cloud.databricks.com"
ENDPOINT = "databricks"

SCHEMA_STMT_ID = "stmt-schema-tags"
TABLE_STMT_ID = "stmt-table-tags"


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
async def make_http() -> AsyncIterator[Callable[..., HttpEndpoint]]:
    """Factory for a test :class:`HttpEndpoint`: no real sleeping, small bounded
    attempt counts, so a retries-exhausted test stays fast. Endpoints built through
    the factory are closed automatically at teardown."""
    made: list[HttpEndpoint] = []

    def _make(**overrides: object) -> HttpEndpoint:
        kwargs: dict[str, object] = dict(
            sleep=_no_sleep,
            max_attempts=3,
            backoff_base_seconds=0.001,
            backoff_max_seconds=0.005,
        )
        kwargs.update(overrides)
        endpoint = HttpEndpoint(BASE_URL, **kwargs)  # type: ignore[arg-type]
        made.append(endpoint)
        return endpoint

    yield _make
    for endpoint in made:
        await endpoint.aclose()


@pytest.fixture
def http(make_http: Callable[..., HttpEndpoint]) -> HttpEndpoint:
    """The common case: a default-configured endpoint."""
    return make_http()


@pytest.fixture
def manual_clock() -> ManualClock:
    """A :class:`ManualClock` so poll-wait tests never really sleep and can assert
    exactly how many polls happened and for how long, deterministically."""
    return ManualClock()


def statement_url(statement_id: str) -> str:
    return f"{BASE_URL}{STATEMENTS_PATH}/{statement_id}"


def statements_url() -> str:
    return f"{BASE_URL}{STATEMENTS_PATH}"


def pending_response(statement_id: str) -> dict[str, Any]:
    """A Statement Execution API response for a statement still running."""
    return {"statement_id": statement_id, "status": {"state": "PENDING"}}


def running_response(statement_id: str) -> dict[str, Any]:
    return {"statement_id": statement_id, "status": {"state": "RUNNING"}}


def succeeded_response(
    statement_id: str,
    *,
    rows: list[list[Any]] | None = None,
    next_chunk_internal_link: str | None = None,
) -> dict[str, Any]:
    """A terminal ``SUCCEEDED`` response, ``INLINE``/``JSON_ARRAY`` shaped."""
    result: dict[str, Any] = {"data_array": rows if rows is not None else []}
    if next_chunk_internal_link is not None:
        result["next_chunk_internal_link"] = next_chunk_internal_link
    return {"statement_id": statement_id, "status": {"state": "SUCCEEDED"}, "result": result}


def chunk_response(
    *, rows: list[list[Any]], next_chunk_internal_link: str | None = None
) -> dict[str, Any]:
    """A ``GET .../result/chunks/{n}`` response body."""
    body: dict[str, Any] = {"data_array": rows}
    if next_chunk_internal_link is not None:
        body["next_chunk_internal_link"] = next_chunk_internal_link
    return body


def failed_response(
    statement_id: str,
    *,
    state: str = "FAILED",
    error_code: str = "INTERNAL_ERROR",
    error_message: str = "the query failed",
) -> dict[str, Any]:
    """A terminal failure response (``FAILED``, or ``CANCELED``/``CLOSED`` with no
    ``error`` body, matching what Databricks emits for those two states)."""
    status: dict[str, Any] = {"state": state}
    if state == "FAILED":
        status["error"] = {"error_code": error_code, "message": error_message}
    return {"statement_id": statement_id, "status": status}


def schema_tag_row(
    catalog_name: str, schema_name: str, tag_name: str, tag_value: str | None
) -> list[Any]:
    """One ``SCHEMA_TAGS`` row, in the exact column order ``sql_tags.py`` selects:
    ``catalog_name, schema_name, tag_name, tag_value``."""
    return [catalog_name, schema_name, tag_name, tag_value]


def table_tag_row(
    catalog_name: str,
    schema_name: str,
    table_name: str,
    tag_name: str,
    tag_value: str | None,
) -> list[Any]:
    """One ``TABLE_TAGS`` row: ``catalog_name, schema_name, table_name, tag_name,
    tag_value``."""
    return [catalog_name, schema_name, table_name, tag_name, tag_value]
