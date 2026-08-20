"""No column in the configuration schema can hold a credential value (the DoD, C2).

Two halves:

* :func:`test_no_credential_shaped_column_in_the_config_schema` reflects the six tables
  this task owns from a real migrated database and asserts the credential-shaped-column
  scanner (``configstore_helpers.credential_shaped_columns``) finds nothing except the one
  documented, allowed exception -- ``endpoints.secret_ref``, a named *reference*, never a
  value (C2).
* :func:`test_the_scanner_actually_catches_a_credential_shaped_column` is the dishonest
  case: it runs the *same* scanner against a deliberately "dirty" table with a ``password``
  column and asserts it is caught. Without this half, a scanner that always returned ``[]``
  would make the first test pass for the wrong reason.

Deliberately scoped to the six tables this task adds, not the whole state-store schema:
``qlabs_catalog_sync.state.models.WatermarkRow.watermark_token`` (T2.2, not owned by this
task) is a legitimate opaque resume cursor, not a credential, and would otherwise be a
false positive against the "token" marker.
"""

from __future__ import annotations

from configstore_helpers import credential_shaped_columns
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, create_engine, inspect

from qlabs_catalog_sync.state.db import create_state_engine

CONFIG_TABLES = [
    "endpoints",
    "sync_pairs",
    "selection_rules",
    "selection_overrides",
    "config_generation",
    "config_changes",
]

#: The one documented exception: a named secret *reference*, never a value (C2).
ALLOWED_CREDENTIAL_ADJACENT_COLUMNS = frozenset({("endpoints", "secret_ref")})


def test_no_credential_shaped_column_in_the_config_schema(migrated_db_url: str) -> None:
    engine = create_state_engine(migrated_db_url)
    try:
        inspector = inspect(engine)
        hits = credential_shaped_columns(
            inspector, CONFIG_TABLES, allowed=ALLOWED_CREDENTIAL_ADJACENT_COLUMNS
        )
    finally:
        engine.dispose()

    assert hits == []


def test_endpoints_secret_ref_is_the_only_credential_adjacent_column(migrated_db_url: str) -> None:
    """Pin the allowlist itself: it names exactly one column, and that column is a string."""
    engine = create_state_engine(migrated_db_url)
    try:
        inspector = inspect(engine)
        columns = {col["name"]: col for col in inspector.get_columns("endpoints")}
    finally:
        engine.dispose()

    assert {("endpoints", "secret_ref")} == ALLOWED_CREDENTIAL_ADJACENT_COLUMNS
    secret_ref_column = columns["secret_ref"]
    assert secret_ref_column["nullable"] is True
    assert str(secret_ref_column["type"]).startswith("VARCHAR")


def test_the_scanner_actually_catches_a_credential_shaped_column() -> None:
    """The dishonest case: a scanner that always returns [] would make the real test lie."""
    dirty_metadata = MetaData()
    Table(
        "fake_endpoints",
        dirty_metadata,
        Column("id", Integer, primary_key=True),
        Column("connector", String(64)),
        Column("password", String(255)),  # exactly the kind of column this must catch
        Column("enabled", Boolean),
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        dirty_metadata.create_all(engine)
        inspector = inspect(engine)
        hits = credential_shaped_columns(inspector, ["fake_endpoints"])
    finally:
        engine.dispose()

    assert hits == [("fake_endpoints", "password")]


def test_the_scanner_respects_its_allowlist() -> None:
    """A column that *is* allowed is not reported, even though its name matches a marker."""
    dirty_metadata = MetaData()
    Table(
        "fake_endpoints",
        dirty_metadata,
        Column("id", Integer, primary_key=True),
        Column("secret_ref", String(255)),
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        dirty_metadata.create_all(engine)
        inspector = inspect(engine)
        hits = credential_shaped_columns(
            inspector, ["fake_endpoints"], allowed=frozenset({("fake_endpoints", "secret_ref")})
        )
    finally:
        engine.dispose()

    assert hits == []
