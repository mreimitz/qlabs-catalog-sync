"""Object builders and a scripted :class:`~qlabs_catalog_sync.scheduler.PairRunner` double
shared by the scheduler tests.

``conftest.py`` puts this directory on ``sys.path`` (pytest runs with
``--import-mode=importlib``, which deliberately does not).

``ScriptedRunner`` is deliberately not a real :class:`~qlabs_catalog_sync.sync.loop.SyncLoop`
over a connector and a state store. What these tests prove is *when* and *how often*
``SyncScheduler`` calls ``run_cycle`` -- registration, cadence, jitter, overlap, failure
isolation, shutdown -- against a real ``apscheduler`` ``AsyncIOScheduler``. What happens
inside one cycle (diffing, writes, the state-store transaction) is T2.4's and T2.7's test
surface already, exercised there against real connectors and a real database; re-deriving
that here would test the same thing twice while leaving the scheduling behavior itself
under-proven.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from apscheduler.job import Job

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.scheduler import SyncScheduler
from qlabs_catalog_sync.sync.loop import RunStatus, SyncRunReport
from qlabs_catalog_sync_sdk.models import EntityType

#: A fixed instant, since these tests never care what time it is -- only how many cycles ran
#: and in what order.
START = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


def fire_now(job: Job) -> None:
    """Force ``job`` to look due right now, regardless of its trigger's real cadence.

    Every overlap/ordering test drives the scheduler by rewriting ``next_run_time`` directly
    and then waking the scheduler up, rather than waiting for real wall-clock intervals to
    elapse -- this is what makes those tests instant and non-flaky instead of sleep-based.
    """
    job.modify(next_run_time=datetime.now(tz=UTC) - timedelta(milliseconds=1))


async def pump(scheduler: SyncScheduler) -> None:
    """Force one round of ``apscheduler``'s due-job processing and let the event loop tick.

    ``AsyncIOScheduler.wakeup()`` is real, public API -- not a private method reached into
    for testing -- but it defers its work via ``call_soon_threadsafe``, so a single
    ``await asyncio.sleep(0)`` after it is what lets that deferred call, and any task it
    submits, actually run. A caller that needs to observe the *result* of a submitted
    coroutine job (as opposed to just the submit-or-skip decision, which happens
    synchronously inside the deferred call) still awaits its own synchronization primitive
    (``ScriptedRunner.started``, a completed task, ...) on top of this -- ``pump`` only
    guarantees the due-job processing pass itself has happened.
    """
    scheduler.scheduler.wakeup()
    await asyncio.sleep(0)


async def wait_until(predicate: object, *, timeout: float = 1.0) -> None:
    """Poll a zero-argument ``predicate`` on event-loop ticks (never real wall-clock delay)
    until it returns true, bounded by ``timeout`` only as a deadlock safety net."""
    assert callable(predicate)

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


def make_pair(
    *,
    name: str = "pair",
    source: str = "fake-source",
    target: str = "fake-target",
    cadence_seconds: int = 60,
    entity_types: list[EntityType] | None = None,
) -> SyncPairConfig:
    """A minimal, valid sync pair -- only the fields the scheduler actually reads vary."""
    return SyncPairConfig(
        name=name,
        source=source,
        target=target,
        catalog_schema_patterns=["sales.*"],
        target_space="Sales Space",
        entity_types=entity_types or [EntityType.DATA_PRODUCT],
        cadence_seconds=cadence_seconds,
    )


def make_report(
    pair: SyncPairConfig, entity_type: EntityType, status: RunStatus = RunStatus.OK
) -> SyncRunReport:
    """A ``run_cycle``-shaped report with ``status`` and nothing else interesting in it."""
    return SyncRunReport(
        pair=pair.name,
        source_endpoint=pair.source,
        target_endpoint=pair.target,
        entity_type=entity_type,
        status=status,
        started_at=START,
        finished_at=START,
        duration_seconds=0.0,
        committed=status is not RunStatus.FAILED,
    )


class ScriptedRunner:
    """A :class:`~qlabs_catalog_sync.scheduler.PairRunner` double whose ``run_cycle``
    behavior is entirely dictated by the test: return a fixed status, raise, or block on an
    ``asyncio.Event`` the test controls -- never a real ``time.sleep``/``asyncio.sleep(n)``,
    so a test can simulate "still running" deterministically without waiting on the clock.
    """

    def __init__(self, pair: SyncPairConfig, *, status: RunStatus = RunStatus.OK) -> None:
        self.pair = pair
        self.status = status
        self.calls: list[EntityType] = []
        self.finished: list[EntityType] = []
        self.bound_contexts: list[dict[str, str]] = []
        self.raises: bool = False
        #: Set by the test to make the *next* ``run_cycle`` call block until the test sets
        #: ``release``. ``started`` is set the moment the call actually blocks, so a test can
        #: ``await started.wait()`` instead of guessing how many event-loop ticks it takes
        #: apscheduler to dispatch the job -- no sleeping, no polling.
        self.block: bool = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        #: Set if a shutdown abandoned this call while it was blocked -- proves shutdown
        #: cancels rather than silently drops an over-budget cycle.
        self.cancelled: bool = False

    async def run_cycle(self, entity_type: EntityType) -> SyncRunReport:
        self.calls.append(entity_type)
        self.bound_contexts.append(dict(structlog.contextvars.get_contextvars()))
        if self.raises:
            raise RuntimeError("scripted run_cycle failure")
        if self.block:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        self.finished.append(entity_type)
        return make_report(self.pair, entity_type, self.status)
