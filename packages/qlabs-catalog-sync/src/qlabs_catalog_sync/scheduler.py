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

Reconcile: configuration changes take effect without a restart (C1)
------------------------------------------------------------------------

WP12 / T12.9. RM-01 fixed a process's pair set at startup: the pairs were environment
configuration, so changing one meant a restart. C1 moves configuration into the state store
and requires the opposite — *"Configuration changes take effect without a restart. Every
write bumps a generation counter, the scheduler reconciles its job set against the database
on a short interval, and a cycle already in flight keeps the configuration it started with.
``max_instances=1`` per pair is unchanged."* This module implements the second and third
clauses; :mod:`qlabs_catalog_sync.configstore` already implements the first.

**Optional, exactly like ``recorder``.** A :class:`SyncScheduler` built without a
``config_source`` behaves precisely as it did before this task — fixed pairs, no database,
no reconcile job — which is what lets ``tests/scheduler`` keep proving real ``apscheduler``
behaviour against a scripted double with no state store at all, and what makes a deployment
that cannot reach its configuration store lose *editability*, never the ability to sync.

**Cheap when nothing changed.** Every tick reads one number:
``config_generation.generation``, a single indexed row. Only when that number moves is the
configuration itself read, and only a pair whose :attr:`PairPlan.fingerprint` actually
changed has its runner rebuilt — so an operator editing pair A never churns pair B's
connectors or resets its schedule. A reconcile that rebuilt every runner on every tick would
hammer the database and re-``setup()`` connectors for nothing; this one, in the steady state,
costs one row read per interval per process.

**Five seconds, and why that number.** :data:`DEFAULT_RECONCILE_INTERVAL_SECONDS` is 5.0. The
cost side is a primary-key lookup on a one-row table — about 17k of them a day, against a
database that already absorbs a full cycle's worth of envelope reads every 15 minutes; it is
not a number worth optimising. The benefit side is human: an operator who saves a change in
the console and watches the pair list wants the engine to have noticed by the time they look
back at it. At five seconds that reads as immediate; at thirty it reads as broken and they
save again. Faster than a second buys nothing anyone can perceive and only adds log noise and
wakeups. A route that needs a change to land *now* rather than within an interval can await
:meth:`SyncScheduler.reconcile` directly. The interval is a constructor argument, not a
constant past this module's edge.

**Reconcile is an ordinary job.** See :meth:`SyncScheduler._add_reconcile_job` for the full
argument; in short, registering it as an APScheduler job rather than a bare ``asyncio`` task
means the shutdown pause already stops it, ``max_instances=1`` already prevents a slow
reconcile overlapping itself, and one ``get_jobs()`` call still answers "what is this process
doing".

**A cycle in flight keeps the configuration it started with — by construction.** A pair's job
carries its runner in ``args``, and ``apscheduler`` binds a job's args at *dispatch* time
(``executors/base.py``'s ``run_job``/``run_coroutine_job`` call ``job.func(*job.args)`` on the
job object captured when the fire was submitted). Reconcile never mutates a live runner; it
builds a new one and swaps the job. So a cycle that is already running holds the old runner —
old rule set, old cadence, old policy — until it finishes, and the new configuration decides
the *next* fire. This is a property of how the swap is done, not a rule someone has to
remember not to break.

**``max_instances=1`` survives a swap.** The executor counts running instances per job *id*,
in a dict it owns (``BaseExecutor._instances``), not in the stored job — so removing a job and
re-adding one under the same id while a cycle is running does not reset that count, and the
replacement job's first due fire is skipped exactly as an overlapping fire of the original
would be. ``tests/api/test_reconcile.py`` drives that sequence against the real scheduler
rather than trusting the reading.

**A withdrawn pair's cycle is not abandoned.** Removing or disabling a pair removes its job
immediately, so it can never fire again — but a cycle already running is left to finish, the
same trade :meth:`SyncScheduler.shutdown` makes and for the same reason (writes already sent
have spent Qlik write-tier budget; finishing converts that into a committed result). A
configuration edit is not an emergency stop.

**A broken pair is reported, not fatal.** A pair whose rows will not translate keeps whatever
job it has, rather than being unscheduled because someone saved a malformed rule; a pair whose
runner will not build is logged, marked degraded in the shared
:class:`~qlabs_catalog_sync.observability.HealthRegistry`, and retried with backoff. Neither
stops any other pair being reconciled in the same pass.

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
import contextlib
import hashlib
import json
import random
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
    JobEvent,
)
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.configstore.models import (
    EndpointRow,
    SelectionOverrideRow,
    SelectionRuleRow,
    SyncPairRow,
)
from qlabs_catalog_sync.configstore.runtime import (
    INERT_CATALOG_SCHEMA_PATTERN,
    sync_pair_config_for_row,
)
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.configstore.types import RuleScope
from qlabs_catalog_sync.observability import HealthRegistry, bind_sync_context, get_logger
from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.selection.rules import SelectionRuleSet
from qlabs_catalog_sync.sync.loop import RunStatus, SyncRunReport
from qlabs_catalog_sync_sdk.models import EntityType

__all__ = [
    "DEFAULT_DEGRADED_AFTER",
    "DEFAULT_JITTER_CAP_SECONDS",
    "DEFAULT_JITTER_FRACTION",
    "DEFAULT_MISFIRE_GRACE_SECONDS",
    "DEFAULT_RECONCILE_INTERVAL_SECONDS",
    "DEFAULT_RETRY_BACKOFF_TICK_CAP",
    "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
    "INERT_CATALOG_SCHEMA_PATTERN",
    "RECONCILE_JOB_ID",
    "ConfigSnapshot",
    "ConfigSource",
    "ConfigStorePairSource",
    "PairLoadFailure",
    "PairPlan",
    "PairRunner",
    "ReconcileResult",
    "RunnerFactory",
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

#: How often the scheduler probes the configuration generation counter (C1). See the module
#: docstring's "Reconcile" section for why five seconds, and what one probe actually costs.
DEFAULT_RECONCILE_INTERVAL_SECONDS: float = 5.0

#: Upper bound, in reconcile ticks, on the backoff between retries of a pair whose runner
#: could not be built. At the default interval this is a retry roughly every 160 seconds for
#: a persistently broken pair, and every tick again the moment the generation moves.
DEFAULT_RETRY_BACKOFF_TICK_CAP: int = 32

#: The job id the reconcile job itself is registered under. Reserved: a sync pair may not use
#: it as a name, because one APScheduler job store is keyed by a single flat id space.
RECONCILE_JOB_ID: Final[str] = "__reconcile__"

#: The placeholder :attr:`~qlabs_catalog_sync.config.SyncPairConfig.catalog_schema_patterns`
#: value used for a store-configured pair that has no object-scope include-glob rule to


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


# ==========================================================================================
# Reconcile against the configuration store (C1)
# ==========================================================================================


@dataclass(frozen=True, slots=True)
class PairPlan:
    """One pair's complete scheduling configuration, frozen as of one generation.

    A plan is the *whole* input a :data:`RunnerFactory` needs to build that pair's runner:
    the :class:`~qlabs_catalog_sync.config.SyncPairConfig` (cadence, entity types, target
    space, manual-edit policy, activation opt-in) and the compiled
    :class:`~qlabs_catalog_sync.selection.rules.SelectionRuleSet` its cycles decide scope
    against (C3/C4). Frozen, and never mutated in place: a cycle in flight holds the runner
    built from *its* plan, and a configuration change produces a new plan and a new runner
    rather than editing the one the running cycle is using. That is the whole mechanism
    behind C1's "a cycle already in flight keeps the configuration it started with".

    :param fingerprint: an opaque, stable digest of every stored value the plan was built
        from. Two plans with equal fingerprints are the same configuration, so the scheduler
        can tell "the generation moved because *some other* pair was edited" from "this pair
        changed" without rebuilding a runner to find out.
    :param jitter_seconds: the pair's explicit jitter override (``sync_pairs.jitter_seconds``),
        or ``None`` to use :meth:`SyncScheduler.jitter_for`'s computed window.
    """

    pair: SyncPairConfig
    selection_rules: SelectionRuleSet
    fingerprint: str
    jitter_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class PairLoadFailure:
    """A stored pair that is enabled but could not be turned into a :class:`PairPlan`.

    Deliberately *not* the same thing as "absent": a pair whose rows will not translate
    (an entity-type list that is empty, a selection rule whose pattern is malformed) keeps
    whatever job it already has rather than being unscheduled, because a broken edit must
    not silently stop a pair that was syncing fine a moment ago. See
    :meth:`SyncScheduler.reconcile`.
    """

    pair: str
    reason: str


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """Everything one read of the configuration store produced: the plans, and the refusals."""

    plans: tuple[PairPlan, ...] = ()
    failures: tuple[PairLoadFailure, ...] = ()


class ConfigSource(Protocol):
    """Where :class:`SyncScheduler` reads the desired job set from.

    Two methods with deliberately different costs. :meth:`generation` is the cheap probe run
    on every reconcile tick — one indexed single-row read; :meth:`load` is the real read, run
    only when that number moved. A source that made them equally expensive would defeat the
    whole point of the counter.

    A ``Protocol`` rather than a concrete dependency on
    :class:`~qlabs_catalog_sync.configstore.service.ConfigService` for the same reason
    :class:`PairRunner` is one: it keeps ``tests/scheduler`` able to exercise real
    ``apscheduler`` behaviour with no database at all, and it makes the environment-declared
    and store-declared cases the same code path from this class's point of view.
    """

    async def generation(self) -> int:
        """The current configuration generation. Must be cheap; called every tick."""
        ...

    async def load(self) -> ConfigSnapshot:
        """Every pair that should currently be scheduled, plus the ones that would not load."""
        ...


#: How :class:`SyncScheduler` turns a :class:`PairPlan` into something it can fire.
#:
#: Async because building a runner may have to reach a connector — an endpoint added through
#: the console has had no ``setup()`` called on it yet. Supplied by the caller that owns the
#: connector pool and the state store (``cli/serve_command.py``), never built here: this
#: module knows *when* a cycle runs, and has never known what a cycle is made of.
RunnerFactory = Callable[[PairPlan], Awaitable[PairRunner]]


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What one :meth:`SyncScheduler.reconcile` pass actually did.

    :param generation: the generation this pass observed.
    :param reloaded: whether the full configuration was read. ``False`` is the steady state —
        the generation had not moved and nothing was outstanding, so the pass cost exactly one
        single-row read.
    :param added: pairs that gained a job.
    :param updated: pairs whose job was rebuilt from a changed configuration.
    :param removed: pairs whose job was withdrawn (deleted, disabled, or an endpoint disabled).
    :param failed: pairs whose runner could not be built this pass; retried, with backoff.
    :param unreadable: pairs whose stored rows would not translate at all this pass.
    """

    generation: int
    reloaded: bool
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """Whether this pass altered the job set in any way."""
        return bool(self.added or self.updated or self.removed)


def _fingerprint(
    pair_row: SyncPairRow,
    source_row: EndpointRow,
    target_row: EndpointRow,
    rule_rows: Sequence[SelectionRuleRow],
    override_rows: Sequence[SelectionOverrideRow],
) -> str:
    """A stable digest of every stored value a pair's runner and its job depend on.

    Deliberately computed from the **rows**, not from the objects built out of them: a
    :class:`~qlabs_catalog_sync.selection.rules.SelectionRuleSet` holds compiled matchers
    whose equality is not something to build change detection on, and a digest over plain
    scalars is both cheaper and easier to reason about.

    Both referenced endpoints are included, so disabling an endpoint or editing its
    non-secret settings rebuilds the pairs that use it. ``created_at``/``updated_at`` are
    excluded on purpose: they move on every write, including one that changed nothing this
    pair cares about, and a rebuild churns a runner (and possibly a connector) for nothing.
    """
    payload = {
        "pair": {
            "name": pair_row.name,
            "source": pair_row.source,
            "target": pair_row.target,
            "target_space": pair_row.target_space,
            "entity_types": [item.value for item in pair_row.entity_types],
            "cadence_seconds": pair_row.cadence_seconds,
            "jitter_seconds": pair_row.jitter_seconds,
            "manual_edit_policy": pair_row.manual_edit_policy.model_dump(mode="json"),
            "activation_opt_in": pair_row.activation_opt_in,
            "enabled": pair_row.enabled,
        },
        "endpoints": [
            {
                "name": row.name,
                "connector": row.connector,
                "role": row.role.value,
                "secret_ref": row.secret_ref,
                "settings": row.settings,
                "enabled": row.enabled,
            }
            for row in (source_row, target_row)
        ],
        "rules": [
            {
                "id": str(row.id),
                "ordinal": row.ordinal,
                "scope": row.scope.value,
                "decision": row.decision.value,
                "matcher_kind": row.matcher_kind.value,
                "pattern": row.pattern,
            }
            for row in sorted(rule_rows, key=lambda item: (item.scope.value, item.ordinal))
        ],
        "overrides": [
            {
                "scope": row.scope.value,
                "object_id": row.object_id,
                "decision": row.decision.value,
                "reason": row.reason,
            }
            for row in sorted(override_rows, key=lambda item: (item.scope.value, item.object_id))
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ConfigStorePairSource:
    """The :class:`ConfigSource` that reads the console's configuration store (C1).

    One instance wraps one :class:`~qlabs_catalog_sync.configstore.service.ConfigService`,
    which is already the only supported way to read or write that schema. This class adds
    exactly two things on top of it: the translation from rows to a
    :class:`~qlabs_catalog_sync.config.SyncPairConfig` plus a compiled
    :class:`~qlabs_catalog_sync.selection.rules.SelectionRuleSet`, and the decision about
    what is *schedulable*.

    **Disabled means not scheduled.** ``endpoints.enabled`` and ``sync_pairs.enabled`` both
    default to ``False`` (T10.1 models them that way because C6's registration is a
    multi-step flow that ends with an explicit enable), and this class honours all three
    flags: a pair is schedulable only when the pair itself, its source endpoint and its
    target endpoint are each enabled. A pair that fails that test is simply absent from the
    snapshot, which is what makes "pause" a plain ``enabled=False`` write with no separate
    mechanism.

    **Enabled-but-broken is not the same as absent.** A pair an operator has enabled whose
    rows will not translate — an empty entity-type list, a selection rule whose pattern is
    malformed — comes back as a :class:`PairLoadFailure` rather than being silently dropped,
    so :meth:`SyncScheduler.reconcile` can leave its existing job alone and report the
    problem instead of unscheduling a pair because of a bad edit. Disabled pairs are filtered
    out *before* translation is attempted, so a half-registered pair (C6) never produces a
    failure at all.

    :param service: the configuration service to read through.
    :param restrict_to: optional pair-name allowlist. Exists for the ``serve --pair`` case:
        an operator who deliberately narrowed one process to a subset of pairs must not have
        the rest reappear the moment reconcile runs.
    """

    def __init__(
        self, service: ConfigService, *, restrict_to: Collection[str] | None = None
    ) -> None:
        self._service = service
        self._restrict_to = frozenset(restrict_to) if restrict_to is not None else None

    async def generation(self) -> int:
        """The current ``config_generation`` value — one indexed single-row read."""
        return await self._service.current_generation()

    async def load(self) -> ConfigSnapshot:
        """Read every schedulable pair, its endpoints, its rules and its overrides."""
        endpoints = {row.name: row for row in await self._service.list_endpoints()}
        plans: list[PairPlan] = []
        failures: list[PairLoadFailure] = []
        for pair_row in await self._service.list_sync_pairs():
            if not self._schedulable(pair_row, endpoints):
                continue
            label = pair_row.name or str(pair_row.id)
            try:
                plans.append(await self._plan_for(pair_row, endpoints))
            except Exception as exc:  # noqa: BLE001 - one bad pair must not lose the rest
                failures.append(
                    PairLoadFailure(pair=label, reason=f"{type(exc).__name__}: {exc}")
                )
        return ConfigSnapshot(plans=tuple(plans), failures=tuple(failures))

    def _schedulable(self, pair_row: SyncPairRow, endpoints: dict[str, EndpointRow]) -> bool:
        if self._restrict_to is not None and pair_row.name not in self._restrict_to:
            return False
        if not pair_row.enabled:
            return False
        source = endpoints.get(pair_row.source)
        target = endpoints.get(pair_row.target)
        return source is not None and source.enabled and target is not None and target.enabled

    async def _plan_for(
        self, pair_row: SyncPairRow, endpoints: dict[str, EndpointRow]
    ) -> PairPlan:
        rule_rows: list[SelectionRuleRow] = []
        override_rows: list[SelectionOverrideRow] = []
        for scope in RuleScope:
            rule_rows.extend(await self._service.list_selection_rules(pair_row.id, scope))
            override_rows.extend(
                await self._service.list_selection_overrides(pair_row.id, scope)
            )
        # Compile first: SelectionRuleSet.build is what validates every pattern, so a
        # malformed rule fails here rather than surviving into a SyncPairConfig whose
        # projected patterns would then fail a second, less informative validator.
        rules = SelectionRuleSet.from_rows(rule_rows, override_rows)
        pair = sync_pair_config_for_row(pair_row, rule_rows)
        return PairPlan(
            pair=pair,
            selection_rules=rules,
            fingerprint=_fingerprint(
                pair_row,
                endpoints[pair_row.source],
                endpoints[pair_row.target],
                rule_rows,
                override_rows,
            ),
            jitter_seconds=pair_row.jitter_seconds,
        )


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
    :param config_source: Where to reconcile the job set against, or ``None`` (the default)
        for a scheduler whose pairs are fixed for the life of the process. Must be supplied
        together with ``runner_factory``. See :meth:`reconcile` and the module docstring's
        "Reconcile" section.
    :param runner_factory: How to build a runner for a pair the store declares. Required
        with, and only with, ``config_source``.
    :param reconcile_interval_seconds: How often to probe the configuration generation;
        see :data:`DEFAULT_RECONCILE_INTERVAL_SECONDS`.
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
        config_source: ConfigSource | None = None,
        runner_factory: RunnerFactory | None = None,
        reconcile_interval_seconds: float = DEFAULT_RECONCILE_INTERVAL_SECONDS,
    ) -> None:
        if (config_source is None) != (runner_factory is None):
            raise ValueError(
                "config_source and runner_factory must be supplied together: reconcile needs "
                "somewhere to read the desired pairs from *and* a way to build a runner for "
                "one it has never seen"
            )
        # An empty runner set is only meaningful when something else can supply pairs later.
        # Without reconcile it is still what it always was: a scheduler with nothing to do.
        if not runners and config_source is None:
            raise ValueError("SyncScheduler needs at least one pair runner")
        names = [runner.pair.name for runner in runners]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate pair name(s) across runners: {duplicates!r}")
        if RECONCILE_JOB_ID in names:
            raise ValueError(f"{RECONCILE_JOB_ID!r} is a reserved job id and cannot be a pair name")
        if degraded_after < 1:
            raise ValueError(f"degraded_after must be at least 1, got {degraded_after}")
        if reconcile_interval_seconds <= 0:
            raise ValueError(
                f"reconcile_interval_seconds must be positive, got {reconcile_interval_seconds}"
            )

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
        self._stopping = False

        # -- reconcile bookkeeping ------------------------------------------------------
        self._source = config_source
        self._runner_factory = runner_factory
        self._reconcile_interval = reconcile_interval_seconds
        #: The runner currently registered for each pair that has a job.
        self._runners_by_pair: dict[str, PairRunner] = {
            runner.pair.name: runner for runner in runners
        }
        #: The plan fingerprint each scheduled job was built from. Pairs constructed from
        #: ``runners`` deliberately have no entry: the store is authoritative once reconcile
        #: is on, so the first pass adopts them from it rather than assuming the process
        #: arguments still match what an operator has since edited in the console.
        self._fingerprints: dict[str, str] = {}
        #: The last successfully loaded desired state, kept so a retry pass costs no read.
        self._desired: dict[str, PairPlan] = {}
        #: Pairs the last load refused to translate, by reason. Their jobs are left alone.
        self._unreadable: dict[str, str] = {}
        #: In-flight fire count per pair (0 or 1, given ``max_instances=1``).
        self._running: dict[str, int] = {}
        #: Pairs whose job was withdrawn while a cycle was still running.
        self._retired: set[str] = set()
        #: Consecutive failed runner builds per pair, and the tick each may next be retried.
        self._build_failures: dict[str, int] = {}
        self._retry_at: dict[str, int] = {}
        self._generation: int | None = None
        self._ticks = 0

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
        """Every pair this scheduler currently holds state for, in registration order.

        With reconcile on, this is the live set, not the constructor's argument: a pair
        added through the console appears here without a restart, and one removed disappears
        — though a pair withdrawn *while a cycle was in flight* stays until that cycle
        finishes, because it is genuinely still running (see :meth:`reconcile`).
        """
        return tuple(self._states)

    @property
    def scheduled_pairs(self) -> tuple[str, ...]:
        """Exactly the pairs that currently have a job — the set a fire can come from.

        Narrower than :attr:`pairs`, which also counts a withdrawn pair still finishing its
        last cycle.
        """
        return tuple(self._runners_by_pair)

    @property
    def generation(self) -> int | None:
        """The configuration generation the last reconcile observed, or ``None``."""
        return self._generation

    @property
    def reconcile_interval_seconds(self) -> float:
        """How often the configuration generation is probed."""
        return self._reconcile_interval

    def runner_for(self, pair_name: str) -> PairRunner | None:
        """The runner a *future* fire of ``pair_name`` would use, or ``None``.

        Deliberately not "the runner the cycle in flight is using": a reconcile that lands
        mid-cycle replaces this while the running cycle keeps the object it started with.
        """
        return self._runners_by_pair.get(pair_name)

    def is_running(self, pair_name: str) -> bool:
        """Whether a cycle for ``pair_name`` is in flight right now."""
        return self._running.get(pair_name, 0) > 0

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
        if self._source is not None:
            self._add_reconcile_job()
        self._scheduler.start()
        self._started = True
        _LOG.info(
            "scheduler.started",
            pairs=list(self.pairs),
            reconcile_interval=self._reconcile_interval if self._source is not None else None,
        )

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
        # Set before pausing: a reconcile already dispatched keeps running for a moment, and
        # this is what stops it installing a job into a scheduler that is going down.
        self._stopping = True
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

    def _add_job(self, runner: PairRunner, *, jitter_override: float | None = None) -> None:
        pair = runner.pair
        jitter = self.jitter_for(pair) if jitter_override is None else max(0.0, jitter_override)
        trigger = IntervalTrigger(
            seconds=pair.cadence_seconds,
            jitter=jitter if jitter > 0 else None,
            timezone=UTC,
        )
        job_kwargs: dict[str, object] = {
            "trigger": trigger,
            # ``args`` is what makes C1's in-flight guarantee mechanical rather than
            # aspirational: apscheduler binds a job's args at *dispatch* time (``run_
            # coroutine_job`` calls ``job.func(*job.args)`` on the job object it captured),
            # so a fire already running holds this exact runner even after reconcile has
            # replaced the job with one carrying a differently-configured runner.
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

    def _add_reconcile_job(self) -> None:
        """Register reconcile as an ordinary job, not a task of its own.

        Three things come free that a hand-rolled ``asyncio`` task would have to re-earn, and
        one of them is a correctness property rather than a convenience:

        * :meth:`shutdown` pauses the scheduler before it waits, and a paused scheduler
          processes no fires at all — so pausing already stops reconcile from installing new
          jobs into a scheduler that is going down. A separate task would need its own
          cancellation, ordered against that same pause.
        * ``max_instances=1`` is the same guarantee this module already relies on for pairs:
          a reconcile that runs long (a slow database, a factory reaching a connector) can
          never have a second one start on top of it.
        * It is visible in ``scheduler.get_jobs()`` like everything else this class registers,
          so "what is this process actually doing" has one answer, not two.

        No jitter, deliberately: the probe is a single indexed row read, and a predictable
        interval is what lets "a saved change takes effect within N seconds" be a statement an
        operator can rely on rather than a distribution. The first fire is *now* rather than
        one interval out — the store is authoritative, so converging on it is the first thing
        this process should do, not the second.
        """
        self._scheduler.add_job(
            self._reconcile_tick,
            trigger=IntervalTrigger(seconds=self._reconcile_interval, timezone=UTC),
            id=RECONCILE_JOB_ID,
            name="config-reconcile",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=self._misfire_grace_seconds,
            replace_existing=False,
            next_run_time=datetime.now(tz=UTC),
        )

    def _first_run_time(self, pair: SyncPairConfig, jitter: float) -> datetime | None:
        """``None`` to accept ``IntervalTrigger``'s own default (first fire one cadence out),
        or an explicit, still-jittered near-immediate time when ``run_immediately`` is set."""
        if not self._run_immediately:
            return None
        offset = random.uniform(0, jitter) if jitter > 0 else 0.0
        return datetime.now(tz=UTC) + timedelta(seconds=offset)

    # -- reconcile --------------------------------------------------------------------------

    async def _reconcile_tick(self) -> None:
        """The scheduled entry point. Never raises — a bad reconcile is not a bad scheduler.

        :meth:`reconcile` itself does raise, because a caller that awaits it directly (an API
        route wanting a configuration change to land immediately rather than within one
        interval) wants to know that the store was unreachable. On the timer there is nobody
        to tell, and the right answer is to keep every pair firing and try again next tick.
        """
        try:
            await self.reconcile()
        except Exception:
            _LOG.exception("scheduler.reconcile.failed")

    async def reconcile(self) -> ReconcileResult:
        """Bring the job set in line with the configuration store, without a restart (C1).

        Cheap by default. Every pass starts with one read of the generation counter; if that
        number has not moved *and* every desired pair is already applied, the pass stops
        there, having done exactly one indexed single-row read and touched nothing. Only a
        moved generation causes the full configuration read, and only a pair whose
        fingerprint actually changed causes a runner to be rebuilt — so editing pair A never
        churns pair B's connectors or resets its schedule.

        The generation is read *before* the configuration, never after. A write landing
        between the two then makes the pass see newer data than the number it recorded, so
        the next tick reloads and finds nothing left to do; reading it afterwards would let
        the pass record a generation whose data it never saw, and lose that change until some
        later, unrelated write happened to bump the counter again.

        What a pass may do to one pair, and what it may never do:

        * **Add** — a pair the store declares and this process has no job for. Its first fire
          follows the same ``run_immediately`` rule every other pair's does; there is one
          first-fire policy in this module, not one for startup and another for reconcile.
        * **Update** — a pair whose fingerprint changed. The job is withdrawn and re-added
          around a freshly built runner. A cycle already in flight is **not** touched: it
          holds the runner object apscheduler bound at dispatch, so it finishes under the
          configuration it started with, and the new one decides the *next* fire. Nothing
          here ever mutates a live runner, which is what makes that true by construction
          rather than by care.
        * **Remove** — a pair the store no longer declares, or that (or whose endpoint) has
          been disabled. The job goes immediately, so it can never fire again; a cycle
          already running is left to finish. That is the same trade the shutdown path makes
          and for the same reason: the engine commits in one transaction, so cancelling is
          *safe*, but the writes already sent have spent real Qlik write-tier budget, and
          finishing turns that spend into a committed result instead of throwing it away.
          Deleting a pair in the console is a configuration change, not an emergency stop.
        * **Leave alone** — a pair the store could not translate at all (:class:`PairLoadFailure`).
          It keeps whatever job it has. Unscheduling a healthy, running pair because someone
          saved a malformed rule is a much worse failure than continuing on the last good
          configuration and saying so.
        * **Never** — take another pair down with it. A runner that cannot be built (an
          endpoint whose connector is gone, a secret reference that no longer resolves) is
          logged, marked degraded in the health registry, and retried with backoff; every
          other pair is reconciled in the same pass regardless.

        :raises RuntimeError: if no ``config_source`` was configured.
        """
        if self._source is None or self._runner_factory is None:
            raise RuntimeError(
                "SyncScheduler.reconcile() needs config_source and runner_factory; this "
                "scheduler was built with a fixed pair set"
            )
        self._ticks += 1
        generation = await self._source.generation()
        reloaded = generation != self._generation
        if reloaded:
            snapshot = await self._source.load()
            self._desired = {plan.pair.name: plan for plan in snapshot.plans}
            self._unreadable = {failure.pair: failure.reason for failure in snapshot.failures}
            self._generation = generation
            # An operator changing anything is a reason to retry a broken pair at once,
            # rather than sitting out the rest of its backoff.
            self._retry_at.clear()
            for failure in snapshot.failures:
                self._report_broken(failure.pair, failure.reason, event="scheduler.pair.unreadable")
        elif not self._diverged():
            return ReconcileResult(generation=generation, reloaded=False)
        if self._stopping:
            return ReconcileResult(generation=generation, reloaded=reloaded)
        return await self._apply(generation, reloaded=reloaded, factory=self._runner_factory)

    def _diverged(self) -> bool:
        """Whether anything desired is still not applied — the retry trigger, no reads needed."""
        for name, plan in self._desired.items():
            if self._fingerprints.get(name) != plan.fingerprint:
                return True
        return any(
            name not in self._desired and name not in self._unreadable
            for name in self._runners_by_pair
        )

    async def _apply(
        self, generation: int, *, reloaded: bool, factory: RunnerFactory
    ) -> ReconcileResult:
        """Withdraw, then build, then install — in that order, for a reason.

        Building a runner is the only genuinely awaited step in a pass (a factory may have to
        reach a connector), and every ``await`` is a point where a due fire can be dispatched.
        So every runner this pass needs is built *first*, and the job set is then rewritten in
        one synchronous stretch with no ``await`` in it: a fire that lands during a reconcile
        sees either the old job set or the new one, never a half-written mixture of the two.
        """
        built, failed = await self._build_all(factory)
        removed = self._apply_removals()
        added, updated = self._install_all(built)
        result = ReconcileResult(
            generation=generation,
            reloaded=reloaded,
            added=tuple(added),
            updated=tuple(updated),
            removed=tuple(removed),
            failed=tuple(failed),
            unreadable=tuple(sorted(self._unreadable)),
        )
        if result.changed or result.failed:
            _LOG.info(
                "scheduler.reconciled",
                generation=generation,
                added=list(result.added),
                updated=list(result.updated),
                removed=list(result.removed),
                failed=list(result.failed),
                unreadable=list(result.unreadable),
            )
        return result

    def _apply_removals(self) -> list[str]:
        removed: list[str] = []
        for name in list(self._runners_by_pair):
            if name in self._desired or name in self._unreadable:
                continue
            self._withdraw(name)
            removed.append(name)
        return removed

    async def _build_all(
        self, factory: RunnerFactory
    ) -> tuple[list[tuple[str, PairPlan, PairRunner]], list[str]]:
        """Build a runner for every pair whose applied fingerprint is stale.

        One pair's failure is caught here and nowhere else: a factory that raises produces a
        degraded health entry, a log line and a backoff, and the loop moves straight on to
        the next pair. Nothing about a broken endpoint, a vanished connector or an
        unresolvable secret reference can reach :meth:`reconcile`'s caller or stop another
        pair being reconciled in the same pass.
        """
        built: list[tuple[str, PairPlan, PairRunner]] = []
        failed: list[str] = []
        for name, plan in self._desired.items():
            if self._fingerprints.get(name) == plan.fingerprint:
                continue
            if self._ticks < self._retry_at.get(name, 0):
                failed.append(name)
                continue
            try:
                runner = await factory(plan)
            except Exception as exc:  # noqa: BLE001 - a broken pair is not a broken scheduler
                failed.append(name)
                self._record_build_failure(name, exc)
                continue
            if runner.pair.name != name:
                failed.append(name)
                self._record_build_failure(
                    name,
                    ValueError(
                        f"runner factory returned a runner for pair {runner.pair.name!r} "
                        f"when asked for {name!r}"
                    ),
                )
                continue
            built.append((name, plan, runner))
        return built, failed

    def _install_all(
        self, built: Sequence[tuple[str, PairPlan, PairRunner]]
    ) -> tuple[list[str], list[str]]:
        added: list[str] = []
        updated: list[str] = []
        for name, plan, runner in built:
            existed = name in self._runners_by_pair
            self._install(name, plan, runner)
            (updated if existed else added).append(name)
        return added, updated

    def _record_build_failure(self, name: str, exc: BaseException) -> None:
        """Report a pair whose runner would not build, and back its retries off.

        Retried every tick would mean a connector ``setup()`` attempt every few seconds for as
        long as an endpoint stays broken — an auth-retry storm against somebody's tenant, for
        a condition only an operator can fix. The backoff doubles per consecutive failure up
        to :data:`DEFAULT_RETRY_BACKOFF_TICK_CAP` ticks, and is cleared outright the moment
        the generation moves: an operator who just edited something wants their fix tried now,
        not on the far side of a backoff their edit was meant to end.
        """
        failures = self._build_failures.get(name, 0) + 1
        self._build_failures[name] = failures
        backoff = min(2 ** min(failures - 1, 16), DEFAULT_RETRY_BACKOFF_TICK_CAP)
        self._retry_at[name] = self._ticks + backoff
        self._report_broken(
            name,
            f"{type(exc).__name__}: {exc}",
            event="scheduler.pair.build_failed",
        )

    def _install(self, name: str, plan: PairPlan, runner: PairRunner) -> None:
        """Register (or re-register) ``name``'s job around ``runner``.

        Removing before adding, rather than ``replace_existing=True``, is not cosmetic: it
        keeps exactly one code path for building a job (:meth:`_add_job`) and makes the
        withdrawal of the old trigger explicit at the one place a pair's schedule is allowed
        to change. Neither form disturbs a cycle already dispatched — apscheduler's executor
        counts instances per job *id* on the executor itself, not on the stored job, so the
        replacement job cannot start a second concurrent fire while the first is still
        running either.
        """
        self._drop_job(name)
        self._runners_by_pair[name] = runner
        self._fingerprints[name] = plan.fingerprint
        self._states.setdefault(name, _PairState())
        self._retired.discard(name)
        self._build_failures.pop(name, None)
        self._retry_at.pop(name, None)
        self._add_job(runner, jitter_override=plan.jitter_seconds)

    def _withdraw(self, name: str) -> None:
        """Stop ``name`` firing again, without disturbing a cycle already in flight."""
        self._drop_job(name)
        self._runners_by_pair.pop(name, None)
        self._fingerprints.pop(name, None)
        self._build_failures.pop(name, None)
        self._retry_at.pop(name, None)
        if self.is_running(name):
            # Keep the pair's state until its last cycle closes out: _record_outcome reads
            # it, and a cycle that is still paying for itself deserves to be reported like
            # any other.
            self._retired.add(name)
            _LOG.info(
                "scheduler.pair.withdrawn_mid_cycle",
                pair=name,
                detail=(
                    "job removed so it can never fire again; the cycle already running is "
                    "left to finish and commit rather than cancelled -- its writes have "
                    "already spent write-tier budget"
                ),
            )
            return
        self._forget(name)
        _LOG.info("scheduler.pair.withdrawn", pair=name)

    def _forget(self, name: str) -> None:
        """Drop everything this class remembers about a pair that no longer has a job."""
        self._states.pop(name, None)
        self._retired.discard(name)
        self._running.pop(name, None)
        if self._health is not None:
            # HealthRegistry has no removal surface, and a pair that no longer exists must
            # not hold /healthz at 503 forever. Marking it healthy is the closest available
            # truth: there is nothing degraded about a pair that is not running.
            self._health.mark_healthy(name)

    def _drop_job(self, name: str) -> None:
        with contextlib.suppress(JobLookupError):
            self._scheduler.remove_job(name)

    def _report_broken(self, name: str, reason: str, *, event: str) -> None:
        _LOG.warning(event, pair=name, reason=reason)
        if self._health is not None:
            self._health.mark_degraded(name, reason=reason)

    # -- firing ---------------------------------------------------------------------------

    async def _run_pair(self, runner: PairRunner) -> None:
        """One fire of one pair's job: every configured entity type, in turn.

        Registered as the job function itself, so ``apscheduler``'s own ``max_instances``
        check guards *this whole method* — the only reentrancy protection this class needs;
        see the module docstring for why a second, application-level lock would be redundant.
        """
        name = runner.pair.name
        task = asyncio.current_task()
        if task is not None:
            self._inflight.add(task)
        # Counted, not flagged: max_instances=1 means this never exceeds one today, but a
        # count degrades gracefully if that ever changes, where a boolean would silently
        # clear the flag on the first of two concurrent fires to finish.
        self._running[name] = self._running.get(name, 0) + 1
        try:
            await self._run_pair_body(runner)
        finally:
            remaining = self._running.get(name, 1) - 1
            if remaining > 0:
                self._running[name] = remaining
            else:
                self._running.pop(name, None)
                if name in self._retired:
                    # The pair was removed through the console mid-cycle; that cycle has now
                    # finished and committed, so there is finally nothing left to keep.
                    self._forget(name)
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
        state = self._states.get(pair_name)
        if state is None:
            # The pair was withdrawn between this fire being dispatched and the task starting
            # (reconcile removes the job, and a job removed after submit_job cannot be
            # un-dispatched). Report the cycle; do not resurrect health or failure state for
            # a pair that no longer exists.
            _LOG.info(
                "scheduler.pair.cycle_finished_after_removal",
                pair=pair_name,
                statuses=[report.status.value for report in reports],
            )
            return
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
        if event.job_id == RECONCILE_JOB_ID:
            # The reconcile job shares this listener but is not a pair; logging it as one
            # would put a job id in a `pair=` field and invent a sync pair nobody configured.
            _LOG.warning(
                "scheduler.reconcile.tick_dropped",
                code=event.code,
                detail=(
                    "a configuration reconcile was still running when the next tick came "
                    "due, or missed its grace window; the job set converges on the next tick"
                ),
            )
            return
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
