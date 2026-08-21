"""Shared fixtures and SQL REST API response builders for the read-path tests (T6.4).

Mirrors the pattern the Databricks connector established in ``tests/read/conftest.py`` and
``tests/sql_tags/conftest.py``: an :class:`HttpEndpoint` factory wired for fast,
deterministic tests (no real sleeping, small retry counts), a
:class:`~qlabs_catalog_sync_sdk.config.ManualClock` so poll-wait tests never really sleep,
and builders shaped like real ``/api/v2/statements`` responses (RS-05 section 3.8) so each
test file stays focused on the behavior it proves rather than JSON scaffolding.

The interesting piece is :class:`StatementRouter`. Every statement this connector issues
goes to the *same* URL (``POST /api/v2/statements``), so a test that registered responses
positionally would silently pass or fail on statement ordering rather than on content.
The router instead dispatches on a substring of the SQL text, records every request body
for assertions, and raises on an unrouted statement -- so a read that starts issuing an
unexpected query fails loudly instead of picking up someone else's canned response.

There is no live Snowflake tenant for this build; every response shape here is the one
``read.py``'s module docstring flags as TENANT-UNVERIFIED.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from qlabs_catalog_sync_sdk.config import ManualClock
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import EntityType, IdentityRef
from qlabs_connector_snowflake.read import STATEMENTS_PATH, StatementClient

BASE_URL = "https://acme-primary.snowflakecomputing.com"
ENDPOINT = "snowflake"
TENANT_ID = "ACME-PRIMARY"
STATEMENT_HANDLE = "01b2c3d4-0000-0000-0000-000000000001"


# ----------------------------------------------------------------------------------
# URLs
# ----------------------------------------------------------------------------------


def statements_url() -> str:
    return f"{BASE_URL}{STATEMENTS_PATH}"


def statement_url(handle: str = STATEMENT_HANDLE) -> str:
    return f"{BASE_URL}{STATEMENTS_PATH}/{handle}"


# ----------------------------------------------------------------------------------
# Response builders (TENANT-UNVERIFIED shapes -- see read.py's module docstring)
# ----------------------------------------------------------------------------------


def result_response(
    columns: tuple[str, ...] | list[str],
    rows: list[list[Any]],
    *,
    handle: str = STATEMENT_HANDLE,
    partitions: int = 1,
) -> dict[str, Any]:
    """A completed statement's body: column descriptors, rows, partition info."""
    return {
        "code": "090001",
        "statementHandle": handle,
        "message": "Statement executed successfully.",
        "resultSetMetaData": {
            "numRows": len(rows),
            "format": "jsonv2",
            "partitionInfo": [{"rowCount": len(rows)} for _ in range(partitions)],
            "rowType": [{"name": name, "type": "text", "nullable": True} for name in columns],
        },
        "data": rows,
    }


def partition_response(rows: list[list[Any]]) -> dict[str, Any]:
    """A ``GET /api/v2/statements/{handle}?partition=N`` body: rows only."""
    return {"data": rows}


def accepted_response(handle: str = STATEMENT_HANDLE) -> dict[str, Any]:
    """The HTTP 202 body for a statement Snowflake is still executing."""
    return {
        "code": "333334",
        "statementHandle": handle,
        "message": "Asynchronous execution in progress. Use provided query id to perform query "
        "monitoring and management.",
        "statementStatusUrl": f"{STATEMENTS_PATH}/{handle}",
    }


# ----------------------------------------------------------------------------------
# Column sets, in the exact order read.py projects them
# ----------------------------------------------------------------------------------

TABLES_COLUMNS = (
    "TABLE_CATALOG",
    "TABLE_SCHEMA",
    "TABLE_NAME",
    "TABLE_OWNER",
    "TABLE_TYPE",
    "IS_TRANSIENT",
    "COMMENT",
    "CREATED",
    "LAST_ALTERED",
)

VIEWS_COLUMNS = (
    "TABLE_CATALOG",
    "TABLE_SCHEMA",
    "TABLE_NAME",
    "TABLE_OWNER",
    "IS_SECURE",
    "COMMENT",
    "CREATED",
    "LAST_ALTERED",
)

COLUMNS_COLUMNS = ("COLUMN_NAME", "ORDINAL_POSITION", "DATA_TYPE", "IS_NULLABLE", "COMMENT")

SCHEMATA_COLUMNS = (
    "CATALOG_NAME",
    "SCHEMA_NAME",
    "SCHEMA_OWNER",
    "IS_TRANSIENT",
    "IS_MANAGED_ACCESS",
    "COMMENT",
    "CREATED",
    "LAST_ALTERED",
)

TAG_REFERENCE_COLUMNS = (
    "TAG_DATABASE",
    "TAG_SCHEMA",
    "TAG_NAME",
    "TAG_VALUE",
    "DOMAIN",
    "OBJECT_DATABASE",
    "OBJECT_SCHEMA",
    "OBJECT_NAME",
    "COLUMN_NAME",
)

LISTING_COLUMNS = (
    "created_on",
    "name",
    "global_name",
    "title",
    "subtitle",
    "share",
    "owner",
    "comment",
    "state",
    "review_state",
)

DESCRIBE_LISTING_COLUMNS = (
    "global_name",
    "name",
    "owner",
    "title",
    "subtitle",
    "description",
    "categories",
    "business_needs",
    "data_attributes",
    "compliance_badges",
    "data_dictionary",
    "targets",
    "state",
    "updated_on",
)

SHARE_COLUMNS = ("created_on", "kind", "name", "shared_on")


# ----------------------------------------------------------------------------------
# Row builders
# ----------------------------------------------------------------------------------


def table_row(
    name: str = "ORDERS",
    *,
    database: str = "SALES_DB",
    schema: str = "PUBLIC",
    owner: str | None = "SALES_ENGINEER",
    table_type: str = "BASE TABLE",
    is_transient: str = "NO",
    comment: str | None = "Order header rows, one per checkout.",
    created: str = "1700000000.000000000",
    last_altered: str = "1700003600.000000000",
) -> list[Any]:
    return [
        database,
        schema,
        name,
        owner,
        table_type,
        is_transient,
        comment,
        created,
        last_altered,
    ]


def view_row(
    name: str = "ORDERS_EU",
    *,
    database: str = "SALES_DB",
    schema: str = "PUBLIC",
    owner: str | None = "SALES_ENGINEER",
    is_secure: str = "YES",
    comment: str | None = "EU-only projection of ORDERS.",
) -> list[Any]:
    return [
        database,
        schema,
        name,
        owner,
        is_secure,
        comment,
        "1700000000.000000000",
        "1700003600.000000000",
    ]


def column_row(
    name: str = "ID",
    *,
    position: int = 1,
    data_type: str = "NUMBER",
    nullable: str = "NO",
    comment: str | None = "Surrogate key",
) -> list[Any]:
    return [name, position, data_type, nullable, comment]


def schema_row(
    name: str = "PUBLIC",
    *,
    database: str = "SALES_DB",
    owner: str | None = "SYSADMIN",
    comment: str | None = "Conformed sales dimensions and facts.",
) -> list[Any]:
    return [
        database,
        name,
        owner,
        "NO",
        "NO",
        comment,
        "1700000000.000000000",
        "1700003600.000000000",
    ]


def tag_reference_row(
    tag_name: str = "COST_CENTER",
    tag_value: str | None = "commerce",
    *,
    tag_database: str = "GOVERNANCE",
    tag_schema: str = "TAGS",
    domain: str = "TABLE",
    object_database: str = "SALES_DB",
    object_schema: str = "PUBLIC",
    object_name: str = "ORDERS",
    column_name: str | None = None,
) -> list[Any]:
    return [
        tag_database,
        tag_schema,
        tag_name,
        tag_value,
        domain,
        object_database,
        object_schema,
        object_name,
        column_name,
    ]


def listing_row(
    *,
    name: str = "SALES_DAILY",
    global_name: str = "GZTSZAS2KH9",
    title: str = "Daily sales",
    subtitle: str = "Daily sales by region, refreshed nightly",
    share: str | None = "SALES_S",
    owner: str = "SALES_PROVIDER",
    comment: str | None = "Managed by the commercial analytics team.",
    state: str = "PUBLISHED",
) -> list[Any]:
    return [
        "1700000000.000000000",
        name,
        global_name,
        title,
        subtitle,
        share,
        owner,
        comment,
        state,
        "APPROVED",
    ]


def describe_listing_row(
    *,
    name: str = "SALES_DAILY",
    global_name: str = "GZTSZAS2KH9",
    title: str = "Daily sales",
    subtitle: str = "Daily sales by region, refreshed nightly",
    description: str | None = "# Daily sales\n\nSales fact tables refreshed **daily**.",
    data_dictionary: Any = None,
    state: str = "PUBLISHED",
) -> list[Any]:
    if data_dictionary is None:
        data_dictionary = json.dumps(
            {
                "featured": {
                    "database": "SALES_DB",
                    "objects": [{"name": "ORDERS", "schema": "PUBLIC", "domain": "TABLE"}],
                }
            }
        )
    return [
        global_name,
        name,
        "SALES_PROVIDER",
        title,
        subtitle,
        description,
        json.dumps(["BUSINESS"]),
        json.dumps([{"name": "Revenue reporting"}]),
        json.dumps({"refresh_rate": "DAILY"}),
        json.dumps(["GDPR"]),
        data_dictionary,
        json.dumps({"accounts": ["Org1.Account1"]}),
        state,
        "1700003600.000000000",
    ]


def share_row(name: str = "SALES_DB.PUBLIC.ORDERS", *, kind: str = "TABLE") -> list[Any]:
    return ["1700000000.000000000", kind, name, "1700000000.000000000"]


# ----------------------------------------------------------------------------------
# Statement routing
# ----------------------------------------------------------------------------------


@dataclass
class _Rule:
    needle: str
    responses: list[httpx.Response]


@dataclass
class StatementRouter:
    """Routes ``POST /api/v2/statements`` by a substring of the submitted SQL.

    Every statement goes to one URL, so matching on the SQL text is what keeps a test
    honest about *which* query it is answering. An unrouted statement raises rather than
    falling through to somebody else's canned response.
    """

    rules: list[_Rule] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def on(self, needle: str, *responses: httpx.Response) -> StatementRouter:
        """Answer any statement containing ``needle``. Several responses are consumed in
        order, the last one repeating."""
        self.rules.append(_Rule(needle=needle, responses=list(responses)))
        return self

    def rows(
        self,
        needle: str,
        columns: tuple[str, ...] | list[str],
        rows: list[list[Any]],
        **kwargs: Any,
    ) -> StatementRouter:
        """Answer ``needle`` with one completed result set."""
        return self.on(needle, httpx.Response(200, json=result_response(columns, rows, **kwargs)))

    def statements_matching(self, needle: str) -> list[str]:
        return [statement for statement in self.statements if needle in statement]

    def body_for(self, needle: str) -> dict[str, Any]:
        """The request body of the first statement containing ``needle``."""
        for body in self.bodies:
            if needle in str(body.get("statement", "")):
                return body
        raise AssertionError(f"no statement containing {needle!r} was submitted")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        statement = str(body.get("statement", ""))
        self.statements.append(statement)
        self.bodies.append(body)
        self.requests.append(request)
        for rule in self.rules:
            if rule.needle in statement:
                if len(rule.responses) > 1:
                    return rule.responses.pop(0)
                return rule.responses[0]
        raise AssertionError(f"unrouted statement: {statement!r}")


# ----------------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------------


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
async def make_http() -> AsyncIterator[Callable[..., HttpEndpoint]]:
    """Factory for a test :class:`HttpEndpoint`: no real sleeping, small bounded attempt
    counts, so a retries-exhausted test stays fast. Endpoints built through the factory
    are closed automatically at teardown."""
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
    """Time only moves when the code under test 'waits', and every wait is recorded."""
    return ManualClock()


@pytest.fixture
def make_client(manual_clock: ManualClock) -> Callable[..., StatementClient]:
    """Factory for a :class:`StatementClient` bound to a test endpoint.

    Defaults to a fixed ``requestId`` so a test can assert the parameter is sent without
    matching a random UUID, and to the manual clock so polling never really sleeps.
    """

    def _make(http: HttpEndpoint, **overrides: Any) -> StatementClient:
        kwargs: dict[str, Any] = dict(
            endpoint=ENDPOINT,
            clock=manual_clock,
            request_id_factory=lambda: "req-0001",
        )
        kwargs.update(overrides)
        return StatementClient(http, **kwargs)

    return _make


@pytest.fixture
def router() -> StatementRouter:
    return StatementRouter()


# ----------------------------------------------------------------------------------
# IdentityRef builders
# ----------------------------------------------------------------------------------


def dataset_ref(native_key: str = "SALES_DB.PUBLIC.ORDERS", **secondary_keys: str) -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key=native_key,
        tenant_id=TENANT_ID,
        secondary_keys=secondary_keys,
    )


def schema_ref(native_key: str = "SALES_DB.PUBLIC", **secondary_keys: str) -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=native_key,
        tenant_id=TENANT_ID,
        secondary_keys=secondary_keys,
    )


def listing_ref(
    native_key: str = "GZTSZAS2KH9", listing_name: str | None = "SALES_DAILY"
) -> IdentityRef:
    secondary = (
        {"listing_name": listing_name} if listing_name else {"listing_global_name": native_key}
    )
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=native_key,
        tenant_id=TENANT_ID,
        secondary_keys=secondary,
    )
