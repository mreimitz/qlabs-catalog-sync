"""Fixtures and ``ACCOUNT_USAGE`` payload builders for the change-feed tests (T6.3).

Mirrors ``qlabs_connector_databricks/tests/changes/conftest.py`` -- realistic payload
builders, route helpers that speak the source's own pagination shape, and a fast
no-real-sleeping HTTP endpoint -- adapted to the one thing that is structurally different
about Snowflake: **every statement goes to the same URL**. The parent directory's
``StatementRouter`` (``tests/read/conftest.py``) already solves that by dispatching on a
substring of the submitted SQL, so this module reuses it, along with the endpoint/client
fixtures and the ``SHOW LISTINGS`` row builder, rather than growing a second copy.

Why these tests live under ``tests/read/`` at all: ``list_changed`` is implemented in
``read.py`` (its module docstring always said it would be), and T6.3's ``verify`` command
is ``pytest packages/qlabs-connector-snowflake/tests/read -q``. Keeping the Databricks
connector's one-file-per-property ``changes/`` layout *inside* ``tests/read/`` gets both:
the same structure to read, and coverage by the task's own verify command.

Each builder returns a row in exactly the column order ``read.py`` projects, because the
SQL REST API returns positional arrays and ``resultSetMetaData.rowType`` is what names
them -- a builder whose order drifted from the projection would silently mislabel every
value. All of it is TENANT-UNVERIFIED (``read.py``'s module docstring, assumptions 9-12).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, ListChangedResult, Watermark
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_snowflake.read import StatementClient, list_changed_candidates

from ..conftest import (
    SCHEMATA_COLUMNS,
    TABLES_COLUMNS,
    StatementRouter,
    result_response,
    statements_url,
)

# ----------------------------------------------------------------------------------
# Column sets, in the exact order read.py projects them
# ----------------------------------------------------------------------------------

#: ``read._ACCOUNT_USAGE_TABLES_COLUMNS``: INFORMATION_SCHEMA's projection plus the stable
#: object id and the drop timestamp this account-wide surface adds.
ACCOUNT_USAGE_TABLES_COLUMNS = ("TABLE_ID", *TABLES_COLUMNS, "DELETED")

#: ``read._ACCOUNT_USAGE_SCHEMATA_COLUMNS``, same construction.
ACCOUNT_USAGE_SCHEMATA_COLUMNS = ("SCHEMA_ID", *SCHEMATA_COLUMNS, "DELETED")

# ----------------------------------------------------------------------------------
# SQL needles the router dispatches on
# ----------------------------------------------------------------------------------

NOW_SQL = "CURRENT_TIMESTAMP"
TABLES_SQL = "ACCOUNT_USAGE.TABLES"
SCHEMATA_SQL = "ACCOUNT_USAGE.SCHEMATA"
LISTINGS_SQL = "SHOW LISTINGS"

# ----------------------------------------------------------------------------------
# Instants
# ----------------------------------------------------------------------------------

#: Snowflake's "now" for the first poll of a test, and the instants a second/third poll
#: use. Three hours apart so each poll's held-back high-water mark lands on the previous
#: poll's "now", which makes the margin arithmetic legible in an assertion.
NOW_1 = "2026-08-21T12:00:00+00:00"
NOW_2 = "2026-08-21T15:00:00+00:00"
NOW_3 = "2026-08-21T18:00:00+00:00"

#: A plausible ``LAST_ALTERED`` well inside every poll's window.
ALTERED = "2026-08-21T08:00:00+00:00"


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text)


# ----------------------------------------------------------------------------------
# Row builders
# ----------------------------------------------------------------------------------


def table_row(
    name: str = "ORDERS",
    *,
    database: str = "SALES_DB",
    schema: str = "PUBLIC",
    table_id: str | None = "1001",
    owner: str | None = "SALES_ENGINEER",
    table_type: str = "BASE TABLE",
    comment: str | None = "Order header rows, one per checkout.",
    created: str = "2026-01-01T00:00:00+00:00",
    last_altered: str | None = ALTERED,
    deleted: str | None = None,
) -> list[Any]:
    """One ``SNOWFLAKE.ACCOUNT_USAGE.TABLES`` row.

    ``table_id`` is held fixed across two polls (with a different ``name``) to simulate a
    rename of the *same* object, exactly as the Databricks conftest does with
    ``table_id``; ``deleted`` non-``None`` is how this surface reports a dropped object.
    """
    return [
        table_id,
        database,
        schema,
        name,
        owner,
        table_type,
        "NO",
        comment,
        created,
        last_altered,
        deleted,
    ]


def schema_row(
    name: str = "PUBLIC",
    *,
    database: str = "SALES_DB",
    schema_id: str | None = "2001",
    owner: str | None = "SYSADMIN",
    comment: str | None = "Conformed sales dimensions and facts.",
    created: str = "2026-01-01T00:00:00+00:00",
    last_altered: str | None = ALTERED,
    deleted: str | None = None,
) -> list[Any]:
    """One ``SNOWFLAKE.ACCOUNT_USAGE.SCHEMATA`` row -- see :func:`table_row`."""
    return [
        schema_id,
        database,
        name,
        owner,
        "NO",
        "NO",
        comment,
        created,
        last_altered,
        deleted,
    ]


# ----------------------------------------------------------------------------------
# Route helpers
# ----------------------------------------------------------------------------------


def _responses(
    columns: Sequence[str], pages: Sequence[Sequence[list[Any]]], **kwargs: Any
) -> list[httpx.Response]:
    return [
        httpx.Response(200, json=result_response(tuple(columns), list(rows), **kwargs))
        for rows in pages
    ]


def set_now(router: StatementRouter, *instants: str) -> None:
    """What ``SELECT CURRENT_TIMESTAMP()`` answers, one value per successive poll.

    The last value repeats, matching :meth:`StatementRouter.on`'s contract, so a test that
    polls three times but only cares about two distinct instants can pass two.
    """
    router.on(NOW_SQL, *_responses(("SNOWFLAKE_NOW",), [[[value]] for value in instants]))


def set_tables(router: StatementRouter, *pages: Sequence[list[Any]]) -> None:
    """What ``ACCOUNT_USAGE.TABLES`` answers, one result set per successive poll."""
    router.on(TABLES_SQL, *_responses(ACCOUNT_USAGE_TABLES_COLUMNS, pages))


def set_schemata(router: StatementRouter, *pages: Sequence[list[Any]]) -> None:
    """What ``ACCOUNT_USAGE.SCHEMATA`` answers, one result set per successive poll."""
    router.on(SCHEMATA_SQL, *_responses(ACCOUNT_USAGE_SCHEMATA_COLUMNS, pages))


def set_listings(
    router: StatementRouter, *pages: Sequence[list[Any]], columns: Sequence[str] | None = None
) -> None:
    """What ``SHOW LISTINGS`` answers, one result set per successive poll."""
    from ..conftest import LISTING_COLUMNS

    router.on(LISTINGS_SQL, *_responses(columns or LISTING_COLUMNS, pages))


# ----------------------------------------------------------------------------------
# Assertion helpers
# ----------------------------------------------------------------------------------


def cursor(result: ListChangedResult) -> dict[str, Any]:
    """The decoded opaque cursor payload this module wrote into ``next_watermark``."""
    payload: dict[str, Any] = json.loads(result.next_watermark.cursor or "{}")
    return payload


def high_water(result: ListChangedResult) -> datetime:
    """The held-back high-water instant carried inside the cursor."""
    return datetime.fromisoformat(cursor(result)["high_water"])


def bodies_matching(router: StatementRouter, needle: str) -> list[dict[str, Any]]:
    """Every submitted request body whose statement contains ``needle``, in order."""
    return [body for body in router.bodies if needle in str(body.get("statement", ""))]


def lower_bound_of(body: dict[str, Any]) -> datetime:
    """The scan's lower bound, read back off the request's bind values.

    Both ``?`` placeholders in ``read._INCREMENTAL_SCAN_WHERE`` carry the same instant, so
    the two bindings must agree -- checked here rather than in every caller.
    """
    bindings = body["bindings"]
    assert bindings["1"]["value"] == bindings["2"]["value"]
    return datetime.fromisoformat(bindings["1"]["value"])


def kinds_by_key(result: ListChangedResult) -> dict[str, str]:
    """``{native_key: change kind}`` -- the shape most assertions here want to read."""
    return {change.ref.native_key: change.kind.value for change in result.changes}


# ----------------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------------


@pytest.fixture
def client(
    respx_mock: Any,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> StatementClient:
    """A statement client whose one endpoint is served by the SQL-text router."""
    respx_mock.post(statements_url()).mock(side_effect=router)
    return make_client(http)


async def poll(
    client: StatementClient,
    entity_type: EntityType,
    since: Watermark,
    **overrides: Any,
) -> ListChangedResult:
    """One ``list_changed`` call, with this suite's fixed endpoint/tenant identity."""
    from ..conftest import ENDPOINT, TENANT_ID

    return await list_changed_candidates(
        client,
        entity_type,
        since,
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        **overrides,
    )
