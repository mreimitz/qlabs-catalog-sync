"""The Alembic migration runs cleanly from empty to head, and turns on SQLite WAL."""

from __future__ import annotations

from sqlalchemy import inspect, text

from qlabs_catalog_sync.state.db import create_state_engine
from qlabs_catalog_sync.state.migrate import downgrade_to_base, upgrade_to_head

EXPECTED_TABLES = {"identity_map", "watermarks", "field_envelopes", "orphan_log"}


def test_migration_creates_every_table_from_empty(db_url: str) -> None:
    upgrade_to_head(db_url)

    engine = create_state_engine(db_url)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) >= EXPECTED_TABLES
    finally:
        engine.dispose()


def test_migration_sets_expected_primary_keys(db_url: str) -> None:
    upgrade_to_head(db_url)

    engine = create_state_engine(db_url)
    try:
        inspector = inspect(engine)
        pk = {
            table: tuple(inspector.get_pk_constraint(table)["constrained_columns"])
            for table in EXPECTED_TABLES
        }
    finally:
        engine.dispose()

    assert pk["identity_map"] == ("neutral_id", "endpoint", "entity_type")
    assert pk["watermarks"] == ("sync_pair", "endpoint", "entity_type")
    assert pk["field_envelopes"] == ("neutral_id", "endpoint", "entity_type", "field")
    assert pk["orphan_log"] == ("neutral_id", "endpoint", "entity_type")


def test_migration_sets_identity_map_unique_constraint(db_url: str) -> None:
    upgrade_to_head(db_url)

    engine = create_state_engine(db_url)
    try:
        inspector = inspect(engine)
        unique_constraints = inspector.get_unique_constraints("identity_map")
    finally:
        engine.dispose()

    assert any(
        set(uc["column_names"]) == {"endpoint", "entity_type", "tenant_id", "native_key"}
        for uc in unique_constraints
    )


def test_downgrade_drops_every_table(db_url: str) -> None:
    upgrade_to_head(db_url)
    downgrade_to_base(db_url)

    engine = create_state_engine(db_url)
    try:
        inspector = inspect(engine)
        assert EXPECTED_TABLES.isdisjoint(set(inspector.get_table_names()))
    finally:
        engine.dispose()


def test_wal_mode_is_actually_on(migrated_db_url: str) -> None:
    engine = create_state_engine(migrated_db_url)
    try:
        with engine.connect() as connection:
            mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
    finally:
        engine.dispose()

    assert mode.lower() == "wal"
