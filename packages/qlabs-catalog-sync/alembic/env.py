"""Alembic environment script for the qlabs-catalog-sync state store.

Configured programmatically rather than via an ``alembic.ini`` at the repository or
package root (T2.2 does not own either location) --
:func:`qlabs_catalog_sync.state.migrate.build_alembic_config` builds the
``script_location``/``sqlalchemy.url`` options in Python and hands them to
:mod:`alembic.command`, which then imports this script. It works identically driven
straight from the CLI (``alembic -x db_url=sqlite:///state.db upgrade head`` from
within this ``alembic/`` directory, using ``-x`` since no ``sqlalchemy.url`` is baked
into a checked-in ini file) for manual/ops use.

Runs migrations on the same synchronous engine the state store itself uses
(:func:`qlabs_catalog_sync.state.db.create_state_engine`), so SQLite gets the same WAL
pragma hook at migration time as at runtime.
"""

from __future__ import annotations

from alembic import context

from qlabs_catalog_sync.state.db import create_state_engine
from qlabs_catalog_sync.state.models import Base

config = context.config

target_metadata = Base.metadata


def _resolve_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        url = config.get_x_argument(as_dictionary=True).get("db_url")
    if not url:
        raise RuntimeError(
            "no database URL configured: set sqlalchemy.url on the Alembic Config, "
            "or pass -x db_url=<url> on the command line"
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL against the target URL without opening a live connection."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection from a freshly built engine."""
    connectable = create_state_engine(_resolve_url())
    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
