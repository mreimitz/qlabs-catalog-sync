"""Shared fixtures for state-store tests: a migrated temp-file SQLite database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore


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
    """``db_url``, migrated from empty to head before the test runs."""
    upgrade_to_head(db_url)
    return db_url


@pytest_asyncio.fixture
async def store(migrated_db_url: str) -> AsyncIterator[StateStore]:
    """A :class:`StateStore` bound to a fresh, fully migrated temp-file database."""
    state_store = StateStore.from_url(migrated_db_url)
    try:
        yield state_store
    finally:
        await state_store.aclose()
