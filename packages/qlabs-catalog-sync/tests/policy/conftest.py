"""Shared fixtures for the T2.5 manual-edit-policy tests.

Self-contained on purpose: this directory is a separate ownership boundary from
``tests/sync`` (T2.4), so it does not import that package's fixtures or helpers even
though the shapes are deliberately similar. Everything here is real, exactly as in
``tests/sync``: a migrated SQLite state store on a temp file, the real
:class:`~qlabs_catalog_sync.identity.IdentityResolver`, and two
:class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` instances -- a Databricks-shaped
read-only source and a Qlik-shaped write target -- whose in-memory stores, checksums and
:meth:`~qlabs_catalog_sync_sdk.testing.FakeConnector.simulate_external_edit` are genuine
behavior, not mocks. Proving "the loop overwrote a hand edit" or "the loop left a hand
edit alone" is only meaningful when the target the loop wrote to is a real one.

Only the loop's backoff sleep is stubbed, so a retry path (should any test need one)
runs instantly instead of actually waiting.
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
from qlabs_catalog_sync.sync.policy import ManualEditWritePolicy
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


async def no_sleep(seconds: float) -> None:
    """The loop's backoff, made instantaneous."""
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
    """One sync pair: Databricks-shaped source to Qlik-shaped target, data products only.

    ``manual_edit_policy`` is left at its default (source-wins for every field) --
    individual tests narrow it with ``pair.model_copy(update={...})``, exactly the
    pattern ``tests/sync`` uses for ``activation_opt_in``.

    ``activation_opt_in=True`` here, unlike ``tests/sync``'s pair fixture: D7's
    activation withholding is orthogonal to T2.5 and already covered where it belongs
    (``tests/sync/test_report_and_policy.py``). Opting in keeps every ``status`` field
    out of these tests' ``withheld`` assertions, so they read as being about the
    manual-edit policy alone -- the seam guarantees D7 runs first and unconditionally
    regardless of this setting, so it is not what is under test here.
    """
    return SyncPairConfig(
        name="db-to-qlik",
        source=SOURCE,
        target=TARGET,
        catalog_schema_patterns=["sales.*"],
        target_space="Sales Space",
        entity_types=[EntityType.DATA_PRODUCT],
        activation_opt_in=True,
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
    """Build a :class:`SyncLoop` over the fixtures, wired with the real T2.5 policy.

    ``write_policy`` defaults to :class:`~qlabs_catalog_sync.sync.policy.
    ManualEditWritePolicy` -- the thing under test -- and every other constructor
    argument can still be overridden per test.
    """

    def factory(**overrides: Any) -> SyncLoop:
        kwargs: dict[str, Any] = {
            "pair": pair,
            "source": source,
            "target": target,
            "store": store,
            "resolver": resolver,
            "clock": clock,
            "sleep": no_sleep,
            "write_policy": ManualEditWritePolicy(),
        }
        kwargs.update(overrides)
        return SyncLoop(**kwargs)

    return factory


# pytest runs with ``--import-mode=importlib``, which deliberately does not put a test
# directory on ``sys.path``. ``policy_helpers.py`` beside this file holds the shared
# builders, so make it importable from the test modules here.
sys.path.insert(0, str(Path(__file__).parent))
