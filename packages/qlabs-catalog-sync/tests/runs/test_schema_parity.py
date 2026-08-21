"""The ORM models and the hand-written migration describe the same run-history schema.

``runs/models.py`` and ``alembic/versions/0003_run_history.py`` are two independent,
hand-written descriptions of one schema. This is the check that diffs them directly --
see ``run_history_helpers.schema_parity_report`` and
``tests/configstore/test_schema_parity.py`` for the pattern this follows.

:func:`test_the_parity_checker_actually_catches_a_drifted_column` and
:func:`test_the_parity_checker_catches_a_nullability_mismatch_too` are the dishonest
case: they run the same comparison function over a deliberately mismatched pair and
assert the mismatch is reported. Without this half, a comparison function that always
returned ``[]`` would make the real test below pass for the wrong reason.
"""

from __future__ import annotations

from run_history_helpers import schema_parity_report
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect

import qlabs_catalog_sync.runs.models  # noqa: F401  registers the run-history tables on Base.metadata
from qlabs_catalog_sync.state.db import create_state_engine
from qlabs_catalog_sync.state.models import Base

RUN_HISTORY_TABLES = ["runs", "run_items", "run_item_unresolved_fields", "run_errors"]


def test_migration_and_models_agree_on_every_run_history_table(migrated_db_url: str) -> None:
    engine = create_state_engine(migrated_db_url)
    try:
        inspector = inspect(engine)
        problems = schema_parity_report(Base.metadata, inspector, RUN_HISTORY_TABLES)
    finally:
        engine.dispose()

    assert problems == []


def test_the_parity_checker_actually_catches_a_drifted_column() -> None:
    """The dishonest case: a checker that always returns [] would make the real test lie."""
    real_metadata = MetaData()
    Table(
        "fake_runs",
        real_metadata,
        Column("id", Integer, primary_key=True),
        Column("pair", String(128), nullable=False),
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        real_metadata.create_all(engine)
        inspector = inspect(engine)

        drifted_metadata = MetaData()
        Table(
            "fake_runs",
            drifted_metadata,
            Column("id", Integer, primary_key=True),
            Column("pair", String(128), nullable=False),
            Column("notes", String(255), nullable=True),
        )

        problems = schema_parity_report(drifted_metadata, inspector, ["fake_runs"])
    finally:
        engine.dispose()

    assert problems == ["fake_runs.notes: declared in the ORM model, missing from the migration"]


def test_the_parity_checker_catches_a_nullability_mismatch_too() -> None:
    """A second flavor of the dishonest case: same column, different nullability."""
    real_metadata = MetaData()
    Table(
        "fake_runs",
        real_metadata,
        Column("id", Integer, primary_key=True),
        Column("pages", Integer, nullable=False),
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        real_metadata.create_all(engine)
        inspector = inspect(engine)

        drifted_metadata = MetaData()
        Table(
            "fake_runs",
            drifted_metadata,
            Column("id", Integer, primary_key=True),
            Column("pages", Integer, nullable=True),  # disagrees with the DB
        )

        problems = schema_parity_report(drifted_metadata, inspector, ["fake_runs"])
    finally:
        engine.dispose()

    assert problems == ["fake_runs.pages: nullable mismatch (model=True, migration=False)"]
