"""Shared fixtures and hand-authored Snowflake payload builders for T6.6's conformance
suite: the SDK's ``ConnectorConformanceSuite`` run against a real ``Connector``, plus the
connector-specific supplementary tests this task adds beside it
(``test_write_refusal.py``, ``test_manifest_read_honesty.py``, ``test_read_cassettes.py``).

Self-contained within ``tests/conformance`` -- matching every other per-task conftest in
this package (``tests/auth/conftest.py``, ``tests/read/conftest.py``, ...): its own
``SnowflakeConfig`` builder, its own throwaway RSA keypair, its own statement-response
builders. Nothing here imports another task's test module and nothing here touches a live
account.

Every payload shape below is drawn from
``planning/Research/RS-05-snowflake-catalog-api/outputs/snowflake-catalog-api-reference.md``
(RS-05) sections 1.2, 1.3, 2.2, 3.4, 3.5, 3.6 and 3.8, hand-derived from the documented
column lists -- never captured from a real tenant. ``read.py``'s own module docstring
flags the same shapes TENANT-UNVERIFIED for the same reason.

**Why there is no long-lived respx router around the ``connector`` fixture.** respx
routers do not nest usefully: with an outer router already active, an inner
``respx.mock()`` never sees the request at all -- the outer one answers first. That
matters here because the base conformance suite's capability-honesty checks all go
through ``qlabs_catalog_sync_sdk.conformance.harness.assert_no_http_calls``, which opens
its own inner router; an outer router live for the whole test would make every one of
those checks report "0 calls" unconditionally, turning a real certification into a
vacuous one. So ``setup_connector()`` below activates nothing (Snowflake's key-pair JWT
is signed locally -- ``setup()`` makes no HTTP call at all, unlike Databricks' OAuth
token fetch), and each test that genuinely needs a response opens its own router for
exactly the call it is making. The one base-suite method that needs a live answer,
``test_healthcheck_returns_a_status``, is overridden in
``test_snowflake_conformance_suite.py`` for that reason -- see its docstring.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import vcr
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from qlabs_catalog_sync_sdk.config import ConnectorContext, ManualClock
from qlabs_catalog_sync_sdk.conformance.harness import vcr_config
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import EntityType, IdentityRef
from qlabs_connector_snowflake import Connector
from qlabs_connector_snowflake.auth import SnowflakeConfig
from qlabs_connector_snowflake.read import STATEMENTS_PATH, StatementClient

ENDPOINT = "snowflake"
ORGANIZATION = "acme"
ACCOUNT = "primary"

#: The account base URL ``SnowflakeConfig`` derives from org/account (RS-05 section 3.1),
#: spelled out here because the cassettes and the respx routes both have to name it.
BASE_URL = f"https://{ORGANIZATION}-{ACCOUNT}.snowflakecomputing.com"
STATEMENTS_URL = f"{BASE_URL}{STATEMENTS_PATH}"
DATABASES_URL = f"{BASE_URL}/api/v2/databases"

#: ``SnowflakeConfig.account_identifier`` -- the upper-cased ``<ORG>-<ACCOUNT>`` form the
#: connector stamps onto every ref's ``tenant_id``.
TENANT_ID = f"{ORGANIZATION}-{ACCOUNT}".upper()

STATEMENT_HANDLE = "01b2c3d4-0000-0000-0000-0000000006c6"

#: Where the hand-authored cassettes live -- the sibling directory T6.6 also owns.
CASSETTE_DIR = Path(__file__).resolve().parent.parent / "cassettes"


# --------------------------------------------------------------------------------------
# Credentials: one throwaway RSA keypair for the whole module
# --------------------------------------------------------------------------------------


def _generate_private_key_pem() -> str:
    """A locally generated, unencrypted PKCS#8 RSA private key.

    Generated once at import rather than per config: the conformance suite builds a fresh
    connector for every test method, and 2048-bit key generation per test would dominate
    this directory's runtime for no added coverage. ``tests/auth/`` is where key handling
    itself (passphrases, malformed PEMs, fingerprints) is actually certified.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


PRIVATE_KEY_PEM = _generate_private_key_pem()


# --------------------------------------------------------------------------------------
# SnowflakeConfig / ConnectorContext builders
# --------------------------------------------------------------------------------------


def build_config(**overrides: Any) -> SnowflakeConfig:
    """A minimally valid :class:`SnowflakeConfig`, with any field overridden."""
    values: dict[str, Any] = {
        "organization": ORGANIZATION,
        "account": ACCOUNT,
        "user": "svc_qlabs_conformance",
        "private_key": PRIVATE_KEY_PEM,
    }
    values.update(overrides)
    return SnowflakeConfig(**values)


def build_ctx(config: SnowflakeConfig | None = None) -> ConnectorContext[SnowflakeConfig]:
    return ConnectorContext.build(
        config=config or build_config(), endpoint=ENDPOINT, clock=ManualClock()
    )


@contextlib.asynccontextmanager
async def setup_connector(**config_overrides: Any) -> AsyncIterator[Connector]:
    """Build, ``setup()`` and yield a real :class:`Connector` -- the exact shape every
    real connector's conformance fixture takes (see ``ConnectorConformanceSuite``'s own
    docstring: build it, setup() it, yield it, close() it).

    No HTTP interception is installed here, deliberately: Snowflake's key-pair JWT is
    minted locally from the private key (``auth.py``), so ``setup()`` performs no network
    I/O whatsoever and nothing needs mocking for it to succeed. See this module's own
    docstring for why keeping this function transport-free -- rather than wrapping the
    yielded connector in a router the way the Databricks conformance fixture wraps its
    OAuth fetch -- is what keeps the base suite's ``assert_no_http_calls`` checks sound.
    """
    connector = Connector(clock=ManualClock())
    await connector.setup(build_ctx(build_config(**config_overrides)))
    try:
        yield connector
    finally:
        await connector.close()


@contextlib.asynccontextmanager
async def statement_client() -> AsyncIterator[StatementClient]:
    """A bare :class:`StatementClient` over an unauthenticated ``HttpEndpoint``.

    For the tests that call ``read.read_schema()``/``read.read_listing()`` directly --
    the two functions that return the *member* half of a
    :class:`~qlabs_connector_snowflake.read.DataProductRead` that ``Connector.read()``
    discards. No auth provider is wired: whatever answers the request (respx or a
    cassette) does not check the header, and building one would only slow the test down.
    """
    async with HttpEndpoint(BASE_URL) as http:
        yield StatementClient(http, endpoint=ENDPOINT)


# --------------------------------------------------------------------------------------
# /api/v2/statements response builders (RS-05 section 3.8)
# --------------------------------------------------------------------------------------


def result_response(columns: tuple[str, ...] | list[str], rows: list[list[Any]]) -> dict[str, Any]:
    """A completed statement's body: column descriptors plus rows, one partition."""
    return {
        "code": "090001",
        "statementHandle": STATEMENT_HANDLE,
        "message": "Statement executed successfully.",
        "resultSetMetaData": {
            "numRows": len(rows),
            "format": "jsonv2",
            "partitionInfo": [{"rowCount": len(rows)}],
            "rowType": [{"name": name, "type": "text", "nullable": True} for name in columns],
        },
        "data": rows,
    }


def healthcheck_response() -> dict[str, Any]:
    """One database entry, the shape ``GET /api/v2/databases?showLimit=1`` returns
    (RS-05 section 3.7's resource REST APIs return a JSON array of objects)."""
    return {"name": "SALES_DB", "kind": "STANDARD", "comment": "Sales domain data."}


# --------------------------------------------------------------------------------------
# Column sets, in the exact order read.py projects them
# --------------------------------------------------------------------------------------

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
    "data_dictionary",
    "state",
    "updated_on",
)

SHARE_COLUMNS = ("created_on", "kind", "name", "shared_on")


# --------------------------------------------------------------------------------------
# Row builders -- the fixture world every test in this directory reads
# --------------------------------------------------------------------------------------

DATABASE = "SALES_DB"
SCHEMA = "PUBLIC"
TABLE = "ORDERS"
TABLE_FQN = f"{DATABASE}.{SCHEMA}.{TABLE}"
SCHEMA_FQN = f"{DATABASE}.{SCHEMA}"
LISTING_GLOBAL_NAME = "GZTSZAS2KH9"
LISTING_NAME = "SALES_DAILY"
SHARE_NAME = "SALES_S"

CREATED_AT = "1700000000.000000000"
LAST_ALTERED_AT = "1700003600.000000000"


def table_row(
    name: str = TABLE,
    *,
    schema: str = SCHEMA,
    owner: str = "SALES_ENGINEER",
    comment: str | None = "Order header rows, one per checkout.",
) -> list[Any]:
    """An ``INFORMATION_SCHEMA.TABLES`` row (RS-05 section 1.2)."""
    return [
        DATABASE,
        schema,
        name,
        owner,
        "BASE TABLE",
        "NO",
        comment,
        CREATED_AT,
        LAST_ALTERED_AT,
    ]


def column_row(
    name: str = "ORDER_ID",
    *,
    position: int = 1,
    data_type: str = "NUMBER",
    nullable: str = "NO",
    comment: str | None = "Surrogate key",
) -> list[Any]:
    """An ``INFORMATION_SCHEMA.COLUMNS`` row (RS-05 section 1.2)."""
    return [name, position, data_type, nullable, comment]


def schema_row(
    name: str = SCHEMA,
    *,
    owner: str = "SYSADMIN",
    comment: str | None = "Conformed sales dimensions and facts.",
) -> list[Any]:
    """An ``INFORMATION_SCHEMA.SCHEMATA`` row (RS-05 section 1.2)."""
    return [DATABASE, name, owner, "NO", "NO", comment, CREATED_AT, LAST_ALTERED_AT]


def tag_row(
    tag_name: str = "COST_CENTER",
    tag_value: str | None = "commerce",
    *,
    tag_database: str = "GOVERNANCE",
    tag_schema: str = "TAGS",
    domain: str = "TABLE",
    object_schema: str = SCHEMA,
    object_name: str = TABLE,
    column_name: str | None = None,
) -> list[Any]:
    """A ``TAG_REFERENCES`` row -- an author-curated tag by default (RS-05 section 3.4)."""
    return [
        tag_database,
        tag_schema,
        tag_name,
        tag_value,
        domain,
        DATABASE,
        object_schema,
        object_name,
        column_name,
    ]


def classification_row(
    tag_name: str = "PRIVACY_CATEGORY",
    tag_value: str = "IDENTIFIER",
    *,
    column_name: str = "ORDER_ID",
    object_name: str = TABLE,
) -> list[Any]:
    """A ``SNOWFLAKE.CORE`` system classification row -- what ``mapping.py`` routes to
    ``classifications`` rather than ``tags`` (RS-05 section 4.2). Column-level, because
    Snowflake's classification engine assigns per column (RS-05 section 1.3)."""
    return tag_row(
        tag_name,
        tag_value,
        tag_database="SNOWFLAKE",
        tag_schema="CORE",
        object_name=object_name,
        column_name=column_name,
    )


def listing_row(
    *,
    name: str = LISTING_NAME,
    global_name: str = LISTING_GLOBAL_NAME,
    share: str | None = SHARE_NAME,
    state: str = "PUBLISHED",
) -> list[Any]:
    """A ``SHOW LISTINGS`` row (RS-05 sections 2.4 / 3.6)."""
    return [
        CREATED_AT,
        name,
        global_name,
        "Daily sales",
        "Daily sales by region, refreshed nightly",
        share,
        "SALES_PROVIDER",
        "Managed by the commercial analytics team.",
        state,
        "APPROVED",
    ]


def describe_listing_row(*, state: str = "PUBLISHED") -> list[Any]:
    """A ``DESCRIBE LISTING`` row -- the descriptive manifest half (RS-05 section 3.6),
    including the long-form Markdown ``description`` the neutral ``documentation`` field
    is built from."""
    return [
        LISTING_GLOBAL_NAME,
        LISTING_NAME,
        "SALES_PROVIDER",
        "Daily sales",
        "Daily sales by region, refreshed nightly",
        "# Daily sales\n\nSales fact tables refreshed **daily**.",
        json.dumps(["BUSINESS"]),
        json.dumps(
            {
                "featured": {
                    "database": DATABASE,
                    "objects": [{"name": TABLE, "schema": SCHEMA, "domain": "TABLE"}],
                }
            }
        ),
        state,
        LAST_ALTERED_AT,
    ]


def share_row(name: str = TABLE_FQN, *, kind: str = "TABLE") -> list[Any]:
    """A ``DESCRIBE SHARE`` row (RS-05 section 3.5)."""
    return [CREATED_AT, kind, name, CREATED_AT]


# --------------------------------------------------------------------------------------
# Statement routing -- every statement goes to one URL, so dispatch on the SQL text
# --------------------------------------------------------------------------------------


@dataclass
class _Rule:
    needle: str
    response: httpx.Response


@dataclass
class StatementRouter:
    """Routes ``POST /api/v2/statements`` by a substring of the submitted SQL.

    Every statement this connector issues goes to the same URL, so a test that registered
    responses positionally would pass or fail on statement *ordering* rather than on
    content. Dispatching on the SQL text is what keeps a test honest about which query it
    is answering, and an unrouted statement raises rather than silently picking up another
    query's canned response. Same design as ``tests/read/conftest.py``'s router, kept as
    its own copy here because each task's conftest in this package is self-contained.
    """

    rules: list[_Rule] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)

    def rows(
        self,
        needle: str,
        columns: tuple[str, ...] | list[str],
        rows: list[list[Any]],
    ) -> StatementRouter:
        """Answer any statement containing ``needle`` with one completed result set."""
        self.rules.append(
            _Rule(needle=needle, response=httpx.Response(200, json=result_response(columns, rows)))
        )
        return self

    def statements_matching(self, needle: str) -> list[str]:
        return [statement for statement in self.statements if needle in statement]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        statement = str(json.loads(request.content).get("statement", ""))
        self.statements.append(statement)
        for rule in self.rules:
            if rule.needle in statement:
                return rule.response
        raise AssertionError(f"unrouted statement: {statement!r}")


def mock_statements(router: respx.MockRouter, statements: StatementRouter) -> respx.Route:
    """Wire a :class:`StatementRouter` onto ``POST /api/v2/statements``."""
    return router.post(STATEMENTS_URL).mock(side_effect=statements)


def mock_healthcheck(router: respx.MockRouter) -> respx.Route:
    """Answer the healthcheck probe (``GET /api/v2/databases?showLimit=1``)."""
    return router.get(DATABASES_URL).mock(
        return_value=httpx.Response(200, json=[healthcheck_response()])
    )


def dataset_statements() -> StatementRouter:
    """The three statements ``read_dataset`` issues: the object row, its columns, its
    tag references (``read.py``'s own docstring). Carries one curated tag and one system
    classification so both neutral fields have something to prove."""
    return (
        StatementRouter()
        .rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row()])
        .rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [column_row()])
        .rows(
            "TAG_REFERENCES",
            TAG_REFERENCE_COLUMNS,
            [tag_row(), classification_row()],
        )
    )


def schema_statements() -> StatementRouter:
    """The four statements ``read_schema`` issues: the schema row, the schema's own tag
    references, the member object rows, and the account-wide member tag index."""
    return (
        StatementRouter()
        .rows("INFORMATION_SCHEMA.SCHEMATA", SCHEMATA_COLUMNS, [schema_row()])
        .rows(
            "INFORMATION_SCHEMA.TAG_REFERENCES",
            TAG_REFERENCE_COLUMNS,
            [tag_row("DOMAIN", "sales", domain="SCHEMA", object_name=SCHEMA)],
        )
        .rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row()])
        .rows(
            "ACCOUNT_USAGE.TAG_REFERENCES",
            TAG_REFERENCE_COLUMNS,
            [tag_row()],
        )
    )


def listing_statements() -> StatementRouter:
    """The three statements ``read_listing`` issues: locate the listing, describe it,
    describe the share beneath it."""
    return (
        StatementRouter()
        .rows("SHOW LISTINGS", LISTING_COLUMNS, [listing_row()])
        .rows("DESCRIBE LISTING", DESCRIBE_LISTING_COLUMNS, [describe_listing_row()])
        .rows("DESCRIBE SHARE", SHARE_COLUMNS, [share_row()])
    )


# --------------------------------------------------------------------------------------
# IdentityRef builders
# --------------------------------------------------------------------------------------


def dataset_ref(native_key: str = TABLE_FQN, **secondary_keys: str) -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key=native_key,
        tenant_id=TENANT_ID,
        secondary_keys=secondary_keys,
    )


def schema_ref(native_key: str = SCHEMA_FQN) -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=native_key,
        tenant_id=TENANT_ID,
    )


def listing_ref(native_key: str = LISTING_GLOBAL_NAME) -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=native_key,
        tenant_id=TENANT_ID,
        secondary_keys={"listing_name": LISTING_NAME},
    )


# --------------------------------------------------------------------------------------
# vcrpy: hand-authored cassette playback (test_read_cassettes.py)
# --------------------------------------------------------------------------------------

#: ``vcr_config``'s default ``match_on`` includes ``query``, which cannot work for this
#: connector: ``StatementClient.execute`` stamps every statement with a fresh
#: ``requestId`` UUID (RS-05 section 3.8's idempotency parameter), so a recorded query
#: string never matches the one a replayed run sends. Matching on method+path instead
#: makes every ``POST /api/v2/statements`` interaction equivalent, and vcrpy then serves
#: them in recorded order -- which is exactly the guarantee these cassettes need, since
#: what distinguishes one statement from the next is its *body*, and vcrpy's own default
#: deliberately excludes body from matching (see ``harness.vcr_config``'s docstring).
CASSETTE_MATCH_ON = ("method", "scheme", "host", "port", "path")


@pytest.fixture
def snowflake_vcr() -> vcr.VCR:
    """The SDK's own pre-configured ``vcr.VCR`` (``qlabs_catalog_sync_sdk.conformance
    .harness.vcr_config``), pointed at this task's owned ``tests/cassettes/`` directory.
    ``record_mode="once"`` (the helper's default) means: play back the hand-authored
    cassette already on disk, never silently re-hit a network."""
    return vcr_config(CASSETTE_DIR, match_on=CASSETTE_MATCH_ON)
