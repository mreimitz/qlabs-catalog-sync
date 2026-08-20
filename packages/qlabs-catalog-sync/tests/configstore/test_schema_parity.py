"""The ORM models and the hand-written migration describe the same schema.

``configstore/models.py`` and ``alembic/versions/0002_config_store.py`` are two
independent, hand-written descriptions of one schema. The ORM round-trip tests
elsewhere in this package only exercise the columns they happen to construct rows
with; a column added to one file and forgotten in the other -- or given a different
nullability or type -- would pass every one of them silently. This is the check that
diffs the two directly: :func:`configstore_helpers.schema_parity_report` compares
``Base.metadata`` (scoped to the six tables this task owns -- the T2.2 tables are not
this task's to assert over) against a database migrated from empty to head.

:func:`test_the_parity_checker_actually_catches_a_drifted_column` is the dishonest
case: it runs the same comparison function over a deliberately mismatched pair (an
ad-hoc "model" with one column the reflected table does not have) and asserts the
mismatch is reported. Without this half, a comparison function that always returned
``[]`` would make the real test above pass for the wrong reason -- the same shape of
proof ``test_credentials.py`` uses for the credential scanner.
"""

from __future__ import annotations

from configstore_helpers import schema_parity_report
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect

import qlabs_catalog_sync.configstore.models  # noqa: F401  registers the six tables on Base.metadata
from qlabs_catalog_sync.state.db import create_state_engine
from qlabs_catalog_sync.state.models import Base

CONFIG_TABLES = [
    "endpoints",
    "sync_pairs",
    "selection_rules",
    "selection_overrides",
    "config_generation",
    "config_changes",
]


def test_migration_and_models_agree_on_every_configuration_table(migrated_db_url: str) -> None:
    engine = create_state_engine(migrated_db_url)
    try:
        inspector = inspect(engine)
        problems = schema_parity_report(Base.metadata, inspector, CONFIG_TABLES)
    finally:
        engine.dispose()

    assert problems == []


def test_the_parity_checker_actually_catches_a_drifted_column() -> None:
    """The dishonest case: a checker that always returns [] would make the real test lie.

    Builds an ad-hoc "model" claiming a ``notes`` column that was never actually created
    in the database, and an ad-hoc "migration" (a real, reflected table) that lacks it --
    exactly the shape of drift a forgotten column in ``0002_config_store.py`` would produce
    against ``configstore/models.py``.
    """
    # What actually exists in the database (stands in for the migration).
    real_metadata = MetaData()
    Table(
        "fake_pairs",
        real_metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(128), nullable=False),
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        real_metadata.create_all(engine)
        inspector = inspect(engine)

        # What the "model" claims -- one column ahead of what was actually created.
        drifted_metadata = MetaData()
        Table(
            "fake_pairs",
            drifted_metadata,
            Column("id", Integer, primary_key=True),
            Column("name", String(128), nullable=False),
            Column("notes", String(255), nullable=True),
        )

        problems = schema_parity_report(drifted_metadata, inspector, ["fake_pairs"])
    finally:
        engine.dispose()

    assert problems == [
        "fake_pairs.notes: declared in the ORM model, missing from the migration"
    ]


def test_the_parity_checker_catches_a_nullability_mismatch_too() -> None:
    """A second flavor of the dishonest case: same column, different nullability."""
    real_metadata = MetaData()
    Table(
        "fake_pairs",
        real_metadata,
        Column("id", Integer, primary_key=True),
        Column("cadence_seconds", Integer, nullable=False),
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        real_metadata.create_all(engine)
        inspector = inspect(engine)

        drifted_metadata = MetaData()
        Table(
            "fake_pairs",
            drifted_metadata,
            Column("id", Integer, primary_key=True),
            Column("cadence_seconds", Integer, nullable=True),  # disagrees with the DB
        )

        problems = schema_parity_report(drifted_metadata, inspector, ["fake_pairs"])
    finally:
        engine.dispose()

    assert problems == [
        "fake_pairs.cadence_seconds: nullable mismatch (model=True, migration=False)"
    ]
