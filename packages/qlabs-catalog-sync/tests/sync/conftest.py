"""Shared fixtures for the sync-loop tests.

Everything here is real. A migrated SQLite state store on a temp file, the real
:class:`~qlabs_catalog_sync.identity.IdentityResolver` over it, the real diff engine, and
two :class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` instances -- a Databricks-shaped
read-only source and a Qlik-shaped write target -- whose stores, changelogs, watermark
paging and idempotency semantics are genuine behavior, not mocks. The properties under test
(one transaction, zero writes on a re-run, a mid-cycle failure that commits nothing) are
only meaningful when the database and the connectors are really doing the work.

Only two things are ever stubbed: the loop's backoff sleep (so a retry test runs instantly)
and the metrics handle -- and that one is a recorder, not a mock.
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
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import SyncLoop
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector

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


class RecordingMetrics:
    """A real ``MetricsHandle`` that records every call instead of talking to Prometheus.

    Asserting on this is how the tests prove the loop emits T2.7's declared metric names
    (and only those) rather than inventing parallel ones.
    """

    def __init__(self) -> None:
        self.counters: list[tuple[str, float, dict[str, str]]] = []
        self.observations: list[tuple[str, float, dict[str, str]]] = []
        self.gauges: list[tuple[str, float, dict[str, str]]] = []

    def increment(self, name: str, *, value: float = 1.0, **labels: str) -> None:
        self.counters.append((name, value, dict(labels)))

    def observe(self, name: str, value: float, **labels: str) -> None:
        self.observations.append((name, value, dict(labels)))

    def set(self, name: str, value: float, **labels: str) -> None:
        self.gauges.append((name, value, dict(labels)))

    def total(self, name: str) -> float:
        """The summed value of every increment recorded for ``name``."""
        return sum(value for recorded, value, _ in self.counters if recorded == name)


async def no_sleep(seconds: float) -> None:
    """The loop's backoff, made instantaneous. Retries are still really performed."""
    return None


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[StateStore]:
    """A :class:`StateStore` on a fresh, fully migrated temp-file SQLite database."""
    url = f"sqlite:///{tmp_path / 'state.db'}"
    upgrade_to_head(url)
    state_store = StateStore.from_url(url)
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


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


@pytest.fixture
def make_loop(
    pair: SyncPairConfig,
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    resolver: IdentityResolver,
    metrics: RecordingMetrics,
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
            "metrics": metrics,
            "clock": clock,
            "sleep": no_sleep,
        }
        kwargs.update(overrides)
        return SyncLoop(**kwargs)

    return factory


# pytest runs with ``--import-mode=importlib``, which deliberately does not put a test
# directory on ``sys.path``. ``sync_helpers.py`` beside this file holds the shared builders
# and the failure-injecting target, so make it importable from the test modules here. The
# name is deliberately not the bare ``helpers`` another test package in this repository
# already claims -- two same-named top-level modules would shadow each other.
sys.path.insert(0, str(Path(__file__).parent))
