"""Shared fixtures for identity tests.

Every test in this package runs against a **real, fully migrated SQLite state store** --
the same fixture shape the state-store tests use -- and a real review file on disk. There
are no mocks of the state store or of the filesystem here: the behaviors under test
(nothing binds itself, ambiguity is reported, the stored map is the only matcher after
binding, tenant scoping) are only meaningful when the database and its unique constraints
are really doing the work.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore

START = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


class Clock:
    """A deterministic clock that advances one second per read.

    Distinct-but-predictable timestamps are what let a test assert that
    ``first_proposed_at`` survived a re-run while ``last_seen_at`` moved on.
    """

    def __init__(self, start: datetime = START) -> None:
        self._now = start

    def __call__(self) -> datetime:
        current = self._now
        self._now += timedelta(seconds=1)
        return current


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def review_path(tmp_path: Path) -> Path:
    return tmp_path / "review" / "identity-review.json"


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
def resolver(store: StateStore, review_path: Path, clock: Clock) -> IdentityResolver:
    return IdentityResolver(store, review_path=review_path, clock=clock)


# pytest runs with ``--import-mode=importlib``, which deliberately does not put a test
# directory on ``sys.path``. ``helpers.py`` beside this file holds the shared object
# builders, so make it importable from the test modules in this directory.
sys.path.insert(0, str(Path(__file__).parent))
