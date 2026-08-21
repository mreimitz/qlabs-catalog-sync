"""Databricks UC tag read path over the Statement Execution API (decision D6).

WP4 / T4.7. Unity Catalog object tags have no REST read-back at all (RS-01 section
1.3: "Read-back: ``INFORMATION_SCHEMA.CATALOG_TAGS``, ``SCHEMA_TAGS``, ``TABLE_TAGS``,
...") -- the only way to read them is SQL against ``INFORMATION_SCHEMA.SCHEMA_TAGS``
and ``INFORMATION_SCHEMA.TABLE_TAGS``, run over the Statement Execution API
(``POST /api/2.0/sql/statements``, RS-01 section 3.1), which needs a SQL warehouse.
This module is the read half of decision D6; ``manifest.py`` (T4.2) is the other
half -- it declares ``tags``/``classifications`` ``ro`` only when
:attr:`~qlabs_connector_databricks.config.DatabricksConfig.has_sql_warehouse` is true
and ``na`` otherwise. The two must never disagree: this module makes that concrete by
refusing to make any HTTP call at all when no warehouse id is given.

## Structural "unavailable" vs. "no tags"

``read_catalog_tags``/``read_tags_for_catalogs`` return ``None`` when
``sql_warehouse_id`` is ``None`` -- not an empty :class:`CatalogTagIndex`. An empty
result would be indistinguishable from "this catalog's objects genuinely have zero
tags", which is exactly the ambiguity D6 exists to avoid (decision-databricks-to-qlik-
mvp.md item 6: "an honest ``na`` beats a silently empty read" -- manifest.py's own
words for the same point). Every object-level accessor
(:meth:`CatalogTagIndex.for_schema`/:meth:`CatalogTagIndex.for_table`) can still return
``[]`` for one object that simply has no tags *once the index exists* -- that is a real
answer, not a stand-in for "unavailable". The wiring layer (out of this module's owned
paths -- see this task's report) is expected to preserve the distinction by not adding
a ``tags`` field envelope at all when the whole index is ``None``, the same way
``read.py`` already omits an envelope for every field it does not populate.

## Batching: one statement pair per catalog, never per object

``INFORMATION_SCHEMA`` is scoped to one catalog (RS-01 section 1.3/3.3 -- there is no
account-wide or cross-catalog tag table), so a query is inherently per-catalog. Given
that, :func:`read_catalog_tags` reads **the whole catalog's** ``SCHEMA_TAGS`` and
``TABLE_TAGS`` in exactly two statements (one each), then groups the rows client-side
by object ``full_name`` in Python. A data product (one UC schema) with 200 tables costs
the same two statements as a data product with two tables -- the row count grows, the
statement count does not. :func:`read_tags_for_catalogs` extends this across several
catalogs by issuing one such pair per catalog (the minimum possible given
``INFORMATION_SCHEMA``'s per-catalog scope), not per schema and not per table.

An optional ``schema_names`` filter (a parameterized ``WHERE schema_name IN (...)``,
see "Identifier and value safety" below) lets a caller that already knows exactly which
schemas it needs shrink the result set without giving up the per-catalog batching --
still two statements, just over fewer rows.

## Polling: the Statement Execution API is asynchronous by nature

Every statement is submitted with ``wait_timeout: "0s"`` -- fully asynchronous --
so the ``POST`` response never itself carries the result; it only ever returns
``PENDING`` (or, in principle, a state that is already terminal). The only way to learn
the outcome is to poll ``GET /api/2.0/sql/statements/{statement_id}`` until
``status.state`` leaves ``{PENDING, RUNNING}`` for a terminal state (``SUCCEEDED``,
``FAILED``, ``CANCELED`` or ``CLOSED``). Submitting with a positive ``wait_timeout``
instead (Databricks holds the HTTP connection open and *sometimes* returns the result
inline) would make the polling path exercise only when the warehouse happens to be
slow -- an "assume the first response has the rows" bug would then pass against a
cooperative mock and fail unpredictably against a real, cold warehouse. Forcing
``wait_timeout: "0s"`` makes the poll loop the *only* path, in tests and in production
alike.

The wait between polls is a fixed interval (``poll_interval_seconds``, default
:data:`DEFAULT_POLL_INTERVAL_SECONDS`) taken through the SDK's ``Clock`` protocol
(``qlabs_catalog_sync_sdk.config.Clock``/``SystemClock``/``ManualClock``) rather than
``asyncio.sleep`` directly, so a test can inject a ``ManualClock`` and assert both that
the code waited and for how long, with the whole test running instantly -- no real
sleeping. The wait is bounded by attempt count (``max_poll_attempts``, default
:data:`DEFAULT_MAX_POLL_ATTEMPTS`) rather than a wall-clock deadline: bounding by count
is exact and trivially deterministic under a ``ManualClock`` (assert the poll count),
where a wall-clock deadline would need the test to reason about elapsed time instead.
Exceeding the bound raises :class:`~qlabs_catalog_sync_sdk.exceptions.TransientError`
-- a cold warehouse that is still starting is exactly the kind of failure a later
attempt might not repeat, so "retry later" is the honest reaction, matching how this
connector treats every other timing-shaped failure. This module deliberately does not
also try to cancel the still-running statement server-side on timeout: doing so adds an
extra fallible HTTP call to an already-failing path for a resource Databricks reclaims
on its own, and would risk masking the real timeout behind a secondary error.

## Identifier and value safety

``catalog_name`` is interpolated directly into the SQL text (``` `catalog_name`.
INFORMATION_SCHEMA.SCHEMA_TAGS ```) because **SQL has no bind-parameter syntax for an
identifier in a ``FROM`` clause** -- parameters stand in for values, never for table/
schema names. Since it cannot be parameterized, it is validated instead, against
:data:`_IDENTIFIER_RE` (letters, digits, underscore only) *before any HTTP call*
(:class:`IdentifierError`) -- a catalog name that fails this check is refused outright
rather than concatenated. ``schema_names`` (used only to narrow the result set) *is* a
value, not an identifier, so it goes through the Statement Execution API's own
``parameters`` array (bind values, ``WHERE schema_name IN (:s0, :s1, ...)``) instead of
being interpolated at all -- the safer path is used exactly where the API supports it.

``catalog_name`` in practice always originates from a Unity Catalog object Databricks
itself already accepted (``read.py``'s ``catalog_name``/``full_name`` fields, or an
operator's own config), so a validation failure here is not expected in steady state --
it is a defense-in-depth guard, not a filter this module expects to reject real traffic
through.

## Tag semantics preserved faithfully

* **Key/value, not key-and-default-value.** ``tag_value`` comes back as JSON ``null``
  for a key-only tag (SQL ``NULL``) and as ``""`` for a tag explicitly set to an empty
  string -- these map to :attr:`~qlabs_catalog_sync_sdk.models.Tag.value` being
  ``None`` vs. ``""`` respectively, never collapsed into each other.
* **Case-sensitive keys.** ``tag_name`` is carried through verbatim -- this module
  never upper/lower-cases it.
* **50-tags-per-object / 256-char key-and-value limits** are Databricks' own write-time
  constraints (RS-01 section 1.3); this module does not re-enforce them on read, it
  trusts what ``INFORMATION_SCHEMA`` returns.

## Assumptions -- ``TENANT_UNVERIFIED`` candidates

Nothing here has been run against a live workspace (RM-01 "no live tenants" convention
-- decision-databricks-to-qlik-mvp.md, agent-guide.md). The specific things assumed,
that a real tenant should confirm (see T8.6's registry):

1. **``INFORMATION_SCHEMA.SCHEMA_TAGS`` columns**: ``catalog_name``, ``schema_name``,
   ``tag_name``, ``tag_value``. RS-01 section 1.3 names the table but not its columns.
2. **``INFORMATION_SCHEMA.TABLE_TAGS`` columns**: ``catalog_name``, ``schema_name``,
   ``table_name``, ``tag_name``, ``tag_value``. Same caveat.
3. **Statement Execution API response shape**: ``status.state``, ``status.error.
   error_code``/``.message``, ``result.data_array``, and multi-chunk continuation via
   ``result.next_chunk_internal_link`` (followed with a plain ``GET``). RS-01 documents
   the endpoint's existence (section 3.1) but not its response schema in this detail.
4. **``status.error.error_code`` vocabulary for permission failures** (assumed to
   contain one of ``PERMISSION``/``UNAUTHENTICATED``/``FORBIDDEN``/``ACCESS_DENIED``,
   see :func:`_looks_like_auth_failure`) -- used only to route a ``FAILED`` statement to
   :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError` instead of
   :class:`~qlabs_catalog_sync_sdk.exceptions.TransientError`; RS-01 does not enumerate
   Statement Execution error codes.
5. **Unity Catalog identifier charset**: RS-01 pins the *tag key* charset (no
   ``. , - = / :``) but not the catalog/schema/table identifier charset. This module's
   validator (letters, digits, underscore) is deliberately conservative -- narrower
   than what Unity Catalog may actually permit -- so it is safe by construction rather
   than exactly permissive; the manifest's own conformance requires no more.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, NoReturn

import httpx
import structlog

from qlabs_catalog_sync_sdk.config import Clock, SystemClock
from qlabs_catalog_sync_sdk.exceptions import AuthError, ConnectorError, TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import Tag

__all__ = [
    "DEFAULT_MAX_POLL_ATTEMPTS",
    "DEFAULT_MAX_RESULT_CHUNKS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "STATEMENTS_PATH",
    "CatalogTagIndex",
    "IdentifierError",
    "read_catalog_tags",
    "read_tags_for_catalogs",
]

_logger = structlog.get_logger("qlabs_connector_databricks.sql_tags")

STATEMENTS_PATH: Final[str] = "/api/2.0/sql/statements"

DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 2.0
DEFAULT_MAX_POLL_ATTEMPTS: Final[int] = 60
DEFAULT_MAX_RESULT_CHUNKS: Final[int] = 50

# Statement Execution API terminal/non-terminal states (RS-01 section 3.1; exact
# vocabulary confirmed by the task brief, not by a live tenant -- see module docstring
# assumption 3).
_PENDING_STATES: Final[frozenset[str]] = frozenset({"PENDING", "RUNNING"})
_TERMINAL_STATES: Final[frozenset[str]] = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"})

# Unity Catalog identifier whitelist -- see module docstring "Identifier and value
# safety". Deliberately conservative: letters, digits, underscore only.
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_]+$")

# TENANT_UNVERIFIED (module docstring assumption 1/2): exact INFORMATION_SCHEMA column
# names, in the order this module selects them (so a raw result row can be indexed
# positionally without re-parsing the manifest's column list).
_SCHEMA_TAGS_COLUMNS: Final[tuple[str, ...]] = (
    "catalog_name",
    "schema_name",
    "tag_name",
    "tag_value",
)
_TABLE_TAGS_COLUMNS: Final[tuple[str, ...]] = (
    "catalog_name",
    "schema_name",
    "table_name",
    "tag_name",
    "tag_value",
)

# TENANT_UNVERIFIED (module docstring assumption 4): error_code substrings taken as
# "this FAILED statement is actually a permission problem".
_AUTH_ERROR_CODE_MARKERS: Final[tuple[str, ...]] = (
    "PERMISSION",
    "UNAUTHENTICATED",
    "FORBIDDEN",
    "ACCESS_DENIED",
)


class IdentifierError(ValueError):
    """A catalog identifier failed validation before any HTTP call was made.

    Deliberately **not** part of the SDK's
    :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError` hierarchy: it guards an
    internal invariant (an identifier this module is about to interpolate into a SQL
    ``FROM`` clause must be provably safe) rather than reporting an operational failure
    against the endpoint, so it does not need a ``retryable`` verdict or an ``endpoint``/
    ``cause`` -- there is no request to retry and nothing was sent. See the module
    docstring's "Identifier and value safety" section.
    """


@dataclass(frozen=True, slots=True)
class CatalogTagIndex:
    """UC tags for every schema and table in one catalog, read in exactly two
    Statement Execution API statements regardless of how many objects the catalog
    holds -- see the module docstring's "Batching" section.

    ``schema_tags``/``table_tags`` are keyed by the object's ``full_name``
    (``catalog.schema`` / ``catalog.schema.table``), matching the identity ``read.py``
    already carries in ``IdentityRef.secondary_keys["full_name"]``, so a caller can look
    a tag list up by exactly the string it already has on hand.
    """

    catalog_name: str
    schema_tags: Mapping[str, tuple[Tag, ...]]
    table_tags: Mapping[str, tuple[Tag, ...]]

    def for_schema(self, full_name: str) -> list[Tag]:
        """Tags for the schema ``full_name`` (``catalog.schema``).

        ``[]`` means "read, and genuinely has no tags" -- a real answer, not a stand-in
        for "unavailable". "Unavailable" is a property of the whole index (``None``
        from :func:`read_catalog_tags`/:func:`read_tags_for_catalogs`), never of one
        object within an index that exists.
        """
        return list(self.schema_tags.get(full_name, ()))

    def for_table(self, full_name: str) -> list[Tag]:
        """Tags for the table/view ``full_name`` (``catalog.schema.table``). See
        :meth:`for_schema` for the ``[]``-vs-``None`` distinction."""
        return list(self.table_tags.get(full_name, ()))


# ----------------------------------------------------------------------------------
# Public entry points
# ----------------------------------------------------------------------------------


async def read_catalog_tags(
    http: HttpEndpoint,
    *,
    sql_warehouse_id: str | None,
    catalog_name: str,
    endpoint: str,
    schema_names: Sequence[str] | None = None,
    clock: Clock | None = None,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_poll_attempts: int = DEFAULT_MAX_POLL_ATTEMPTS,
    max_result_chunks: int = DEFAULT_MAX_RESULT_CHUNKS,
) -> CatalogTagIndex | None:
    """Read every ``SCHEMA_TAGS``/``TABLE_TAGS`` row for one catalog (decision D6).

    Returns ``None`` -- and makes **no** HTTP call -- when ``sql_warehouse_id`` is
    ``None``. Pass ``ctx.config.sql_warehouse_id``
    (:class:`~qlabs_connector_databricks.config.DatabricksConfig`) directly; there is no
    need to also consult ``has_sql_warehouse`` first, ``None`` means exactly what that
    property reports.

    Raises :class:`IdentifierError` if ``catalog_name`` is not a safe SQL identifier,
    *before* any HTTP call (see module docstring). Raises
    :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError` /
    :class:`~qlabs_catalog_sync_sdk.exceptions.TransientError` for HTTP and statement
    failures (see :func:`_raise_for_status`/:func:`_raise_for_statement_failure`).
    """
    if sql_warehouse_id is None:
        return None
    _validate_catalog_identifier(catalog_name)
    active_clock: Clock = clock if clock is not None else SystemClock()

    schema_sql, schema_params = _build_tags_statement(
        catalog_name=catalog_name,
        table="SCHEMA_TAGS",
        columns=_SCHEMA_TAGS_COLUMNS,
        order_by=("schema_name", "tag_name"),
        schema_names=schema_names,
    )
    schema_rows = await _run_statement(
        http,
        warehouse_id=sql_warehouse_id,
        statement=schema_sql,
        parameters=schema_params,
        endpoint=endpoint,
        clock=active_clock,
        poll_interval_seconds=poll_interval_seconds,
        max_poll_attempts=max_poll_attempts,
        max_result_chunks=max_result_chunks,
    )

    table_sql, table_params = _build_tags_statement(
        catalog_name=catalog_name,
        table="TABLE_TAGS",
        columns=_TABLE_TAGS_COLUMNS,
        order_by=("schema_name", "table_name", "tag_name"),
        schema_names=schema_names,
    )
    table_rows = await _run_statement(
        http,
        warehouse_id=sql_warehouse_id,
        statement=table_sql,
        parameters=table_params,
        endpoint=endpoint,
        clock=active_clock,
        poll_interval_seconds=poll_interval_seconds,
        max_poll_attempts=max_poll_attempts,
        max_result_chunks=max_result_chunks,
    )

    _logger.info(
        "databricks.sql_tags.catalog_read",
        endpoint=endpoint,
        catalog=catalog_name,
        schema_tag_rows=len(schema_rows),
        table_tag_rows=len(table_rows),
    )
    return CatalogTagIndex(
        catalog_name=catalog_name,
        schema_tags=_group_schema_tag_rows(schema_rows),
        table_tags=_group_table_tag_rows(table_rows),
    )


async def read_tags_for_catalogs(
    http: HttpEndpoint,
    *,
    sql_warehouse_id: str | None,
    catalog_names: Sequence[str],
    endpoint: str,
    clock: Clock | None = None,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_poll_attempts: int = DEFAULT_MAX_POLL_ATTEMPTS,
    max_result_chunks: int = DEFAULT_MAX_RESULT_CHUNKS,
) -> Mapping[str, CatalogTagIndex] | None:
    """:func:`read_catalog_tags` for several catalogs at once.

    ``INFORMATION_SCHEMA`` is scoped to one catalog (module docstring "Batching"), so
    reading tags across catalogs is inherently one statement pair per catalog -- the
    minimum possible, never one per schema or table. Returns ``None`` -- with no HTTP
    call for any catalog -- when ``sql_warehouse_id`` is ``None``, checked once up
    front rather than relying solely on each per-catalog call's own short-circuit.
    """
    if sql_warehouse_id is None:
        return None
    active_clock: Clock = clock if clock is not None else SystemClock()
    indexes: dict[str, CatalogTagIndex] = {}
    for catalog_name in catalog_names:
        index = await read_catalog_tags(
            http,
            sql_warehouse_id=sql_warehouse_id,
            catalog_name=catalog_name,
            endpoint=endpoint,
            clock=active_clock,
            poll_interval_seconds=poll_interval_seconds,
            max_poll_attempts=max_poll_attempts,
            max_result_chunks=max_result_chunks,
        )
        assert index is not None  # sql_warehouse_id is bound (not None) in this branch
        indexes[catalog_name] = index
    return indexes


# ----------------------------------------------------------------------------------
# Identifier validation
# ----------------------------------------------------------------------------------


def _validate_catalog_identifier(catalog_name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(catalog_name):
        raise IdentifierError(
            f"catalog name {catalog_name!r} is not a safe SQL identifier (expected "
            "only ASCII letters, digits and underscores); refusing to interpolate it "
            "into a Statement Execution API query"
        )
    return catalog_name


# ----------------------------------------------------------------------------------
# Statement building
# ----------------------------------------------------------------------------------


def _build_tags_statement(
    *,
    catalog_name: str,
    table: str,
    columns: Sequence[str],
    order_by: Sequence[str],
    schema_names: Sequence[str] | None,
) -> tuple[str, list[dict[str, str]]]:
    """Build one ``SELECT ... FROM <catalog>.INFORMATION_SCHEMA.<table>`` statement.

    ``catalog_name`` must already be validated by :func:`_validate_catalog_identifier`
    -- this function does not re-check it, it only assembles text. ``schema_names``, if
    given, becomes a parameterized ``WHERE schema_name IN (:s0, :s1, ...)`` -- values,
    never interpolated (module docstring "Identifier and value safety").
    """
    column_list = ", ".join(columns)
    sql = f"SELECT {column_list} FROM `{catalog_name}`.INFORMATION_SCHEMA.{table}"
    parameters: list[dict[str, str]] = []
    if schema_names is not None:
        placeholders: list[str] = []
        for index, name in enumerate(schema_names):
            marker = f"s{index}"
            placeholders.append(f":{marker}")
            parameters.append({"name": marker, "value": name, "type": "STRING"})
        sql += f" WHERE schema_name IN ({', '.join(placeholders)})"
    sql += f" ORDER BY {', '.join(order_by)}"
    return sql, parameters


# ----------------------------------------------------------------------------------
# Statement submission + polling
# ----------------------------------------------------------------------------------


def _state_of(payload: Mapping[str, Any]) -> str:
    status = payload.get("status")
    if isinstance(status, Mapping):
        return str(status.get("state", "UNKNOWN"))
    return "UNKNOWN"


async def _run_statement(
    http: HttpEndpoint,
    *,
    warehouse_id: str,
    statement: str,
    parameters: Sequence[Mapping[str, str]],
    endpoint: str,
    clock: Clock,
    poll_interval_seconds: float,
    max_poll_attempts: int,
    max_result_chunks: int,
) -> list[list[Any]]:
    """Submit one statement (always fully async, ``wait_timeout: "0s"``) and poll it
    to a terminal state, then return its materialized rows.

    See module docstring "Polling" for why ``wait_timeout`` is always ``"0s"`` and why
    the bound is an attempt count rather than a wall-clock deadline.
    """
    body: dict[str, Any] = {
        "warehouse_id": warehouse_id,
        "statement": statement,
        "wait_timeout": "0s",
        "format": "JSON_ARRAY",
        "disposition": "INLINE",
    }
    if parameters:
        body["parameters"] = list(parameters)

    payload = await _post(http, STATEMENTS_PATH, json=body, endpoint=endpoint)
    statement_id = str(payload.get("statement_id") or "<unknown>")
    state = _state_of(payload)

    attempts = 0
    while state in _PENDING_STATES:
        attempts += 1
        if attempts > max_poll_attempts:
            raise TransientError(
                f"statement {statement_id} did not reach a terminal state within "
                f"{max_poll_attempts} poll attempts "
                f"({max_poll_attempts * poll_interval_seconds:.0f}s bounded wait)",
                endpoint=endpoint,
            )
        await clock.sleep(poll_interval_seconds)
        payload = await _get(http, f"{STATEMENTS_PATH}/{statement_id}", endpoint=endpoint)
        state = _state_of(payload)

    if state != "SUCCEEDED":
        _raise_for_statement_failure(payload, endpoint=endpoint, statement_id=statement_id)

    return await _collect_rows(
        http,
        payload,
        endpoint=endpoint,
        statement_id=statement_id,
        max_chunks=max_result_chunks,
    )


async def _collect_rows(
    http: HttpEndpoint,
    payload: Mapping[str, Any],
    *,
    endpoint: str,
    statement_id: str,
    max_chunks: int,
) -> list[list[Any]]:
    """Materialize every row of a ``SUCCEEDED`` statement's ``INLINE``/``JSON_ARRAY``
    result, following ``result.next_chunk_internal_link`` (TENANT_UNVERIFIED, module
    docstring assumption 3) until it is absent, bounded defensively at
    ``max_chunks`` -- mirrors ``read.py``'s ``_paginate_defensively`` bound.
    """
    result = payload.get("result")
    result = result if isinstance(result, Mapping) else {}
    rows: list[list[Any]] = list(result.get("data_array") or [])
    next_link = result.get("next_chunk_internal_link")

    chunk_count = 0
    while isinstance(next_link, str) and next_link:
        chunk_count += 1
        if chunk_count > max_chunks:
            raise TransientError(
                f"statement {statement_id} result did not terminate within {max_chunks} chunks",
                endpoint=endpoint,
            )
        chunk_payload = await _get(http, next_link, endpoint=endpoint)
        rows.extend(chunk_payload.get("data_array") or [])
        next_link = chunk_payload.get("next_chunk_internal_link")
    return rows


# ----------------------------------------------------------------------------------
# HTTP + statement error translation
# ----------------------------------------------------------------------------------


def _try_parse_retry_after(value: str | None) -> float | None:
    """Best-effort ``Retry-After`` parse (delta-seconds form only) -- mirrors
    ``read.py``'s identical helper. By the time this module sees an
    ``httpx.HTTPStatusError``, :class:`HttpEndpoint` has already retried a 429/5xx and
    honored ``Retry-After`` internally; this is only surfaced onward as a hint."""
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def _raise_for_status(exc: httpx.HTTPStatusError, *, endpoint: str) -> NoReturn:
    status = exc.response.status_code
    if status in (401, 403):
        raise AuthError(
            f"authentication/authorization failed calling the Statement Execution API "
            f"(HTTP {status})",
            endpoint=endpoint,
            cause=exc,
        ) from exc
    # 429/5xx (retries already exhausted by HttpEndpoint) and any other unexpected
    # status: the honest fallback is "this attempt failed", not "the object is gone" or
    # "the plan was wrong" -- mirrors read.py's/auth.py's identical philosophy.
    raise TransientError(
        f"Statement Execution API request failed with HTTP {status}",
        endpoint=endpoint,
        cause=exc,
        retry_after_seconds=_try_parse_retry_after(exc.response.headers.get("retry-after")),
    ) from exc


def _looks_like_auth_failure(error_code: str) -> bool:
    upper = error_code.upper()
    return any(marker in upper for marker in _AUTH_ERROR_CODE_MARKERS)


def _raise_for_statement_failure(
    payload: Mapping[str, Any], *, endpoint: str, statement_id: str
) -> NoReturn:
    """A statement that reached a terminal state other than ``SUCCEEDED``.

    This is not an HTTP-layer failure -- ``HttpEndpoint`` already retried anything
    retryable at that layer to get this far -- it is the *query itself* failing, being
    canceled, or (unexpectedly) already closed. A permission-shaped ``error_code``
    (module docstring assumption 4) maps to
    :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError`, matching how a 401/403 REST
    response maps elsewhere in this connector (``auth.py``'s
    ``translate_databricks_error``, ``read.py``'s ``_raise_for_status``). Everything
    else -- an unrecognized query error, ``CANCELED``, ``CLOSED`` -- maps to
    :class:`~qlabs_catalog_sync_sdk.exceptions.TransientError` as the same honest "this
    attempt failed, we do not know that a retry would fail identically" fallback used
    throughout this codebase: our own SQL text is fixed and tested, so a real-world
    failure here is far more likely to be a transient warehouse/resource condition than
    a syntax bug this suite would have already caught.
    """
    status = payload.get("status")
    state = str(status.get("state", "UNKNOWN")) if isinstance(status, Mapping) else "UNKNOWN"
    error = status.get("error") if isinstance(status, Mapping) else None
    error_code = str(error.get("error_code", "")) if isinstance(error, Mapping) else ""
    if isinstance(error, Mapping) and error.get("message"):
        message = str(error["message"])
    else:
        message = f"statement {statement_id} ended in terminal state {state}"

    if _looks_like_auth_failure(error_code):
        raise AuthError(
            f"statement {statement_id} failed: {message} (error_code={error_code or 'unknown'})",
            endpoint=endpoint,
        )
    raise TransientError(
        f"statement {statement_id} did not succeed: {message} "
        f"(state={state}, error_code={error_code or 'unknown'})",
        endpoint=endpoint,
    )


async def _post(
    http: HttpEndpoint, url: str, *, json: Mapping[str, Any], endpoint: str
) -> dict[str, Any]:
    try:
        response = await http.post(url, json=json)
    except httpx.HTTPStatusError as exc:
        _raise_for_status(exc, endpoint=endpoint)
    except httpx.TransportError as exc:
        raise TransientError(
            f"transport error calling {url}: {exc}", endpoint=endpoint, cause=exc
        ) from exc
    result: dict[str, Any] = response.json()
    return result


async def _get(http: HttpEndpoint, url: str, *, endpoint: str) -> dict[str, Any]:
    try:
        response = await http.get(url)
    except httpx.HTTPStatusError as exc:
        _raise_for_status(exc, endpoint=endpoint)
    except httpx.TransportError as exc:
        raise TransientError(
            f"transport error calling {url}: {exc}", endpoint=endpoint, cause=exc
        ) from exc
    result: dict[str, Any] = response.json()
    return result


# ----------------------------------------------------------------------------------
# Row grouping: raw INFORMATION_SCHEMA rows -> Tag lists keyed by full_name
# ----------------------------------------------------------------------------------


def _schema_full_name(catalog_name: str, schema_name: str) -> str:
    return f"{catalog_name}.{schema_name}"


def _table_full_name(catalog_name: str, schema_name: str, table_name: str) -> str:
    return f"{catalog_name}.{schema_name}.{table_name}"


def _tag_from_row(tag_name: Any, tag_value: Any) -> Tag:
    """Build one :class:`Tag`, preserving the SQL-``NULL``-vs-empty-string distinction:
    ``tag_value is None`` (JSON ``null``) becomes ``Tag.value is None``; any other value
    -- including ``""`` -- is carried through verbatim, and ``tag_name`` is never
    case-folded (tag keys are case-sensitive, RS-01 section 1.3)."""
    return Tag(key=str(tag_name), value=None if tag_value is None else str(tag_value))


def _require_row_width(row: Sequence[Any], *, expected: int, table: str) -> None:
    """Fail loudly when a result row is not the shape this module assumes.

    The ``INFORMATION_SCHEMA`` column sets are a documented assumption, not something
    confirmed against a live workspace (see this module's TENANT_UNVERIFIED notes), so a
    row of the wrong width means the assumption is wrong. Raising a typed
    :class:`ConnectorError` names that plainly; indexing past the end would surface as a
    bare ``IndexError`` the engine has no handling for, and silently skipping the row
    would hide a wrong column assumption behind an entity that simply looks untagged.
    """
    if len(row) < expected:
        raise ConnectorError(
            f"{table} returned a row with {len(row)} column(s), expected at least "
            f"{expected}: the assumed INFORMATION_SCHEMA column set does not match this "
            f"workspace"
        )


def _group_schema_tag_rows(rows: Sequence[Sequence[Any]]) -> dict[str, tuple[Tag, ...]]:
    grouped: dict[str, list[Tag]] = {}
    for row in rows:
        _require_row_width(row, expected=4, table="INFORMATION_SCHEMA.SCHEMA_TAGS")
        catalog_name, schema_name, tag_name, tag_value = row[0], row[1], row[2], row[3]
        full_name = _schema_full_name(str(catalog_name), str(schema_name))
        grouped.setdefault(full_name, []).append(_tag_from_row(tag_name, tag_value))
    return {key: tuple(value) for key, value in grouped.items()}


def _group_table_tag_rows(rows: Sequence[Sequence[Any]]) -> dict[str, tuple[Tag, ...]]:
    grouped: dict[str, list[Tag]] = {}
    for row in rows:
        _require_row_width(row, expected=5, table="INFORMATION_SCHEMA.TABLE_TAGS")
        catalog_name, schema_name, table_name, tag_name, tag_value = (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
        )
        full_name = _table_full_name(str(catalog_name), str(schema_name), str(table_name))
        grouped.setdefault(full_name, []).append(_tag_from_row(tag_name, tag_value))
    return {key: tuple(value) for key, value in grouped.items()}
