"""Nothing caller-supplied ever reaches SQL text unquoted.

Three rules, proven separately: identifiers are double-quoted and escaped, literals that
cannot be bound are single-quoted and escaped, and ordinary ``WHERE``-clause values travel
in the SQL REST API's ``bindings`` object instead of the statement text. Mirrors the
Databricks connector's ``tests/sql_tags/test_identifier_safety.py``.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_snowflake.read import (
    MAX_IDENTIFIER_LENGTH,
    IdentifierError,
    StatementClient,
    quote_identifier,
    quote_literal,
    quote_qualified_name,
    read_dataset,
    read_listing,
    read_object_tag_references,
)

from .conftest import (
    COLUMNS_COLUMNS,
    TABLES_COLUMNS,
    TAG_REFERENCE_COLUMNS,
    StatementRouter,
    column_row,
    dataset_ref,
    listing_ref,
    statements_url,
    table_row,
)

# ----------------------------------------------------------------------------------
# quote_identifier
# ----------------------------------------------------------------------------------


def test_an_identifier_is_double_quoted() -> None:
    assert quote_identifier("SALES_DB") == '"SALES_DB"'


def test_an_embedded_double_quote_is_doubled() -> None:
    assert quote_identifier('we"ird') == '"we""ird"'


def test_an_injection_attempt_is_neutralized_by_quoting() -> None:
    hostile = 'x" . INFORMATION_SCHEMA.TABLES; DROP TABLE T; --'

    quoted = quote_identifier(hostile)

    # Exactly one opening and one closing quote; every inner quote is doubled, so the
    # identifier can never terminate early and start a new SQL clause.
    assert quoted.startswith('"')
    assert quoted.endswith('"')
    assert quoted[1:-1].count('"') % 2 == 0
    assert quoted == '"x"" . INFORMATION_SCHEMA.TABLES; DROP TABLE T; --"'


def test_a_dot_inside_an_identifier_never_splits_a_qualified_name() -> None:
    assert quote_qualified_name("A.B", "C") == '"A.B"."C"'


def test_a_qualified_name_quotes_every_part_independently() -> None:
    assert quote_qualified_name("SALES_DB", "PUBLIC", "ORDERS") == '"SALES_DB"."PUBLIC"."ORDERS"'


def test_case_is_preserved_because_quoting_makes_it_significant() -> None:
    """RS-05 3.8: identifiers must match the stored (usually uppercase) case. This module
    never folds case on the caller's behalf in either direction."""
    assert quote_identifier("sales_db") == '"sales_db"'


@pytest.mark.parametrize("bad", ["", "with\x00nul"])
def test_an_unusable_identifier_is_refused(bad: str) -> None:
    with pytest.raises(IdentifierError):
        quote_identifier(bad)


def test_an_over_long_identifier_is_refused() -> None:
    with pytest.raises(IdentifierError, match="limit"):
        quote_identifier("A" * (MAX_IDENTIFIER_LENGTH + 1))


def test_an_identifier_at_the_limit_is_accepted() -> None:
    assert quote_identifier("A" * MAX_IDENTIFIER_LENGTH).count("A") == MAX_IDENTIFIER_LENGTH


def test_a_qualified_name_needs_at_least_one_part() -> None:
    with pytest.raises(IdentifierError):
        quote_qualified_name()


# ----------------------------------------------------------------------------------
# quote_literal
# ----------------------------------------------------------------------------------


def test_a_literal_is_single_quoted() -> None:
    assert quote_literal("SALES_DAILY") == "'SALES_DAILY'"


def test_an_embedded_single_quote_is_doubled() -> None:
    assert quote_literal("o'brien") == "'o''brien'"


def test_a_backslash_is_escaped_because_snowflake_honors_backslash_escapes() -> None:
    """Escaping only the quote would leave ``\\'`` as an injection path."""
    assert quote_literal("a\\'b") == "'a\\\\''b'"


def test_a_literal_containing_nul_is_refused() -> None:
    with pytest.raises(IdentifierError):
        quote_literal("with\x00nul")


def test_a_non_string_literal_is_refused() -> None:
    with pytest.raises(IdentifierError):
        quote_literal(7)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------
# Applied end to end
# ----------------------------------------------------------------------------------


async def test_a_where_clause_value_is_bound_never_interpolated(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """A schema/table name containing a quote must work correctly *because* it travels as
    a bind value, never spliced into the SQL text."""
    hostile = "o'brien"
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row(name=hostile)])
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [column_row()])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    await read_dataset(make_client(http), dataset_ref(f"SALES_DB.PUBLIC.{hostile}"))

    body = router.body_for("INFORMATION_SCHEMA.TABLES")
    assert "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?" in body["statement"]
    assert body["bindings"] == {
        "1": {"type": "TEXT", "value": "PUBLIC"},
        "2": {"type": "TEXT", "value": hostile},
    }
    # The raw value never appears spliced into the statement text itself.
    assert hostile not in body["statement"]


async def test_the_database_is_quoted_into_the_from_clause(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """A database name cannot be a bind parameter -- SQL has no placeholder for an
    identifier in a ``FROM`` clause -- so it is quoted instead."""
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row(database='SA"LES')])
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    await read_dataset(make_client(http), dataset_ref('SA"LES.PUBLIC.ORDERS'))

    statement = router.body_for("INFORMATION_SCHEMA.TABLES")["statement"]
    assert '"SA""LES".INFORMATION_SCHEMA.TABLES' in statement


async def test_an_unsafe_database_name_is_refused_before_any_http_call(
    respx_mock: object, http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    catch_all = respx_mock.route(host="acme-primary.snowflakecomputing.com").mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(IdentifierError):
        await read_dataset(make_client(http), dataset_ref("with\x00nul.PUBLIC.ORDERS"))

    assert catch_all.call_count == 0


async def test_tag_reference_arguments_are_escaped_literals(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """A table function's arguments cannot be bound, so they go through
    :func:`quote_literal` -- and a hostile object name is neutralized by the escaping."""
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    await read_object_tag_references(
        make_client(http),
        database="SALES_DB",
        object_fqn="SALES_DB.PUBLIC.O'BRIEN'); DROP TABLE T; --",
        domain="TABLE",
    )

    statement = router.body_for("TAG_REFERENCES")["statement"]
    assert "'SALES_DB.PUBLIC.O''BRIEN''); DROP TABLE T; --'" in statement
    assert "'TABLE'" in statement
    # The escaped literal cannot terminate early, so no second statement is formed.
    assert statement.count("DROP TABLE") == 1
    assert statement.endswith("))")


async def test_a_show_listings_like_pattern_is_an_escaped_literal(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.rows("SHOW LISTINGS", ("global_name", "name"), [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    with pytest.raises(Exception, match="no listing matching"):
        await read_listing(
            make_client(http), listing_ref("GZ1", listing_name="SALES'; DROP TABLE T; --")
        )

    statement = router.body_for("SHOW LISTINGS")["statement"]
    assert statement == "SHOW LISTINGS LIKE 'SALES''; DROP TABLE T; --'"
