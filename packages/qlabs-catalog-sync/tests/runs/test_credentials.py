"""No column in the run-history schema can hold a credential value.

Two halves, the same shape as ``tests/configstore/test_credentials.py``:

* :func:`test_no_credential_shaped_column_in_the_run_history_schema` reflects the four
  tables this task owns from a real migrated database and asserts the credential-shaped-
  column scanner (``run_history_helpers.credential_shaped_columns``) finds nothing.
  Unlike the configuration schema, run history has no allowed exception at all -- there
  is no column here that is even a credential *reference* -- so the allowlist stays empty.
* :func:`test_the_scanner_actually_catches_a_credential_shaped_column` is the dishonest
  case: it runs the *same* scanner against a deliberately "dirty" table with a ``token``
  column and asserts it is caught. Without this half, a scanner that always returned
  ``[]`` would make the first test pass for the wrong reason.
"""

from __future__ import annotations

from run_history_helpers import credential_shaped_columns
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect

from qlabs_catalog_sync.state.db import create_state_engine

RUN_HISTORY_TABLES = ["runs", "run_items", "run_item_unresolved_fields", "run_errors"]


def test_no_credential_shaped_column_in_the_run_history_schema(migrated_db_url: str) -> None:
    engine = create_state_engine(migrated_db_url)
    try:
        inspector = inspect(engine)
        hits = credential_shaped_columns(inspector, RUN_HISTORY_TABLES)
    finally:
        engine.dispose()

    assert hits == []


def test_the_scanner_actually_catches_a_credential_shaped_column() -> None:
    """The dishonest case: a scanner that always returns [] would make the real test lie."""
    dirty_metadata = MetaData()
    Table(
        "fake_runs",
        dirty_metadata,
        Column("id", Integer, primary_key=True),
        Column("pair", String(64)),
        Column("api_token", String(255)),  # exactly the kind of column this must catch
        Column("committed", Integer),
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        dirty_metadata.create_all(engine)
        inspector = inspect(engine)
        hits = credential_shaped_columns(inspector, ["fake_runs"])
    finally:
        engine.dispose()

    assert hits == [("fake_runs", "api_token")]


def test_the_scanner_respects_its_allowlist() -> None:
    """A column that *is* allowed is not reported, even though its name matches a marker."""
    dirty_metadata = MetaData()
    Table(
        "fake_runs",
        dirty_metadata,
        Column("id", Integer, primary_key=True),
        Column("secret_ref", String(255)),
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        dirty_metadata.create_all(engine)
        inspector = inspect(engine)
        hits = credential_shaped_columns(
            inspector, ["fake_runs"], allowed=frozenset({("fake_runs", "secret_ref")})
        )
    finally:
        engine.dispose()

    assert hits == []
