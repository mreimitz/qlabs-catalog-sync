"""``0002 -> 0003 -> 0002`` applies and rolls back cleanly; ``0003`` down is exactly ``0002``."""

from __future__ import annotations

from sqlalchemy import inspect

from qlabs_catalog_sync.state.db import create_state_engine
from qlabs_catalog_sync.state.migrate import (
    build_alembic_config,
    downgrade_to_base,
    upgrade_to_head,
)

RUN_HISTORY_TABLES = {"runs", "run_items", "run_item_unresolved_fields", "run_errors"}
PRE_EXISTING_TABLES = {
    # T2.2
    "identity_map",
    "watermarks",
    "field_envelopes",
    "orphan_log",
    # T10.1
    "endpoints",
    "sync_pairs",
    "selection_rules",
    "selection_overrides",
    "config_generation",
    "config_changes",
}


def test_migration_creates_every_run_history_table_from_empty(db_url: str) -> None:
    upgrade_to_head(db_url)

    engine = create_state_engine(db_url)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert table_names >= RUN_HISTORY_TABLES
    # 0003 adds tables; it does not touch the 0001/0002 schema it migrates on top of.
    assert table_names >= PRE_EXISTING_TABLES


def test_migration_sets_expected_primary_keys(db_url: str) -> None:
    upgrade_to_head(db_url)

    engine = create_state_engine(db_url)
    try:
        inspector = inspect(engine)
        pk = {
            table: tuple(inspector.get_pk_constraint(table)["constrained_columns"])
            for table in RUN_HISTORY_TABLES
        }
    finally:
        engine.dispose()

    assert pk["runs"] == ("id",)
    assert pk["run_items"] == ("id",)
    assert pk["run_item_unresolved_fields"] == ("id",)
    assert pk["run_errors"] == ("id",)


def test_migration_sets_expected_foreign_keys(db_url: str) -> None:
    upgrade_to_head(db_url)

    engine = create_state_engine(db_url)
    try:
        inspector = inspect(engine)
        item_fks = inspector.get_foreign_keys("run_items")
        field_fks = inspector.get_foreign_keys("run_item_unresolved_fields")
        error_fks = inspector.get_foreign_keys("run_errors")
    finally:
        engine.dispose()

    assert item_fks[0]["referred_table"] == "runs"
    assert item_fks[0]["constrained_columns"] == ["run_id"]
    assert item_fks[0]["options"].get("ondelete") == "CASCADE"

    assert field_fks[0]["referred_table"] == "run_items"
    assert field_fks[0]["constrained_columns"] == ["run_item_id"]
    assert field_fks[0]["options"].get("ondelete") == "CASCADE"

    assert error_fks[0]["referred_table"] == "runs"
    assert error_fks[0]["constrained_columns"] == ["run_id"]
    assert error_fks[0]["options"].get("ondelete") == "CASCADE"


def test_migration_sets_expected_indexes(db_url: str) -> None:
    upgrade_to_head(db_url)

    engine = create_state_engine(db_url)
    try:
        inspector = inspect(engine)
        run_indexes = {ix["name"] for ix in inspector.get_indexes("runs")}
        item_indexes = {ix["name"] for ix in inspector.get_indexes("run_items")}
        error_indexes = {ix["name"] for ix in inspector.get_indexes("run_errors")}
    finally:
        engine.dispose()

    assert run_indexes == {"ix_runs_pair_entity_type_started_at", "ix_runs_status"}
    assert item_indexes == {"ix_run_items_run_id", "ix_run_items_neutral_id_endpoint"}
    assert error_indexes == {"ix_run_errors_run_id"}


def test_downgrade_one_step_leaves_exactly_the_pre_existing_schema(db_url: str) -> None:
    upgrade_to_head(db_url)

    from alembic import command

    command.downgrade(build_alembic_config(db_url), "0002")

    engine = create_state_engine(db_url)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    # alembic_version is alembic's own bookkeeping table, present at every revision.
    assert table_names - {"alembic_version"} == PRE_EXISTING_TABLES


def test_downgrade_to_base_drops_every_table(db_url: str) -> None:
    upgrade_to_head(db_url)
    downgrade_to_base(db_url)

    engine = create_state_engine(db_url)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert (RUN_HISTORY_TABLES | PRE_EXISTING_TABLES).isdisjoint(table_names)
