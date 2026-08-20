"""Shared fixtures for the restart-safety tests (T8.4).

Everything here is real. A migrated SQLite state store on a temp file, the real
:class:`~qlabs_catalog_sync.identity.IdentityResolver` over it, the real diff engine,
and two :class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` instances -- a
Databricks-shaped read-only source and a Qlik-shaped write target -- whose stores,
changelogs, watermark paging and idempotency semantics are genuine behavior, not mocks.

``db_url`` is exposed separately from ``store`` (rather than folding the two together,
as ``tests/sync/conftest.py`` does) because the whole point of this suite's restart
tests is to build a *second*, independent :class:`StateStore` -- a fresh SQLAlchemy
engine and session factory -- against the very same on-disk file, simulating the sync
engine's process restarting while Databricks and Qlik (the two connector fixtures) do
not.

Only two things are ever stubbed: the loop's backoff sleep (so a retry runs instantly)
and the clock (deterministic, so a watermark token compares equal across two
independently-built loops in the same test).
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Callable
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

# pytest runs with --import-mode=importlib, which deliberately does not put a test
# directory on sys.path; restart_helpers.py beside this file holds this suite's shared
# builders, so make it importable from the test modules here (the same insert every
# other test package's conftest.py does for its own helpers module). Must happen before
# the module-level import below.
sys.path.insert(0, str(Path(__file__).parent))

from restart_helpers import (  # noqa: E402
    SOURCE_ENDPOINT,
    TARGET_ENDPOINT,
    Clock,
    no_sleep,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A path to a not-yet-existing SQLite file, unique per test."""
    return tmp_path / "restart-state.db"


@pytest.fixture
def db_url(db_path: Path) -> str:
    """The migrated SQLAlchemy URL for :func:`db_path`.

    Kept as a plain string (not a :class:`StateStore`) so a test can build more than
    one independent store against it -- exactly what "a fresh engine resumes from the
    persisted watermark" needs to prove.
    """
    url = f"sqlite:///{db_path}"
    upgrade_to_head(url)
    return url


@pytest_asyncio.fixture
async def store(db_url: str) -> AsyncIterator[StateStore]:
    """A :class:`StateStore` on the migrated temp-file database."""
    state_store = StateStore.from_url(db_url)
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
    return FakeConnector.read_only_source(name=SOURCE_ENDPOINT)


@pytest.fixture
def target() -> FakeConnector:
    """A Qlik-shaped write target -- the sole write connector in v1."""
    return FakeConnector.write_target(name=TARGET_ENDPOINT)


@pytest.fixture
def pair() -> SyncPairConfig:
    """One sync pair: Databricks-shaped source to Qlik-shaped target, data products only."""
    return SyncPairConfig(
        name="db-to-qlik",
        source=SOURCE_ENDPOINT,
        target=TARGET_ENDPOINT,
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
