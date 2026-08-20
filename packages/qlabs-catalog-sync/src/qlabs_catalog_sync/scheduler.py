"""Scheduler — one APScheduler job per sync pair, cadence plus jitter, never overlapping itself.

WP2 / T2.6. This module owns exactly one decision: *when* :meth:`~qlabs_catalog_sync.sync.
loop.SyncLoop.run_cycle` gets called next, for which pair, for which entity type. It knows
nothing about connectors, diffing, or the state store — that is T2.4's job, already built,
and this module is the thing the module docstring there names directly: "Construct it once
per pair and call ``run_cycle`` on a cadence (T2.6's scheduler does exactly that)."

Built on ``apscheduler`` 3.11's ``AsyncIOScheduler`` (pinned ``apscheduler>=3.10,<4`` —
4.0 is still pre-release; RS-07 section 5 is explicit about not shipping it), because it is
exactly the "in-process, no extra infrastructure" scheduler RS-07 section 1 calls for, and
because its executor already implements the one behavior this task's DoD requires
(max-instances skip, not queue) rather than needing it hand-rolled.

One job per **pair**, not per entity type
------------------------------------------

:class:`~qlabs_catalog_sync.config.SyncPairConfig` carries exactly one ``cadence_seconds``
for the whole pair, not one per entity type in ``entity_types`` — so per-entity-type jobs
would either invent a cadence the config does not have, or force every entity type in a pair
onto identical timing artificially. A single job per pair matches the granularity the config
actually expresses. Firing the job walks ``pair.entity_types`` and calls ``run_cycle`` for
each in turn, *sequentially* — not fanned out concurrently — for two reasons: it keeps the
request burst against a shared Qlik target endpoint no worse than one entity type's worth at
a time (relevant at Qlik's 100 req/min write tier, RS-02 section 3.4), and it means
``max_instances=1`` on the one job protects the *whole* pair's cycle, not just one entity
type of it — a dataset cycle can never start while that same pair's data-product cycle is
still running.

A run_cycle failure for one entity type (``RunStatus.FAILED``, returned, not raised — see
:meth:`~qlabs_catalog_sync.sync.loop.SyncLoop.run_cycle`) does not stop the remaining entity
types in the same fire; each is independent. Only an actual exception escaping ``run_cycle``
— a violation of that documented contract, or a bug in this module's own loop — stops the
remaining entity types for *this* fire; the pair is retried whole on its next scheduled fire,
and every other pair's job is completely unaffected (APScheduler jobs share nothing but the
event loop).

Jitter: up to 10% of cadence, capped at 60 seconds
----------------------------------------------------

:func:`jitter_seconds_for` computes ``min(cadence_seconds * 0.10, 60.0)`` and that value is
passed as ``IntervalTrigger(..., jitter=...)``, which — per ``apscheduler.triggers.base.
BaseTrigger._apply_jitter`` — adds ``random.uniform(0, jitter)`` to every computed fire time.
It only ever *delays*, never advances a fire, which is exactly what "do not stampede the
target" needs: pairs sharing a cadence spread out over a trailing window instead of
clustering at one instant, and no pair fires *earlier* than its configured cadence.

The magnitude is a deliberate trade-off, not a guess. The MVP's default cadence is 900s (15
minutes, per ``SyncPairConfig.cadence_seconds``'s own docstring); Qlik's write tier (RS-02
section 3.4) is 100 requests/minute per user per tenant — the tier every ``create``/
``update`` call in this engine spends against. A synchronized fleet of pairs sharing a
tenant and a cadence is precisely how that limit gets hit: N pairs each opening their cycle
(and its first burst of writes) in the same instant multiplies against one shared budget. 10%
of cadence is enough spread to matter (90s of spread on a 15-minute cadence, the MVP default)
without meaningfully delaying a pair's freshness guarantee; the 60-second cap keeps that
spread from ballooning on the long cadences RS-07 section 6 suggests for large dataset/table
catalogs (30-60 minutes) — a pair does not need a 3-6 minute jitter window to avoid a
stampede, and an unbounded fraction would erode the cadence guarantee for no added safety.
Both numbers are named constants (:data:`DEFAULT_JITTER_FRACTION`, :data:`DEFAULT_
JITTER_CAP_SECONDS`) and overridable per :class:`SyncScheduler` instance, not hard-coded past
this module's edge.

The very first fire, deliberately, is *not* immediate. ``IntervalTrigger`` with no explicit
``start_date`` fires first at ``now + interval (+ jitter)``, never at ``now`` itself — which
means a process restart does not re-create the exact stampede jitter exists to prevent: every
configured pair coming due at once, right as the container comes up. :attr:`SyncScheduler`
accepts ``run_immediately`` for a caller that would rather trade that safety margin for a
faster first sync after a cold start; it defaults to ``False``.

``max_instances=1`` and ``coalesce=True``: skip, not queue — verified, not assumed
--------------------------------------------------------------------------------------

Both are passed explicitly on every job, even though they are also ``apscheduler``'s own
``job_defaults`` (``apscheduler.schedulers.base.BaseScheduler._job_defaults`` — verified by
reading the installed 3.11.3 source in this environment: ``coalesce`` defaults ``True``,
``max_instances`` defaults ``1``). Passing them explicitly is not redundant belt-and-braces
for its own sake: it makes the DoD's requirement legible at the one call site that grants it,
rather than resting on a library default that a future ``apscheduler`` upgrade could change
silently.

What "skip rather than queue" actually means, read out of ``apscheduler`` 3.11.3's own
``schedulers/base.py`` (``BaseScheduler._process_jobs``) and ``executors/base.py``
(``BaseExecutor.submit_job``): when a due job's executor already has ``max_instances``
running instances, ``submit_job`` raises ``MaxInstancesReachedError`` *before* the job
function is ever invoked for that fire — the run time is dropped, an ``EVENT_JOB_MAX_
INSTANCES`` event fires, and — critically — the job's ``next_run_time`` still advances past
that fire exactly as it would for a completed one. There is no retry, no backlog, and no
second attempt at the same fire once the running instance finishes; the next execution is the
*next* scheduled one. ``coalesce=True`` handles the adjacent case — if several fire times
accumulated while the job was *not* running (a paused scheduler, a slow host clock), only the
most recent of them runs, not one per missed interval. Together they are the whole "skipped,
not queued" guarantee, and :mod:`tests.scheduler.test_overlap` drives the real
``AsyncIOScheduler`` (not a mock of it) through exactly this sequence — a slow first cycle
still in flight when its interval elapses again — and counts actual invocations, which is
the only way to tell "skipped" and "queued-and-caught-up" apart from the outside.

Shutdown: wait for the in-flight cycle, bounded; never wait for one that has not started
--------------------------------------------------------------------------------------------

A SIGTERM must not cut a cycle off mid-write and it must not hang the container forever
either, so :meth:`SyncScheduler.shutdown` does three things in order:

1. **Pause first.** ``AsyncIOScheduler.pause()`` stops any *new* fire from being processed
   (``_process_jobs`` returns immediately while paused) without touching a job already
   handed to its executor — so this alone is what stops the fleet without disturbing whatever
   is currently running.
2. **Wait for whatever was already running, bounded by ``timeout``** (default :data:`DEFAULT_
   SHUTDOWN_TIMEOUT_SECONDS`, 30s). This module tracks the ``asyncio.Task`` for every
   in-flight pair fire itself (:attr:`SyncScheduler._inflight`) rather than trusting
   ``AsyncIOExecutor.shutdown()`` for this — that method unconditionally cancels every
   pending future regardless of its own ``wait`` argument (verified by reading ``executors/
   asyncio.py``: its ``shutdown`` docstring even says as much — "there is no way to honor
   ``wait=True``"), which is exactly the mid-write kill this method exists to avoid. Letting
   an in-flight cycle finish naturally is strictly better than cutting it off: the engine
   commits state in one transaction (T2.4), so an abandoned cycle can never leave it
   half-written, but a write already sent to Qlik and then abandoned before its commit still
   spent real budget against the 100 req/min write tier for nothing — finishing pays that
   cost off with an actual commit instead of throwing it away.
3. **Abandon what is still running past the timeout.** A cycle stuck long enough to blow the
   shutdown budget is logged and cancelled rather than left to block process exit
   indefinitely; per point 2, cancelling it is safe — nothing it did commits without reaching
   its single transaction — it is just the wasted-budget cost paid at that point instead of
   never.

Never waited for: a fire that has not yet been dispatched to the executor. Pausing in step 1
prevents any new one from starting, so there is nothing to wait for there by construction.

Health: a pair repeatedly failing is degraded, using T2.7's registry, not a new signal
------------------------------------------------------------------------------------------

:class:`~qlabs_catalog_sync.observability.HealthRegistry` already answers "is endpoint X
healthy" — :meth:`~qlabs_catalog_sync.sync.loop.SyncLoop.run_cycle` marks the source and
target endpoints healthy on a non-failed cycle, and degraded on an ``AuthError`` quarantine.
What it does not cover: a cycle that fails for a reason that never quarantines an endpoint
(a bug, an unexpected error) leaves both endpoints exactly where they were — genuinely
unreported. This module closes that gap the same way, through the same registry, keyed by
the *pair* name rather than an endpoint name (:class:`HealthRegistry` is a plain string-keyed
component map; a pair is as legitimate a component as an endpoint). After :attr:`SyncScheduler
.degraded_after` **consecutive** ``RunStatus.FAILED`` fires for one pair (default
:data:`DEFAULT_DEGRADED_AFTER`, 3 — one bad cycle is noise a transient retry already
absorbs inside ``run_cycle`` itself; three in a row is a pattern), the pair is marked
degraded; any non-``FAILED`` fire resets the counter and marks it healthy again.
``RunStatus.SKIPPED`` (this pair/entity-type combination is not configured to run at all) is
deliberately excluded from the failure count — it is a standing configuration fact that will
never resolve itself by retrying, and counting it would flap ``/healthz`` to degraded forever
for something no amount of waiting fixes.

No scheduler-specific metric is invented here. ``PrometheusMetrics`` (T2.7) exposes a fixed,
curated counter/histogram set and raises on any other name by design (see its docstring) —
precisely so cardinality stays intentional. ``run_cycle`` already emits ``qlabs_sync_cycle_
duration_seconds`` labeled by pair and entity type for every entity type this module calls it
for, which is the metric that answers "how is pair X doing"; there is nothing left for this
module to add without duplicating it under a different name. What this module *does* add is
structured logging: every fire is wrapped in :func:`~qlabs_catalog_sync.observability.
bind_sync_context` ``(pair=...)`` for the duration of every entity type's cycle plus the
scheduler's own lifecycle log lines, so a log line has the pair name whether it came from
this module or from deep inside ``run_cycle``.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
    JobEvent,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.observability import HealthRegistry, bind_sync_context, get_logger
from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.sync.loop import RunStatus, SyncRunReport
from qlabs_catalog_sync_sdk.models import EntityType

__all__ = [
    "DEFAULT_DEGRADED_AFTER",
    "DEFAULT_JITTER_CAP_SECONDS",
    "DEFAULT_JITTER_FRACTION",
    "DEFAULT_MISFIRE_GRACE_SECONDS",
    "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
    "PairRunner",
    "SyncScheduler",
    "jitter_seconds_for",
]

_LOG = get_logger("qlabs.catalog_sync.scheduler")


def _utc_now() -> datetime:
    """The wall clock, injectable so tests can pin run-history timestamps."""
    return datetime.now(UTC)

#: Jitter as a fraction of a pair's cadence — see the module docstring for the magnitude's
#: justification against Qlik's 100 req/min write tier (RS-02 section 3.4).
DEFAULT_JITTER_FRACTION: float = 0.10

#: Upper bound on jitter regardless of cadence, in seconds.
DEFAULT_JITTER_CAP_SECONDS: float = 60.0

#: Consecutive ``RunStatus.FAILED`` fires before a pair is marked degraded in the shared
#: :class:`~qlabs_catalog_sync.observability.HealthRegistry`.
DEFAULT_DEGRADED_AFTER: int = 3

#: How long, past its scheduled fire time, a due job may still be honored rather than logged
#: as missed. ``apscheduler``'s own default is 1 second — comfortable for a thread-pool
#: executor with dispatch latency, tight for ``AsyncIOExecutor`` sharing one event loop with
#: every other pair's cycle, where a large diff briefly monopolizing the loop should not cost
#: a pair its fire.
DEFAULT_MISFIRE_GRACE_SECONDS: int = 30

#: How long :meth:`SyncScheduler.shutdown` waits for an in-flight cycle before abandoning it.
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS: float = 30.0


def jitter_seconds_for(
    cadence_seconds: int,
    *,
    fraction: float = DEFAULT_JITTER_FRACTION,
    cap_seconds: float = DEFAULT_JITTER_CAP_SECONDS,
) -> float:
    """The jitter window for one pair's cadence: ``min(cadence * fraction, cap)``, floored at 0.

    Pure and deterministic in its bound, not its output — :class:`~apscheduler.triggers.
    interval.IntervalTrigger` draws a fresh ``random.uniform(0, jitter)`` per fire, so this
    function only fixes the *window's width*, not the actual per-fire delay.
    """
    if cadence_seconds <= 0 or fraction <= 0:
        return 0.0
    return max(0.0, min(cadence_seconds * fraction, cap_seconds))


class PairRunner(Protocol):
    """What the scheduler needs in order to fire one sync pair.

    :class:`~qlabs_catalog_sync.sync.loop.SyncLoop` satisfies this structurally — it exposes
    ``pair`` and an ``async def run_cycle(entity_type)`` — without this module importing it
    for anything but the :class:`~qlabs_catalog_sync.sync.loop.SyncRunReport`/``RunStatus``
    shapes ``run_cycle`` promises to return. A ``Protocol`` here, rather than a concrete
    dependency on ``SyncLoop``, is what lets ``tests/scheduler`` exercise real ``apscheduler``
    scheduling behavior against a tiny scripted double instead of a full connector/state-store
    stack — this module's tests are about *when* a cycle runs, not what happens inside one,
    and that is already T2.4's and T2.7's test surface.
    """

    @property
    def pair(self) -> SyncPairConfig:
        """The pair this runner cycles. Supplies the job id, cadence, and entity types."""
        ...

    async def run_cycle(self, entity_type: EntityType) -> SyncRunReport:
        """Run one cycle for ``entity_type``. Must never raise for a connector/engine
        failure — see :meth:`SyncLoop.run_cycle`'s documented contract, which this protocol
        exists to describe rather than duplicate."""
        ...


@dataclass(slots=True)
class _PairState:
    """Per-pair bookkeeping the scheduler keeps between fires."""

    consecutive_failures: int = 0


class SyncScheduler:
    """One ``AsyncIOScheduler`` job per :class:`PairRunner`, on that pair's cadence.

    Construct after building one :class:`~qlabs_catalog_sync.sync.loop.SyncLoop` per
    configured pair (each ``SyncLoop`` already carries everything needed to run every entity
    type that pair syncs); call :meth:`start` from inside a running asyncio event loop, and
    :meth:`shutdown` from a signal handler for that same loop. See the module docstring for
    the reasoning behind every default below.

    :param runners: One :class:`PairRunner` per sync pair; pair names must be unique (this is
        also enforced by :class:`~qlabs_catalog_sync.config.EngineConfig`, but this class does
        not assume it was built through one).
    :param health: Optional shared health registry; a pair is marked degraded after
        ``degraded_after`` consecutive failed fires and healthy again on the next non-failed
        one. ``None`` disables the signal entirely (nothing else in this class needs it).
    :param jitter_fraction: Fraction of cadence used as the jitter window, per pair.
    :param jitter_cap_seconds: Upper bound on the jitter window regardless of cadence.
    :param degraded_after: Consecutive failed fires before a pair is marked degraded.
    :param misfire_grace_seconds: How late a due fire may still run rather than be logged as
        missed and skipped — see :data:`DEFAULT_MISFIRE_GRACE_SECONDS`.
    :param run_immediately: Fire every pair once at (or near) startup instead of waiting a
        full cadence for the first cycle. Off by default — seer the module docstring's note
        on why an immediate first fire re-creates the stampede jitter exists to avoid, unless
        this is explicitly opted into (the immediate fire is still jittered, the same way a
        steady-state one is).
    :param scheduler: Inject a pre-built ``AsyncIOScheduler`` (a custom job store, for
        instance). Defaults to a fresh, otherwise-unconfigured one.
    """

    def __init__(
        self,
        *,
        runners: Sequence[PairRunner],
        health: HealthRegistry | None = None,
        jitter_fraction: float = DEFAULT_JITTER_FRACTION,
        jitter_cap_seconds: float = DEFAULT_JITTER_CAP_SECONDS,
        degraded_after: int = DEFAULT_DEGRADED_AFTER,
        misfire_grace_seconds: int = DEFAULT_MISFIRE_GRACE_SECONDS,
        run_immediately: bool = False,
        scheduler: AsyncIOScheduler | None = None,
        recorder: RunRecorder | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not runners:
            raise ValueError("SyncScheduler needs at least one pair runner")
        names = [runner.pair.name for runner in runners]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate pair name(s) across runners: {duplicates!r}")
        if degraded_after < 1:
            raise ValueError(f"degraded_after must be at least 1, got {degraded_after}")

        self._runners = list(runners)
        self._health = health
        self._jitter_fraction = jitter_fraction
        self._jitter_cap_seconds = jitter_cap_seconds
        self._degraded_after = degraded_after
        self._misfire_grace_seconds = misfire_grace_seconds
        self._run_immediately = run_immediately
        self._recorder = recorder
        self._clock = clock
        self._scheduler: AsyncIOScheduler = (
            scheduler if scheduler is not None else AsyncIOScheduler()
        )
        self._states: dict[str, _PairState] = {name: _PairState() for name in names}
        self._inflight: set[asyncio.Task[None]] = set()
        self._started = False
        self._scheduler.add_listener(
            self._on_job_event, EVENT_JOB_MAX_INSTANCES | EVENT_JOB_MISSED | EVENT_JOB_ERROR
        )

    # -- properties -------------------------------------------------------------------------

    @property
    def scheduler(self) -> AsyncIOScheduler:
        """The underlying ``AsyncIOScheduler``, exposed read/inspect-only.

        This class configures it; it does not hide it. A caller (or a test) that needs to
        know exactly what was registered — job count, trigger, ``max_instances`` — reads it
        here rather than this class re-exposing every one of ``apscheduler``'s own accessors.
        """
        return self._scheduler

    @property
    def pairs(self) -> tuple[str, ...]:
        """The pair names this scheduler was built with, in registration order."""
        return tuple(self._states)

    def consecutive_failures(self, pair_name: str) -> int:
        """How many fires in a row this pair has failed, right now. Resets on any other
        outcome; see the module docstring for why ``RunStatus.SKIPPED`` does not count."""
        return self._states[pair_name].consecutive_failures

    # -- lifecycle --------------------------------------------------------------------------

    def jitter_for(self, pair: SyncPairConfig) -> float:
        """This scheduler's configured jitter window for ``pair``'s cadence."""
        return jitter_seconds_for(
            pair.cadence_seconds,
            fraction=self._jitter_fraction,
            cap_seconds=self._jitter_cap_seconds,
        )

    def start(self) -> None:
        """Register one job per pair and start firing cycles.

        Must be called from inside a running asyncio event loop — ``AsyncIOScheduler`` binds
        to ``asyncio.get_running_loop()`` the moment it starts. Every job is registered before
        the scheduler itself starts, so the first fire of any pair can never race the
        registration of another.
        """
        if self._started:
            raise RuntimeError("SyncScheduler.start() called twice")
        for runner in self._runners:
            self._add_job(runner)
        self._scheduler.start()
        self._started = True
        _LOG.info("scheduler.started", pairs=list(self.pairs))

    async def shutdown(self, *, timeout: float | None = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Stop firing new cycles, then wait for whatever is already running.

        See the module docstring's "Shutdown" section for the full reasoning. In short: pause
        first (no new fire is processed from this point on), wait up to ``timeout`` seconds
        (``None`` waits indefinitely) for every currently in-flight pair fire to finish on its
        own, and cancel whatever is still running past that bound. Safe to call when nothing
        is running, and safe to call more than once.
        """
        if not self._started:
            return
        self._scheduler.pause()
        inflight = list(self._inflight)
        if inflight:
            _LOG.info("scheduler.shutdown.waiting", inflight=len(inflight), timeout=timeout)
            _done, pending = await asyncio.wait(inflight, timeout=timeout)
            if pending:
                _LOG.warning(
                    "scheduler.shutdown.abandoning",
                    abandoned=len(pending),
                    detail=(
                        "cycle(s) still running past the shutdown timeout; nothing they "
                        "planned commits without reaching the engine's single transaction, "
                        "so this is safe, just wasted write-tier budget"
                    ),
                )
                for task in pending:
                    task.cancel()
                # Let the cancellation actually land -- ScriptedRunner-style callers that
                # observe it (and this class's own ``_inflight`` bookkeeping, via each
                # task's ``finally``) must see it happen before ``shutdown`` reports done.
                await asyncio.wait(pending)
        self._scheduler.shutdown(wait=False)
        # AsyncIOScheduler.shutdown() only *schedules* its real teardown (state -> STOPPED,
        # executors/jobstores closed) via call_soon_threadsafe rather than running it inline
        # -- a single tick is enough to let that deferred call run before this method
        # reports the scheduler stopped (verified against the installed apscheduler 3.11.3
        # source: ``AsyncIOScheduler._shutdown`` is the ``@run_in_event_loop``-wrapped one).
        await asyncio.sleep(0)
        self._started = False
        _LOG.info("scheduler.stopped")

    # -- job registration ---------------------------------------------------------------------

    def _add_job(self, runner: PairRunner) -> None:
        pair = runner.pair
        jitter = self.jitter_for(pair)
        trigger = IntervalTrigger(
            seconds=pair.cadence_seconds,
            jitter=jitter if jitter > 0 else None,
            timezone=UTC,
        )
        job_kwargs: dict[str, object] = {
            "trigger": trigger,
            "args": [runner],
            "id": pair.name,
            "name": f"sync-pair:{pair.name}",
            "max_instances": 1,
            "coalesce": True,
            "misfire_grace_time": self._misfire_grace_seconds,
            "replace_existing": False,
        }
        first_run = self._first_run_time(pair, jitter)
        if first_run is not None:
            job_kwargs["next_run_time"] = first_run
        self._scheduler.add_job(self._run_pair, **job_kwargs)

    def _first_run_time(self, pair: SyncPairConfig, jitter: float) -> datetime | None:
        """``None`` to accept ``IntervalTrigger``'s own default (first fire one cadence out),
        or an explicit, still-jittered near-immediate time when ``run_immediately`` is set."""
        if not self._run_immediately:
            return None
        offset = random.uniform(0, jitter) if jitter > 0 else 0.0
        return datetime.now(tz=UTC) + timedelta(seconds=offset)

    # -- firing ---------------------------------------------------------------------------

    async def _run_pair(self, runner: PairRunner) -> None:
        """One fire of one pair's job: every configured entity type, in turn.

        Registered as the job function itself, so ``apscheduler``'s own ``max_instances``
        check guards *this whole method* — the only reentrancy protection this class needs;
        see the module docstring for why a second, application-level lock would be redundant.
        """
        task = asyncio.current_task()
        if task is not None:
            self._inflight.add(task)
        try:
            await self._run_pair_body(runner)
        finally:
            if task is not None:
                self._inflight.discard(task)

    async def _run_pair_body(self, runner: PairRunner) -> None:
        pair = runner.pair
        with bind_sync_context(pair=pair.name):
            reports: list[SyncRunReport] = []
            failed = False
            try:
                for entity_type in pair.entity_types:
                    run_id = await self._begin_run(pair, entity_type)
                    try:
                        report = await runner.run_cycle(entity_type)
                    except Exception as exc:
                        # run_cycle is documented never to raise, but it can: it loads the
                        # stored watermark before entering its own try block, so a state
                        # store failure there escapes. Closing the run out here is what
                        # stops a crashed cycle leaving a row stuck at RUNNING forever.
                        await self._fail_run(run_id, exc)
                        raise
                    await self._finish_run(run_id, report)
                    reports.append(report)
                    if report.status is RunStatus.FAILED:
                        failed = True
            except Exception:
                # SyncLoop.run_cycle is documented never to raise for a connector or engine
                # failure -- it returns a FAILED report instead, precisely so the scheduler
                # keeps running. Reaching here means that contract was violated (or this
                # method has a bug of its own); either way, one pair's crash must not take
                # the scheduler -- or any other pair's job -- down with it.
                failed = True
                _LOG.exception("scheduler.pair.crashed", pair=pair.name)
            self._record_outcome(pair.name, failed=failed, reports=reports)

    async def _begin_run(self, pair: SyncPairConfig, entity_type: EntityType) -> uuid.UUID | None:
        """Open a run-history row for one cycle, or ``None`` when history is not configured.

        Run history is optional so ``tests/scheduler`` can keep exercising scheduling
        behaviour against a scripted double with no database at all, and so an operator
        running without it loses reporting rather than the ability to sync.
        """
        if self._recorder is None:
            return None
        return await self._recorder.start(
            pair=pair.name,
            source_endpoint=pair.source,
            target_endpoint=pair.target,
            entity_type=entity_type,
            dry_run=False,
            started_at=self._clock(),
        )

    async def _finish_run(self, run_id: uuid.UUID | None, report: SyncRunReport) -> None:
        """Close a run out from the report the cycle actually produced."""
        if self._recorder is None or run_id is None:
            return
        await self._recorder.finish(run_id, report)

    async def _fail_run(self, run_id: uuid.UUID | None, exc: BaseException) -> None:
        """Close a run out as failed when the cycle raised instead of returning a report.

        Never raises: a run-history write that fails must not turn a recoverable cycle
        crash into a scheduler crash, and the original exception is the one worth
        propagating.
        """
        if self._recorder is None or run_id is None:
            return
        try:
            await self._recorder.fail(
                run_id,
                message=f"{type(exc).__name__}: {exc}",
                finished_at=self._clock(),
            )
        except Exception:
            _LOG.exception("scheduler.run_history.fail_write_failed", run_id=str(run_id))

    def _record_outcome(
        self, pair_name: str, *, failed: bool, reports: Sequence[SyncRunReport]
    ) -> None:
        state = self._states[pair_name]
        if failed:
            state.consecutive_failures += 1
            _LOG.warning(
                "scheduler.pair.failed",
                pair=pair_name,
                consecutive_failures=state.consecutive_failures,
            )
            if self._health is not None and state.consecutive_failures >= self._degraded_after:
                self._health.mark_degraded(
                    pair_name,
                    reason=(
                        f"{state.consecutive_failures} consecutive failed sync cycles "
                        "(see qlabs_sync_errors_total and the run reports for detail)"
                    ),
                )
        else:
            if state.consecutive_failures and self._health is not None:
                _LOG.info("scheduler.pair.recovered", pair=pair_name)
            state.consecutive_failures = 0
            if self._health is not None:
                self._health.mark_healthy(pair_name)
        _LOG.info(
            "scheduler.pair.cycle_finished",
            pair=pair_name,
            entity_types=[report.entity_type.value for report in reports],
            statuses=[report.status.value for report in reports],
        )

    # -- apscheduler event bridge ------------------------------------------------------------

    def _on_job_event(self, event: JobEvent) -> None:
        """Structured-log the ``apscheduler`` events this class cares about.

        Only a log line: nothing here feeds :class:`HealthRegistry` or the failure counter —
        those are driven from the reports :meth:`_run_pair_body` actually observed, which is
        a strictly more informative signal than "a fire was skipped" (a healthy, fast pair
        that happens to overlap its own slow neighbor cycle is not the same condition as a
        pair whose cycles keep failing).
        """
        if event.code == EVENT_JOB_MAX_INSTANCES:
            _LOG.warning(
                "scheduler.pair.fire_skipped",
                pair=event.job_id,
                detail=(
                    "the previous cycle for this pair was still running; this fire was "
                    "skipped, not queued, and the job's schedule already advanced past it"
                ),
            )
        elif event.code == EVENT_JOB_MISSED:
            _LOG.warning(
                "scheduler.pair.fire_missed",
                pair=event.job_id,
                detail="fire time exceeded the misfire grace period before it could run",
            )
        elif event.code == EVENT_JOB_ERROR:
            _LOG.error(
                "scheduler.pair.job_error",
                pair=event.job_id,
                detail="the job callback raised outside run_cycle's documented contract",
            )
