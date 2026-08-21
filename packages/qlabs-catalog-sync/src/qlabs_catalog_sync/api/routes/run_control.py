"""Dry-run and run-control routes (WP12/T12.6): plan a cycle, run one now, pause, resume.

Nested under one pair (``/pairs/{pair_id}/dry-run``, ``/run-now``, ``/pause``,
``/resume``, ``/run-status``) — every one of these actions is meaningless without a
pair, exactly like ``routes/selection.py``'s rules and overrides.

**The plan is ``SyncRunReport``, not a second shape.** ``sync/loop.py``'s own docstring
is explicit: *"T2.8's dry run serializes exactly this (``SyncRunReport.to_json()``) as
its machine-readable plan, and T8.1's pilot asserts against it."* :func:`dry_run` and
:func:`run_now` below call :meth:`~qlabs_catalog_sync.sync.loop.SyncLoop.run_cycle`
the same way ``cli/sync_commands.py``'s ``dry-run``/``run`` commands do, and
:class:`SyncRunReportOut` is ``report.to_json()`` given a concrete pydantic shape
(:meth:`SyncRunReportOut.of` is ``model_validate(report.to_json())``, nothing more) so
T12.8's generated TypeScript client sees a real schema instead of an opaque blob — the
console never renders something the CLI's own ``dry-run`` did not already print.

**This is also where console-configured selection first reaches the loop.**
``sync/loop.py``'s docstring names the seam directly: *"the configuration store already
holds ordered rules and overrides per pair... wiring the loop to load them is the API
and scheduler work, not this function's [``rule_set_for_pair``'s]."* A pair created
through ``routes/pairs.py`` has no ``catalog_schema_patterns`` at all — C3 moved
selection into the ``selection_rules``/``selection_overrides`` tables — so
:func:`~qlabs_catalog_sync.configstore.runtime.sync_pair_config_for_row` fills
:class:`~qlabs_catalog_sync.config.SyncPairConfig`'s required-non-empty
``catalog_schema_patterns`` with a projection of the pair's own include rules (falling
back to :data:`~qlabs_catalog_sync.configstore.runtime.INERT_CATALOG_SCHEMA_PATTERN`),
and every loop built here is constructed with an explicit ``selection_rules=``, which is
what makes that field never consulted: :class:`~qlabs_catalog_sync.sync.loop.SyncLoop`
only falls back to ``rule_set_for_pair(pair)`` when ``selection_rules`` is omitted.

**Turning stored rows into live objects is ``configstore/runtime.py``'s job, not this
module's.** Connectors are still built fresh per request and closed when it finishes —
that has not changed — but the construction itself
(:func:`~qlabs_catalog_sync.configstore.runtime.build_connector_for_endpoint`), the pair
translation (:func:`~qlabs_catalog_sync.configstore.runtime.sync_pair_config_for_row`)
and the rule-set load (:func:`~qlabs_catalog_sync.configstore.runtime.
selection_rows_for_pair`) now live in one shared module below both ``api/`` and ``cli/``.
This module originally carried private copies of all three, because ``cli/wiring.py``'s
``build_connector_pool``/``build_sync_loop`` are keyed to a static, YAML-loaded
``EngineConfig`` and cannot see a database-backed pair's endpoints, and because
``routes/`` must not import ``cli/`` (the reverse import already exists,
``cli/serve_command.py`` -> ``api/app.py``). The scheduler's reconcile needed the same
three things and could not reach them here either. ``configstore/runtime.py`` is the
answer to that: one implementation, imported by both, with the dependency arrow pointing
one way. All this module keeps is the translation of that module's layer-neutral
:class:`~qlabs_catalog_sync.configstore.runtime.EndpointSetupError` into the same 422
``endpoint_setup_failed`` :class:`~qlabs_catalog_sync.api.errors.APIError` it raised
before — a connector that is unregistered or broken still propagates as
``ConnectorLookupError``'s own subclasses, and a malformed ``secret_ref`` still as
``SecretRefFormatError``, both already mapped by ``api/errors.py``.

Synchronous, with a bounded timeout — not start-and-poll
----------------------------------------------------------

A dry run is real I/O: it lists and reads from the source and (for ``run-now``) writes
to the target, potentially across many pages. Both routes run the whole cycle inline and
answer once it finishes, wrapped in ``asyncio.timeout`` (:data:`DEFAULT_DRY_RUN_TIMEOUT_SECONDS`
/ :data:`DEFAULT_RUN_NOW_TIMEOUT_SECONDS`, both overridable per :func:`build_run_control_router`
call). Start-and-poll (kick off a background task, hand back a job id, let the console
poll it) was the other option and was rejected for this task: it needs a job store the
console can poll against, ownership of what happens to that job if the process restarts
mid-run, and a second status shape — real scope, and not represented anywhere in this
task's ``owns_paths`` (``run_control.py`` plus its own test file only). A future task
that wants it can build it *on top of* this route without changing what it returns on
success: ``run_cycle`` itself does not change shape either way.

The cost of going synchronous is a request that can legitimately take up to the timeout
before answering — acceptable for an operator-triggered action in a console, not for a
public API. **On timeout the client gets an explicit, honest answer**: a 504 with
``code="dry_run_timed_out"`` / ``code="run_now_timed_out"``, not a connection drop and
not a silent partial success. For ``run-now`` specifically: the in-flight guard
(:data:`_running`) is released in the same ``finally`` that closes the connectors, so a
timed-out run-now does not permanently wedge that pair — the *next* run-now attempt is
free to try again — but the underlying cycle itself is not cancelled gracefully; the
timeout tears down the coroutine (and, via ``finally``, closes the connectors) at
whatever ``await`` it was suspended on. A write already sent to Qlik before the timeout
fired is not rolled back — nothing here can roll back a real HTTP call already
accepted by Qlik, same as everywhere else in this engine (see ``sync/loop.py``'s own
"the two writes are not symmetric" discussion) — but nothing commits to the state store
either, so the next cycle (dry-run, run-now, or a real scheduled one, once one exists —
see below) re-reads and re-diffs from where the state store still says it is, and a
replayed write lands on ``WriteOutcome.NO_OP`` exactly as an ordinary crash-between-write
-and-commit would.

Run-now: ``max_instances=1`` per pair, enforced at this router, not the scheduler
------------------------------------------------------------------------------------

**There is no live ``SyncScheduler`` reachable from this router, and no live one is
wired into the API process at all today** — ``api/app.py``'s ``create_app`` takes no
scheduler parameter, and ``cli/serve_command.py`` (the only place a ``SyncScheduler`` is
actually constructed) builds it from ``select_pairs(engine_config, ...)`` — the static,
YAML-loaded ``EngineConfig`` — never from ``ConfigService``. A pair created through
``routes/pairs.py`` and a pair a running scheduler has a job for are, today, simply two
different objects with no shared identity (the former has a UUID; ``SyncPairConfig`` has
none). See the module's "Problems" note in this task's own report for the full finding;
it is not something this file can fix (``scheduler.py`` and ``cli/serve_command.py`` are
both outside this task's ``owns_paths``, and ``scheduler.py`` is being edited in
parallel by T12.9, whose own DoD — "a reconcile during an in-flight cycle... no pair
ever runs twice concurrently" — is exactly the piece that eventually makes a *shared*
concurrency guarantee possible).

Given that, this router's own ``run-now`` **cannot** ask a live scheduler "is this pair
already running" — there is nothing to ask. What it *can* honestly guarantee, and does,
is that **two concurrent ``run-now`` calls for the same pair, through this API, never
run at the same time**: :data:`_running` is a plain ``set[uuid.UUID]`` checked and
inserted with no ``await`` between the check and the insert (safe under asyncio's
single-threaded cooperative scheduling with no lock needed), and a pair already in it
is refused immediately with 409 ``code="sync_cycle_already_running"`` — refused, never
queued, matching the DoD's own wording. This is the same shape as ``AsyncIOScheduler``'s
own ``max_instances=1`` skip (drop the fire, do not queue it, per ``scheduler.py``'s own
module docstring), just enforced by this router over ``run-now`` calls specifically
rather than by ``apscheduler`` over scheduled fires. Once a real ``SyncScheduler`` is
wired to database-backed pairs, closing the gap between "no two run-nows overlap" and
"no run-now overlaps a scheduled fire either" needs one more piece neither this file nor
``scheduler.py`` has today: a way for this router to ask the *scheduler* whether pair X
is currently firing (``SyncScheduler`` tracks in-flight tasks in a private, unkeyed
``_inflight`` set — not queryable per pair even from inside the class's own public
surface) or for the scheduler to route *through* this router's guard. That is a design
decision for whoever adds it, not a workaround this file reaches for uninvited.

Pause / resume: real, persisted, queryable — and honest about what it does not yet do
------------------------------------------------------------------------------------------

``scheduler.py`` exposes no ``pause_pair``/``resume_pair``/``is_paused`` method at all
today (verified by reading the whole file: its ``__all__`` is
``DEFAULT_DEGRADED_AFTER``, ``DEFAULT_JITTER_CAP_SECONDS``, ``DEFAULT_JITTER_FRACTION``,
``DEFAULT_MISFIRE_GRACE_SECONDS``, ``DEFAULT_SHUTDOWN_TIMEOUT_SECONDS``, ``PairRunner``,
``SyncScheduler``, ``jitter_seconds_for`` — nothing about pausing one pair). Its own
``.scheduler`` property (the raw ``AsyncIOScheduler``) is documented "exposed
read/inspect-only" for a caller that wants to know what was registered, explicitly
**not** as a mutation surface this class re-exposes; calling ``pause_job``/``resume_job``
on it directly would reach around that documented boundary and bypass ``SyncScheduler``'s
own health/failure-counter bookkeeping for the pair besides. Per this task's own
instructions, that is exactly the "STOP and report, do not work around it" case, not
something to route around by calling private/undocumented surface.

What pause/resume do instead: toggle the **real**, already-persisted
``SyncPairRow.enabled`` flag through ``ConfigService.update_sync_pair`` — the same field
``routes/pairs.py``'s ``PATCH /pairs/{id}`` already exposes (T12.4: *"the two explicit
opt-ins v1 never flips on by default: activation_opt_in (D7) and enabled itself"*), not
a second, competing on/off concept invented here. This is deliberate, not a shortcut:

* It is the one thing this router can offer that is **not a lie** — ``GET .../run-status``
  reports ``enabled``/``paused`` read straight back from the database, true right now
  and true from any other process that reads the same row (a console reload, a second
  API replica), unlike an in-memory flag that would vanish on restart and disagree
  across replicas.
* It is very plausibly the exact signal T12.9's reconcile logic (running in parallel,
  same "pairs added, removed, **paused**... picked up without a restart" DoD wording)
  will read to decide whether a pair keeps a scheduled job — ``enabled`` is the only
  per-pair boolean the schema has for "should this pair run at all", and inventing a
  second one here (a ``paused`` column, or a local flag) ahead of that task landing
  would create exactly the two-sources-of-truth problem C1 exists to avoid.

**What it honestly does not do yet**: because no live scheduler reads
``ConfigService``-backed pairs at all (see above), flipping ``enabled`` through this
route has **no effect on any live cadence today**. ``run-now`` *does* honor it (refuses
with 409 ``code="sync_pair_paused"`` on a disabled pair — a deliberate choice, not
DoD-mandated: triggering a real write cycle on a pair an operator just turned off reads
as exactly the kind of silent surprise this task's design notes warn against), so pause
is not a complete no-op even before a scheduler exists. But "a paused pair does not fire
on its cadence" is, right now, vacuously true only because there is no cadence anything
fires on for a database-backed pair — not because this route stopped one. This is the
finding this task's instructions asked to be reported plainly rather than faked, and it
is reported plainly: **T12.9 (or whoever wires ``SyncScheduler`` to ``ConfigService``
pairs) needs to consult this same ``enabled`` flag when reconciling; nothing here needs
to change on this file's side when that lands.**

Pause never touches a cycle in flight, by construction — not by a special case
------------------------------------------------------------------------------------

The service's shutdown path (``scheduler.py``) deliberately lets a running cycle finish
rather than abandoning API budget already spent. Pause here behaves the same way, and
trivially so: pausing is a plain ``ConfigService`` write, completely disjoint from
whatever coroutine a ``run-now`` call might already be executing in this process —
:meth:`~qlabs_catalog_sync.sync.loop.SyncLoop.run_cycle` never consults ``enabled``
mid-cycle (it is not even passed the row), so a cycle already running when ``/pause`` is
called runs to completion exactly as it would have anyway. Pause only ever prevents a
*new* ``run-now`` from starting afterward (via the 409 above); there is no cancellation
path here to abandon a cycle with, because there is no cancellation path here at all.

Dry runs are not written to run history; run-now cycles are
------------------------------------------------------------------------------------

``runs/recorder.py``'s own docstring leaves this open explicitly: *"T13.6's dry-run
preview screen is free to call ``run_cycle(dry_run=True)`` directly and render the
report without ever touching ``RunRecorder``, the same way ``cli/sync_commands.py``'s
``dry-run`` command does today... a separate call the wiring section below leaves open."*
This module keeps that CLI parity: :func:`dry_run` never touches ``recorder``.
:func:`run_now`, though, wraps each entity type's ``run_cycle`` with
``recorder.start``/``finish``/``fail`` exactly the way ``SyncScheduler._run_pair_body``
does (``scheduler.py``), so a cycle an operator triggers by hand leaves the same
``runs`` row a scheduled one would — an operator looking at run history should not see a
gap during exactly the window they intervened. ``recorder`` is optional
(mirrors ``SyncScheduler.__init__``'s own ``recorder: RunRecorder | None = None``): a
caller of :func:`build_run_control_router` that does not pass one gets no run-history
row from ``run-now`` either, never a crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict

from qlabs_catalog_sync.configstore.models import EndpointRow, SyncPairRow
from qlabs_catalog_sync.configstore.runtime import (
    EndpointSetupError,
    build_connector_for_endpoint,
    selection_rows_for_pair,
    sync_pair_config_for_row,
)
from qlabs_catalog_sync.configstore.service import ConfigService, SyncPairNotFoundError
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.observability import HealthRegistry, get_logger
from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.selection.rules import SelectionRuleSet
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import (
    RecordOutcome,
    RunStatus,
    SkipReason,
    SyncLoop,
    SyncRunReport,
)
from qlabs_catalog_sync_sdk.config import MetricsHandle
from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.models import EntityType

from ..auth import require_session
from ..errors import API_ERROR_RESPONSES, APIError

__all__ = ["build_run_control_router"]

_LOG = get_logger("qlabs.catalog_sync.api.routes.run_control")

#: Ceiling on one dry-run request: build both connectors, run every requested entity
#: type's cycle. Generous -- this is real I/O across potentially many pages -- but
#: bounded, so a browser request cannot hang indefinitely; see the module docstring's
#: "Synchronous, with a bounded timeout" section for what the client sees when it fires.
DEFAULT_DRY_RUN_TIMEOUT_SECONDS: Final[float] = 120.0

#: Same bound, for ``run-now``. A separate constant (even though the value matches today)
#: because a real cycle and a dry-run one may reasonably want different budgets later.
DEFAULT_RUN_NOW_TIMEOUT_SECONDS: Final[float] = 120.0

#: Pair ids with a ``run-now`` cycle currently executing through this router, process-wide
#: (module-level, shared by every app/router instance in this process -- deliberately
#: not per-router-instance state, since a request handler closure would otherwise let
#: two ``create_app()`` calls in the same process each think a pair is free when the
#: other is mid-cycle). See the module docstring's "Run-now" section for exactly what
#: this does and does not guarantee.
_running: set[uuid.UUID] = set()


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------
# Response / request models -- SyncRunReportOut mirrors SyncRunReport.to_json() field for
# field (see the module docstring: this is not a second plan shape, it is that JSON given
# a concrete pydantic type so it reaches the OpenAPI schema).
# --------------------------------------------------------------------------------------


class DroppedFieldOut(BaseModel):
    """One field the target's manifest could not carry (D2/D3)."""

    model_config = ConfigDict(frozen=True)

    field: str
    reason: str
    capability_mode: str | None
    native_path: str | None


class WithheldFieldOut(BaseModel):
    """One field engine policy held back (D7 activation, or an injected manual-edit policy)."""

    model_config = ConfigDict(frozen=True)

    field: str
    reason: str


class RecordReportOut(BaseModel):
    """What the cycle did with one candidate object, and why."""

    model_config = ConfigDict(frozen=True)

    native_key: str
    entity_type: EntityType
    outcome: RecordOutcome
    neutral_id: uuid.UUID | None
    display_name: str | None
    target_native_key: str | None
    reason: SkipReason | None
    detail: str | None
    was_read: bool
    changed_fields: list[str]
    written_fields: list[str]
    dropped: list[DroppedFieldOut]
    withheld: list[WithheldFieldOut]
    target_skipped_fields: list[str]
    holds_watermark: bool


class OrphanReportOut(BaseModel):
    """A source object that vanished (D4: recorded, reported, never deleted)."""

    model_config = ConfigDict(frozen=True)

    neutral_id: uuid.UUID
    entity_type: EntityType
    endpoint: str
    native_key: str | None
    observed_at: datetime


class ErrorReportOut(BaseModel):
    """One error the cycle hit, classified by the SDK exception type that carried it."""

    model_config = ConfigDict(frozen=True)

    kind: str
    message: str
    endpoint: str | None
    native_key: str | None
    operation: str | None
    retryable: bool
    fatal: bool


class WatermarkOut(BaseModel):
    """The cycle's watermark movement for one entity type."""

    model_config = ConfigDict(frozen=True)

    before: str | None
    after: str | None
    advanced: bool
    held_by: list[str]
    has_more: bool
    pages: int


class RunCountsOut(BaseModel):
    """Per-outcome record counts for one cycle."""

    model_config = ConfigDict(frozen=True)

    read: int
    created: int
    written: int
    unchanged: int
    no_op: int
    skipped: int
    orphaned: int
    filtered: int
    failed: int


class SyncRunReportOut(BaseModel):
    """One entity type's cycle report -- ``SyncRunReport.to_json()``, typed.

    Built with :meth:`of`, never by hand: this is ``report.to_json()`` validated into a
    concrete shape, not a redescription of it. See the module docstring.
    """

    model_config = ConfigDict(frozen=True)

    pair: str
    source_endpoint: str
    target_endpoint: str
    entity_type: EntityType
    status: RunStatus
    dry_run: bool
    committed: bool
    create_enabled: bool
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    watermark: WatermarkOut
    counts: RunCountsOut
    records: list[RecordReportOut]
    orphans: list[OrphanReportOut]
    errors: list[ErrorReportOut]
    quarantined_endpoints: list[str]

    @classmethod
    def of(cls, report: SyncRunReport) -> SyncRunReportOut:
        """The one, only, conversion: validate ``report.to_json()`` into this shape."""
        return cls.model_validate(report.to_json())


class RunReportsOut(BaseModel):
    """One or more entity types' cycle reports for one pair -- a dry run's plan, or what
    ``run-now`` actually did."""

    model_config = ConfigDict(frozen=True)

    pair_id: uuid.UUID
    pair_name: str
    generated_at: datetime
    runs: list[SyncRunReportOut]


class DryRunRequest(BaseModel):
    """Narrow a dry run to specific entity types (omitted or empty means every entity
    type this pair is configured to sync), and preview what a create would look like
    (off by default) -- mirrors the CLI's ``dry-run --create-missing``: without it, a
    source object with no target binding previews as a skip
    (``SkipReason.NO_TARGET_BINDING``), not a create."""

    model_config = ConfigDict(extra="forbid")

    entity_types: list[EntityType] | None = None
    create_missing: bool = False


class RunNowRequest(BaseModel):
    """Narrow ``run-now`` to specific entity types, and opt into creating missing target
    objects (off by default -- see ``sync/loop.py``'s own module docstring for why)."""

    model_config = ConfigDict(extra="forbid")

    entity_types: list[EntityType] | None = None
    create_missing: bool = False


class RunControlStatusOut(BaseModel):
    """Whether a pair is paused and/or has a ``run-now`` cycle in flight through this API.

    ``enabled``/``paused`` are ``SyncPairRow.enabled`` read straight back from the
    database (``paused`` is exactly ``not enabled``) -- real, persisted, true from any
    process that reads the same row. ``running`` is this process's own ``run-now``
    in-flight tracking. See the module docstring's "Pause / resume" section for exactly
    what pausing does and does not control today.
    """

    model_config = ConfigDict(frozen=True)

    pair_id: uuid.UUID
    pair_name: str
    enabled: bool
    paused: bool
    running: bool


# --------------------------------------------------------------------------------------
# Connector construction -- delegated to configstore/runtime.py, which owns the one
# implementation of "a stored row becomes something the engine can run" (see the module
# docstring). All that is left here is the layer translation: that module's neutral
# EndpointSetupError becomes this router's 422, unchanged in code, message and entity.
# --------------------------------------------------------------------------------------


async def _build_connector(
    row: EndpointRow,
    registry: ConnectorRegistry,
    *,
    metrics: MetricsHandle | None,
) -> Connector:
    """:func:`~qlabs_catalog_sync.configstore.runtime.build_connector_for_endpoint`, with
    its :class:`~qlabs_catalog_sync.configstore.runtime.EndpointSetupError` translated
    into the 422 this route has always raised for it.

    A connector that is unregistered or broken still propagates as
    ``ConnectorLookupError``'s own subclasses, and a malformed ``secret_ref`` still as
    ``SecretRefFormatError`` -- both already mapped by ``api/errors.py``, and neither is a
    setup failure.
    """
    try:
        return await build_connector_for_endpoint(row, registry, metrics=metrics)
    except EndpointSetupError as exc:
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "endpoint_setup_failed",
            str(exc),
            entity=exc.endpoint,
        ) from exc


def _select_entity_types(
    configured: Sequence[EntityType], requested: Sequence[EntityType] | None
) -> list[EntityType]:
    """``configured``, or its overlap with ``requested`` when the caller narrowed it --
    mirrors ``cli/wiring.py``'s ``select_entity_types`` (not imported: see the module
    docstring for why this router does not reach into ``cli/``)."""
    if not requested:
        return list(configured)
    allowed = set(configured)
    return [entity_type for entity_type in requested if entity_type in allowed]


# --------------------------------------------------------------------------------------
# The router
# --------------------------------------------------------------------------------------


def build_run_control_router(
    *,
    config_service: ConfigService,
    registry: ConnectorRegistry,
    store: StateStore,
    resolver: IdentityResolver,
    metrics: MetricsHandle | None = None,
    health: HealthRegistry | None = None,
    recorder: RunRecorder | None = None,
    dry_run_timeout_seconds: float = DEFAULT_DRY_RUN_TIMEOUT_SECONDS,
    run_now_timeout_seconds: float = DEFAULT_RUN_NOW_TIMEOUT_SECONDS,
    clock: Callable[[], datetime] = _utc_now,
) -> APIRouter:
    """Build the run-control router over already-built dependencies.

    Mirrors ``api.routes.pairs.build_pairs_router``'s shape: a factory taking every
    dependency explicitly, called once from
    :func:`~qlabs_catalog_sync.api.app.create_app`. ``metrics``/``health``/``recorder``
    are optional (mirrors ``SyncLoop``'s and ``SyncScheduler``'s own optional
    parameters); ``config_service``/``registry``/``store``/``resolver`` are not, because
    neither a dry run nor ``run-now`` can do anything at all without them. See this
    task's own report for exactly what :func:`~qlabs_catalog_sync.api.app.create_app`
    needs to grow to supply them in production.
    """
    router = APIRouter(prefix="/pairs", tags=["run-control"])

    async def _load_pair(pair_id: uuid.UUID) -> SyncPairRow:
        row = await config_service.get_sync_pair(pair_id)
        if row is None:
            raise SyncPairNotFoundError(pair_id)
        return row

    async def _build_loop(
        pair_row: SyncPairRow, *, dry_run: bool, create_missing: bool
    ) -> tuple[SyncLoop, Connector, Connector]:
        source_row = await _load_pair_endpoint(pair_row.source)
        target_row = await _load_pair_endpoint(pair_row.target)
        rule_rows, override_rows = await selection_rows_for_pair(config_service, pair_row.id)
        selection_rules = SelectionRuleSet.from_rows(rule_rows, override_rows)
        source = await _build_connector(source_row, registry, metrics=metrics)
        target = await _build_connector(target_row, registry, metrics=metrics)
        loop = SyncLoop(
            pair=sync_pair_config_for_row(pair_row, rule_rows),
            source=source,
            target=target,
            store=store,
            resolver=resolver,
            selection_rules=selection_rules,
            metrics=metrics,
            health=health,
            create_missing=create_missing,
            dry_run=dry_run,
        )
        return loop, source, target

    async def _load_pair_endpoint(name: str) -> EndpointRow:
        row = await config_service.get_endpoint(name)
        if row is None:
            # Should not happen: sync_pairs.source/target are FKs onto endpoints.name
            # with ON DELETE RESTRICT (configstore/models.py), so a pair can never
            # reference a deleted endpoint. Defensive, not a documented behavior.
            raise APIError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "sync_pair_endpoint_missing",
                f"endpoint {name!r} is referenced by a sync pair but no longer configured",
                entity=name,
            )
        return row

    async def _run(
        pair_row: SyncPairRow,
        *,
        entity_types: Sequence[EntityType] | None,
        dry_run: bool,
        create_missing: bool,
        record_history: bool,
        timeout_seconds: float,
        timeout_code: str,
    ) -> list[SyncRunReport]:
        if not pair_row.entity_types:
            raise APIError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "sync_pair_no_entity_types",
                f"sync pair {pair_row.name!r} has no configured entity types",
                entity=pair_row.name,
            )
        selected = _select_entity_types(pair_row.entity_types, entity_types)

        source: Connector | None = None
        target: Connector | None = None
        reports: list[SyncRunReport] = []
        try:
            async with asyncio.timeout(timeout_seconds):
                loop, source, target = await _build_loop(
                    pair_row, dry_run=dry_run, create_missing=create_missing
                )
                for entity_type in selected:
                    reports.append(
                        await _run_one(
                            loop,
                            pair_row=pair_row,
                            entity_type=entity_type,
                            dry_run=dry_run,
                            record_history=record_history,
                        )
                    )
        except TimeoutError:
            raise APIError(
                status.HTTP_504_GATEWAY_TIMEOUT,
                timeout_code,
                f"sync pair {pair_row.name!r}: cycle did not finish within {timeout_seconds:.0f}s",
                entity=pair_row.name,
            ) from None
        finally:
            for connector in (source, target):
                if connector is not None:
                    with contextlib.suppress(Exception):
                        await connector.close()
        return reports

    async def _run_one(
        loop: SyncLoop,
        *,
        pair_row: SyncPairRow,
        entity_type: EntityType,
        dry_run: bool,
        record_history: bool,
    ) -> SyncRunReport:
        if not record_history or recorder is None:
            return await loop.run_cycle(entity_type)
        run_id = await recorder.start(
            pair=pair_row.name,
            source_endpoint=pair_row.source,
            target_endpoint=pair_row.target,
            entity_type=entity_type,
            dry_run=dry_run,
            started_at=clock(),
        )
        try:
            report = await loop.run_cycle(entity_type)
        except Exception as exc:
            await recorder.fail(run_id, message=f"{type(exc).__name__}: {exc}", finished_at=clock())
            raise
        await recorder.finish(run_id, report)
        return report

    def _status_of(row: SyncPairRow) -> RunControlStatusOut:
        return RunControlStatusOut(
            pair_id=row.id,
            pair_name=row.name,
            enabled=row.enabled,
            paused=not row.enabled,
            running=row.id in _running,
        )

    # ------------------------------------------------------------------------------
    # Dry run
    # ------------------------------------------------------------------------------

    @router.post(
        "/{pair_id}/dry-run",
        response_model=RunReportsOut,
        responses=API_ERROR_RESPONSES,
        summary="Plan this pair's next cycle: the writes it would make, zero mutations",
    )
    async def dry_run(pair_id: uuid.UUID, payload: DryRunRequest) -> RunReportsOut:
        pair_row = await _load_pair(pair_id)
        reports = await _run(
            pair_row,
            entity_types=payload.entity_types,
            dry_run=True,
            create_missing=payload.create_missing,
            record_history=False,
            timeout_seconds=dry_run_timeout_seconds,
            timeout_code="dry_run_timed_out",
        )
        return RunReportsOut(
            pair_id=pair_row.id,
            pair_name=pair_row.name,
            generated_at=clock(),
            runs=[SyncRunReportOut.of(report) for report in reports],
        )

    # ------------------------------------------------------------------------------
    # Run now
    # ------------------------------------------------------------------------------

    @router.post(
        "/{pair_id}/run-now",
        response_model=RunReportsOut,
        responses=API_ERROR_RESPONSES,
        summary="Run one real cycle now; refused if this pair is paused or already running",
    )
    async def run_now(
        pair_id: uuid.UUID, payload: RunNowRequest, request: Request
    ) -> RunReportsOut:
        require_session(request)
        pair_row = await _load_pair(pair_id)
        if not pair_row.enabled:
            raise APIError(
                status.HTTP_409_CONFLICT,
                "sync_pair_paused",
                f"sync pair {pair_row.name!r} is paused; resume it before running it now",
                entity=pair_row.name,
            )
        if pair_id in _running:
            raise APIError(
                status.HTTP_409_CONFLICT,
                "sync_cycle_already_running",
                f"sync pair {pair_row.name!r} already has a cycle in flight; refusing to "
                "start a second one concurrently",
                entity=pair_row.name,
            )
        _running.add(pair_id)
        try:
            reports = await _run(
                pair_row,
                entity_types=payload.entity_types,
                dry_run=False,
                create_missing=payload.create_missing,
                record_history=True,
                timeout_seconds=run_now_timeout_seconds,
                timeout_code="run_now_timed_out",
            )
        finally:
            _running.discard(pair_id)
        return RunReportsOut(
            pair_id=pair_row.id,
            pair_name=pair_row.name,
            generated_at=clock(),
            runs=[SyncRunReportOut.of(report) for report in reports],
        )

    # ------------------------------------------------------------------------------
    # Pause / resume / status
    # ------------------------------------------------------------------------------

    @router.post(
        "/{pair_id}/pause",
        response_model=RunControlStatusOut,
        responses=API_ERROR_RESPONSES,
        summary="Pause this pair (see run_control.py's module docstring for the scope of this)",
    )
    async def pause(pair_id: uuid.UUID, request: Request) -> RunControlStatusOut:
        session = require_session(request)
        row = await config_service.update_sync_pair(
            pair_id, enabled=False, actor=session.username, now=clock()
        )
        return _status_of(row)

    @router.post(
        "/{pair_id}/resume",
        response_model=RunControlStatusOut,
        responses=API_ERROR_RESPONSES,
        summary="Resume a paused pair",
    )
    async def resume(pair_id: uuid.UUID, request: Request) -> RunControlStatusOut:
        session = require_session(request)
        row = await config_service.update_sync_pair(
            pair_id, enabled=True, actor=session.username, now=clock()
        )
        return _status_of(row)

    @router.get(
        "/{pair_id}/run-status",
        response_model=RunControlStatusOut,
        responses=API_ERROR_RESPONSES,
        summary="Whether this pair is paused and/or has a run-now cycle in flight",
    )
    async def run_status(pair_id: uuid.UUID) -> RunControlStatusOut:
        row = await _load_pair(pair_id)
        return _status_of(row)

    return router
