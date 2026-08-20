"""Shared fixtures for configstore tests: a migrated temp-file SQLite database.

Deliberately does not reuse ``tests/state/conftest.py``'s ``store`` fixture --
:class:`~qlabs_catalog_sync.state.store.StateStore` is T2.2's async wrapper around the
*state* tables (``identity_map``, ``watermarks``, ...) and this task does not extend it.
These tests exercise the configuration models directly through a plain synchronous
:class:`~sqlalchemy.orm.Session`, which is all a schema/migration task needs.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from qlabs_catalog_sync.state.db import create_state_engine
from qlabs_catalog_sync.state.migrate import upgrade_to_head

# pytest runs with ``--import-mode=importlib``, which deliberately does not put a test
# directory on ``sys.path``. ``configstore_helpers.py`` beside this file holds the shared
# row factories and the credential scanner, so make it importable from the test modules
# here (same shim as ``tests/scheduler/conftest.py``).
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A path to a not-yet-existing SQLite file, unique per test."""
    return tmp_path / "state.db"


@pytest.fixture
def db_url(db_path: Path) -> str:
    """The SQLAlchemy URL for :func:`db_path`."""
    return f"sqlite:///{db_path}"


@pytest.fixture
def migrated_db_url(db_url: str) -> str:
    """``db_url``, migrated from empty to head (through ``0002``) before the test runs."""
    upgrade_to_head(db_url)
    return db_url


@pytest.fixture
def engine(migrated_db_url: str) -> Iterator[Engine]:
    """A :class:`~sqlalchemy.Engine` bound to a fresh, fully migrated temp-file database.

    Built through :func:`create_state_engine`, so SQLite foreign-key enforcement (``PRAGMA
    foreign_keys=ON``) is on -- the same as production -- which is what makes the FK/cascade
    tests in this package meaningful.
    """
    eng = create_state_engine(migrated_db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A :class:`~sqlalchemy.orm.Session` bound to :func:`engine`, closed after the test."""
    factory = sessionmaker(bind=engine)
    with factory() as sess:
        yield sess
