"""Shared fixtures for run-history tests.

A migrated temp-file SQLite database (through ``0003``, this task's own migration), a
:class:`~qlabs_catalog_sync.state.store.StateStore` and a
:class:`~qlabs_catalog_sync.runs.recorder.RunRecorder` sharing it, and everything needed
to run a real :class:`~qlabs_catalog_sync.sync.loop.SyncLoop` cycle against two
:class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` instances -- so the recorder is
proven against a genuine
:class:`~qlabs_catalog_sync.sync.loop.SyncRunReport`, not a hand-built one. Mirrors
``tests/sync/conftest.py``'s shape (not imported from it -- this package does not own
that file).
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import SyncLoop
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector

# pytest runs with ``--import-mode=importlib``, which deliberately does not put a test
# directory on ``sys.path``. ``run_history_helpers.py`` beside this file holds the shared
# parity/credential checkers and the unresolved-field target, so make it importable from
# the test modules here (same shim as ``tests/configstore/conftest.py``).
sys.path.insert(0, str(Path(__file__).parent))

START = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)

SOURCE = "fake-source"
TARGET = "fake-target"


class Clock:
    """A deterministic clock that advances one second per read."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start

    def __call__(self) -> datetime:
        current = self._now
        self._now += timedelta(seconds=1)
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
    """The SQLAlchemy URL for :func:`db_path`."""
    return f"sqlite:///{db_path}"


@pytest.fixture
def migrated_db_url(db_url: str) -> str:
    """``db_url``, migrated from empty to head (through ``0003``) before the test runs."""
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
def recorder(store: StateStore) -> RunRecorder:
    """A :class:`RunRecorder` sharing ``store``'s engine."""
    return RunRecorder.from_store(store)


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


@pytest.fixture
def make_loop(
    pair: SyncPairConfig,
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    resolver: IdentityResolver,
    clock: Clock,
) -> Callable[..., SyncLoop]:
    """Build a :class:`SyncLoop` over the fixtures, with any constructor override applied."""

    def factory(**overrides: Any) -> SyncLoop:
        kwargs: dict[str, Any] = {
            "pair": pair,
            "source": source,
            "target": target,
            "store": store,
            "resolver": resolver,
            "clock": clock,
            "sleep": no_sleep,
        }
        kwargs.update(overrides)
        return SyncLoop(**kwargs)

    return factory
