"""``0001 -> 0002 -> base`` applies and rolls back cleanly; ``0002`` down is exactly ``0001``."""

from __future__ import annotations

from sqlalchemy import inspect

from qlabs_catalog_sync.state.db import create_state_engine
from qlabs_catalog_sync.state.migrate import (
    build_alembic_config,
    downgrade_to_base,
    upgrade_to_head,
)

CONFIG_TABLES = {
    "endpoints",
    "sync_pairs",
    "selection_rules",
    "selection_overrides",
    "config_generation",
    "config_changes",
}
T2_2_TABLES = {"identity_map", "watermarks", "field_envelopes", "orphan_log"}


def test_migration_creates_every_config_table_from_empty(db_url: str) -> None:
    upgrade_to_head(db_url)

    engine = create_state_engine(db_url)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert table_names >= CONFIG_TABLES
    # 0002 adds tables; it does not touch the T2.2 schema it migrates on top of.
    assert table_names >= T2_2_TABLES


def test_migration_sets_expected_primary_keys(db_url: str) -> None:
    upgrade_to_head(db_url)

    engine = create_state_engine(db_url)
    try:
        inspector = inspect(engine)
        pk = {
            table: tuple(inspector.get_pk_constraint(table)["constrained_columns"])
            for table in CONFIG_TABLES
        }
    finally:
        engine.dispose()

    assert pk["endpoints"] == ("name",)
    assert pk["sync_pairs"] == ("id",)
    assert pk["selection_rules"] == ("id",)
    assert pk["selection_overrides"] == ("id",)
    assert pk["config_generation"] == ("id",)
    assert pk["config_changes"] == ("id",)


def test_migration_sets_expected_foreign_keys(db_url: str) -> None:
    upgrade_to_head(db_url)

    engine = create_state_engine(db_url)
    try:
        inspector = inspect(engine)
        pair_fks = inspector.get_foreign_keys("sync_pairs")
        rule_fks = inspector.get_foreign_keys("selection_rules")
        override_fks = inspector.get_foreign_keys("selection_overrides")
    finally:
        engine.dispose()

    referred_columns = {
        (fk["constrained_columns"][0], fk["referred_table"]) for fk in pair_fks
    }
    assert referred_columns == {("source", "endpoints"), ("target", "endpoints")}
    assert all(fk["options"].get("ondelete") == "RESTRICT" for fk in pair_fks)

    assert rule_fks[0]["referred_table"] == "sync_pairs"
    assert rule_fks[0]["options"].get("ondelete") == "CASCADE"
    assert override_fks[0]["referred_table"] == "sync_pairs"
    assert override_fks[0]["options"].get("ondelete") == "CASCADE"


def test_downgrade_one_step_leaves_exactly_the_t2_2_schema(db_url: str) -> None:
    upgrade_to_head(db_url)

    from alembic import command

    command.downgrade(build_alembic_config(db_url), "0001")

    engine = create_state_engine(db_url)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    # alembic_version is alembic's own bookkeeping table, present at every revision.
    assert table_names - {"alembic_version"} == T2_2_TABLES


def test_downgrade_to_base_drops_every_table(db_url: str) -> None:
    upgrade_to_head(db_url)
    downgrade_to_base(db_url)

    engine = create_state_engine(db_url)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert (CONFIG_TABLES | T2_2_TABLES).isdisjoint(table_names)
