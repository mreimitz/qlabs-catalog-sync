"""Shared fixtures for the orphan-lifecycle tests (T2.9).

Mirrors ``tests/sync/conftest.py`` and ``tests/state/conftest.py``: everything here is
real -- a migrated SQLite state store on a temp file (with its URL exposed separately,
so a restart test can reopen the same database) and
:class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` instances whose stores,
changelogs and idempotency semantics are genuine behavior, not mocks.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector

START = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)

SOURCE = "fake-source"
TARGET = "fake-target"


class Clock:
    """A deterministic clock that advances one minute per read."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start

    def __call__(self) -> datetime:
        current = self._now
        self._now += timedelta(minutes=1)
        return current


async def no_sleep(seconds: float) -> None:
    """The loop's backoff, made instantaneous."""
    return None


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A path to a not-yet-existing SQLite file, unique per test."""
    return tmp_path / "state.db"


@pytest.fixture
def db_url(db_path: Path) -> str:
    """The SQLAlchemy URL for :func:`db_path`, exposed so a test can reopen it."""
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


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def resolver(store: StateStore, tmp_path: Path, clock: Clock) -> IdentityResolver:
    return IdentityResolver(store, review_path=tmp_path / "identity-review.json", clock=clock)


@pytest.fixture
def source() -> FakeConnector:
    """A Databricks-shaped, read-only source connector."""
    return FakeConnector.read_only_source(name=SOURCE)


@pytest.fixture
def target() -> FakeConnector:
    """A Qlik-shaped write target -- the sole write connector in v1."""
    return FakeConnector.write_target(name=TARGET)


@pytest.fixture
def pair() -> SyncPairConfig:
    """One sync pair: Databricks-shaped source to Qlik-shaped target, data products only."""
    return SyncPairConfig(
        name="db-to-qlik",
        source=SOURCE,
        target=TARGET,
        catalog_schema_patterns=["sales.*"],
        target_space="Sales Space",
        entity_types=[EntityType.DATA_PRODUCT],
    )


# pytest runs with ``--import-mode=importlib``, which deliberately does not put a test
# directory on ``sys.path``. ``orphans_helpers.py`` beside this file holds the shared
# builders, so make it importable from the test modules here (same pattern as
# tests/sync/conftest.py; the module name is distinct from that package's own
# ``sync_helpers.py`` so neither shadows the other).
sys.path.insert(0, str(Path(__file__).parent))
