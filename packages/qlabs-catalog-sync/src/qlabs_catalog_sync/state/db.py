"""State store engine construction.

Builds the SQLAlchemy engine the state store and the Alembic migration both run on.
For SQLite, a ``connect`` hook turns on WAL journaling (plus ``synchronous=NORMAL``
and ``foreign_keys=ON``) on every new DBAPI connection, rather than assuming a
database file is already in WAL mode -- a hook is what makes a brand-new file WAL
from its very first connection instead of relying on a separate one-time setup step
that could be skipped. The same schema (:mod:`qlabs_catalog_sync.state.models`) runs
unchanged on PostgreSQL later; this module is the only place that knows SQLite needs
tuning at all, and it does nothing for non-SQLite dialects.

Async note: the engine here is a synchronous :class:`sqlalchemy.Engine`, not
SQLAlchemy's asyncio engine. SQLAlchemy's async engine requires an async DBAPI driver
(``aiosqlite`` for SQLite, ``asyncpg``/``psycopg`` for PostgreSQL); none is a pinned
dependency of this package (``uv.lock`` has no ``aiosqlite`` entry) and T2.2 does not
own ``pyproject.toml``/``uv.lock`` to add one. :class:`~qlabs_catalog_sync.state.store.StateStore`
keeps its public surface fully ``async def`` regardless, by running the blocking
session calls in a worker thread (:func:`asyncio.to_thread`) -- the same technique
``aiosqlite`` itself uses internally. Swapping to a native async engine later, once a
driver dependency exists, is a change inside this module only.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event

__all__ = ["create_state_engine"]


def create_state_engine(url: str, *, echo: bool = False) -> Engine:
    """Build the engine the state store runs on.

    ``url`` is any SQLAlchemy database URL (``sqlite:///path/to/state.db``,
    ``sqlite:///:memory:``, or a ``postgresql+psycopg://...`` URL later). WAL mode is
    only meaningful for SQLite and is skipped entirely for other dialects.
    """
    engine = create_engine(url, echo=echo, future=True)
    if engine.dialect.name == "sqlite":
        _enable_sqlite_wal(engine)
    return engine


def _enable_sqlite_wal(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
