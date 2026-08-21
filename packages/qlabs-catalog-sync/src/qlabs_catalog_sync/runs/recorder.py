"""Run history: the recorder a sync cycle's caller drives around ``run_cycle``.

WP11 / T11.4. :class:`RunRecorder` turns a
:class:`~qlabs_catalog_sync.sync.loop.SyncRunReport` — the loop's own, already-complete
description of one cycle — into the queryable rows ``qlabs_catalog_sync.runs.models``
defines. It does not invent a parallel report shape: every field it persists is read
straight off the report the loop already produces (or, for :meth:`RunRecorder.start`,
off exactly the values ``SyncLoop`` exposes as public properties — ``pair``,
``source_endpoint``, ``target_endpoint``, ``dry_run`` — before the cycle has run at all).

Two calls bracket one cycle
----------------------------

.. code-block:: python

    run_id = await recorder.start(
        pair=loop.pair.name,
        source_endpoint=loop.source_endpoint,
        target_endpoint=loop.target_endpoint,
        entity_type=entity_type,
        dry_run=loop.dry_run,
        started_at=started_at,
    )
    try:
        report = await loop.run_cycle(entity_type)
    except Exception as exc:
        await recorder.fail(run_id, message=f"{type(exc).__name__}: {exc}", finished_at=...)
        raise
    else:
        await recorder.finish(run_id, report)

:meth:`start` inserts the row *before* the cycle does any work, at
:class:`~qlabs_catalog_sync.runs.models.RunRecordStatus.RUNNING`. :meth:`finish` closes it
out from a real report. Nothing here calls ``run_cycle`` itself — see the module's
"Wiring" section below for exactly where this belongs and why it is not built into
``SyncLoop`` by this task.

The crashed-cycle case
-----------------------

``SyncLoop.run_cycle`` is documented to never raise for a connector or engine failure —
it returns a report with :attr:`~qlabs_catalog_sync.sync.loop.RunStatus.FAILED` instead,
which :meth:`finish` records exactly like any other status. :meth:`fail` exists for the
narrower case that contract does not cover: an exception that escapes ``run_cycle``
entirely (a bug, or genuinely — see ``sync/loop.py``'s ``_load_watermark``, called before
``run_cycle``'s own try block, so a state-store failure there is not caught by it) reaching
the caller shown above, whose ``except`` still owns a ``run_id`` with no report to close
it out with.

Past that, there is a failure mode no ``try``/``except``/``finally`` in this process can
close out at all: the process itself dying between :meth:`start` and either
:meth:`finish` or :meth:`fail` (a hard kill, an OOM kill, a host failure). No code in
*this* process runs when that happens, by definition — a ``finally`` block only ever
executes if something in the process is still running to execute it. A reader who queries
run history while that has happened sees a row genuinely stuck at
:attr:`~qlabs_catalog_sync.runs.models.RunRecordStatus.RUNNING`, with a ``started_at`` and
no ``finished_at`` — indistinguishable, from inside the row alone, from a cycle that is
still legitimately in flight.

:meth:`reap_stale` is what turns that row into a ``FAILED`` one, but it can only do so
from a *different* process — the next one to start. Call it once, at process startup,
before scheduling or running any new cycle: at that moment nothing this process has
started can legitimately still be ``RUNNING`` (nothing has been scheduled yet), so every
``RUNNING`` row it finds is necessarily left over from a process that no longer exists.
This is a sweep, not a per-row diagnosis — it cannot tell a slow cycle from a crashed one
while both processes are alive, which is exactly why it is meant to run once at startup
rather than on a timer against a live scheduler. See the module's "Wiring" section for
where to call it.

Dry runs are recorded
-----------------------

:attr:`~qlabs_catalog_sync.sync.loop.SyncRunReport.dry_run` is copied straight onto the
``runs`` row and nothing here special-cases it. Three reasons:

1. The task's own DoD says "every cycle produces exactly one run row" — a dry run is a
   real cycle (``run_cycle`` ran the whole plan phase; only the commit was skipped), not a
   different kind of thing.
2. Making the recorder unconditional keeps the decision out of the wiring: whatever calls
   :meth:`start`/:meth:`finish` around a dry-run cycle does not need to know that and skip
   the calls — it always makes them, and a reader who only wants real cycles filters
   ``dry_run = false``, exactly as they already filter on ``status``.
3. It does not corrupt the "what actually happened" story: :attr:`committed` (always
   ``False`` for a dry run, since ``SyncRunReport.committed`` is) is an equally queryable,
   independent column. A console's default run-history view can filter ``committed`` (or
   ``dry_run``) without this module ever having thrown a preview's plan away.

This is a decision about run *history* specifically — T13.6's dry-run preview screen is
free to call ``run_cycle(dry_run=True)`` directly and render the report without ever
touching :class:`RunRecorder`, the same way ``cli/sync_commands.py``'s ``dry-run`` command
does today. Whether *that* path is also worth wiring to the recorder (so an ad hoc preview
also leaves a row) is a separate call the wiring section below leaves open.

Wiring — for the orchestrator, not built here
------------------------------------------------

This task owns ``runs/`` only; ``sync/loop.py`` and ``scheduler.py`` are not touched here.
The natural call site already exists and needs no ``SyncLoop`` changes at all:
``SyncScheduler._run_pair_body`` (``qlabs_catalog_sync/scheduler.py``), in its
``for entity_type in pair.entity_types: report = await runner.run_cycle(entity_type)``
loop. Wrap each iteration with :meth:`start`/:meth:`finish`/:meth:`fail` exactly as shown
above (``pair.source``/``pair.target`` supply the endpoint names; the scheduler's own
``PairRunner`` protocol already forces every runner it drives to expose ``pair`` and
``run_cycle``, so no other change to that protocol is required — though adding a
``dry_run: bool`` property to it, mirroring ``SyncLoop.dry_run``, would let ``start()`` be
called with the runner's real value instead of the scheduler's own assumption that it
never drives a dry-run loop, which is true today only because
``cli/serve_command.py`` always builds its loops with ``dry_run=False``).

Construct one :class:`RunRecorder` from the same :class:`~qlabs_catalog_sync.state.store.StateStore`
the scheduler's loops already share (:meth:`RunRecorder.from_store`) and pass it into
``SyncScheduler.__init__`` as a new, optional constructor parameter (``None`` by default,
so existing callers/tests that build a scheduler without a database keep working
unchanged). Call :meth:`reap_stale` once in ``cli/serve_command.py``'s ``_serve``, right
after the recorder is constructed and before ``scheduler.start()`` — that is the process
startup moment the "Wiring" section above describes.

Beware a real circular import if ``sync/loop.py`` itself is ever changed to import
:class:`RunRecorder`: this module imports
:class:`~qlabs_catalog_sync.sync.loop.SyncRunReport` (and its ``RecordOutcome``/
``SkipReason``/``RunStatus``) from ``sync/loop.py`` at module load time, and those names
are defined only partway through that file — well before the ``SyncLoop`` class, but
after that file's own top-level imports. If ``sync/loop.py`` in turn imported
``RunRecorder`` at its own top level, Python would fail importing either module first.
The scheduler wiring above sidesteps this entirely (``scheduler.py`` already imports
``SyncRunReport``/``RunStatus`` from ``sync/loop.py`` and would additionally import
:class:`RunRecorder` from here — neither of *those* two modules imports the other). If a
future change wires the recorder directly into ``SyncLoop`` instead, import
:class:`RunRecorder` there with a local import inside the method that needs it, not at
module top level.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from qlabs_catalog_sync.runs.models import (
    RunErrorRow,
    RunItemRow,
    RunItemUnresolvedFieldRow,
    RunRecordStatus,
    RunRow,
)
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import (
    RecordOutcome,
    RecordReport,
    SyncRunReport,
)
from qlabs_catalog_sync_sdk.models import EntityType

__all__ = [
    "RunErrorRecord",
    "RunItemRecord",
    "RunRecord",
    "RunRecorder",
    "is_reportable",
]

_T = TypeVar("_T")

#: Default message stamped on a run :meth:`RunRecorder.reap_stale` closes out. Public so a
#: caller (or a test) can recognize a reaped run without string-matching a private literal.
STALE_RUN_MESSAGE = (
    "run left RUNNING with no finish or fail recorded -- the process that started it "
    "likely crashed or was killed before it could close the row out"
)


def is_reportable(record: RecordReport) -> bool:
    """Whether ``record`` earns a ``run_items`` row. See ``runs.models``'s module docstring.

    True when the record left work outstanding (:attr:`RecordReport.holds_watermark`),
    when it is an orphan (D4) or a failure, or when the target reported fields it could
    not resolve (:attr:`RecordReport.target_skipped_fields` -- decisions D2 and D3).
    Everything else — every clean write, every unchanged record, every object this pair's
    selection rules simply do not select — is represented only in the run's own count
    columns, never as a row here.
    """
    return (
        record.holds_watermark
        or record.outcome in (RecordOutcome.ORPHANED, RecordOutcome.FAILED)
        or bool(record.target_skipped_fields)
    )


# ==========================================================================================
# Read-side records — plain dataclasses over the reflected rows, the same shape
# qlabs_catalog_sync.state.store uses for IdentityBinding/OrphanRecord/WatermarkState.
# ==========================================================================================


@dataclass(frozen=True, slots=True)
class RunRecord:
    """A stored ``runs`` row."""

    id: uuid.UUID
    pair: str
    source_endpoint: str
    target_endpoint: str
    entity_type: EntityType
    status: RunRecordStatus
    dry_run: bool
    committed: bool
    create_enabled: bool
    watermark_before: str | None
    watermark_after: str | None
    watermark_advanced: bool
    has_more: bool
    pages: int
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    read_count: int
    created_count: int
    written_count: int
    unchanged_count: int
    no_op_count: int
    skipped_count: int
    orphaned_count: int
    filtered_count: int
    failed_count: int
    error_count: int
    quarantined_endpoints: tuple[str, ...]

    @property
    def write_count(self) -> int:
        """Records whose write actually changed the target. Derived, not stored — see
        ``runs.models``'s module docstring."""
        return self.created_count + self.written_count


@dataclass(frozen=True, slots=True)
class RunItemRecord:
    """A stored ``run_items`` row, with its ``run_item_unresolved_fields`` children."""

    id: uuid.UUID
    run_id: uuid.UUID
    native_key: str
    neutral_id: uuid.UUID | None
    display_name: str | None
    target_native_key: str | None
    outcome: RecordOutcome
    reason: str | None
    detail: str | None
    endpoint: str | None
    held_watermark: bool
    unresolved_fields: tuple[str, ...]

    @property
    def is_orphan(self) -> bool:
        """True for a D4 orphan -- join ``(neutral_id, endpoint)`` plus the run's own
        ``entity_type`` against ``orphan_log`` for the authoritative record."""
        return self.outcome is RecordOutcome.ORPHANED


@dataclass(frozen=True, slots=True)
class RunErrorRecord:
    """A stored ``run_errors`` row."""

    id: uuid.UUID
    run_id: uuid.UUID
    kind: str
    message: str
    endpoint: str | None
    native_key: str | None
    operation: str | None
    retryable: bool
    fatal: bool


def _run_from_row(row: RunRow) -> RunRecord:
    return RunRecord(
        id=row.id,
        pair=row.pair,
        source_endpoint=row.source_endpoint,
        target_endpoint=row.target_endpoint,
        entity_type=EntityType(row.entity_type),
        status=RunRecordStatus(row.status),
        dry_run=row.dry_run,
        committed=row.committed,
        create_enabled=row.create_enabled,
        watermark_before=row.watermark_before,
        watermark_after=row.watermark_after,
        watermark_advanced=row.watermark_advanced,
        has_more=row.has_more,
        pages=row.pages,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_seconds=row.duration_seconds,
        read_count=row.read_count,
        created_count=row.created_count,
        written_count=row.written_count,
        unchanged_count=row.unchanged_count,
        no_op_count=row.no_op_count,
        skipped_count=row.skipped_count,
        orphaned_count=row.orphaned_count,
        filtered_count=row.filtered_count,
        failed_count=row.failed_count,
        error_count=row.error_count,
        quarantined_endpoints=tuple(row.quarantined_endpoints),
    )


def _item_from_row(row: RunItemRow, unresolved_fields: tuple[str, ...]) -> RunItemRecord:
    return RunItemRecord(
        id=row.id,
        run_id=row.run_id,
        native_key=row.native_key,
        neutral_id=row.neutral_id,
        display_name=row.display_name,
        target_native_key=row.target_native_key,
        outcome=RecordOutcome(row.outcome),
        reason=row.reason,
        detail=row.detail,
        endpoint=row.endpoint,
        held_watermark=row.held_watermark,
        unresolved_fields=unresolved_fields,
    )


def _error_from_row(row: RunErrorRow) -> RunErrorRecord:
    return RunErrorRecord(
        id=row.id,
        run_id=row.run_id,
        kind=row.kind,
        message=row.message,
        endpoint=row.endpoint,
        native_key=row.native_key,
        operation=row.operation,
        retryable=row.retryable,
        fatal=row.fatal,
    )


# ==========================================================================================
# The recorder
# ==========================================================================================


class RunRecorder:
    """Owns the engine and session factory for run history; the entry point onto it.

    Deliberately not built on :class:`~qlabs_catalog_sync.state.store.StateStore`'s own
    :class:`~qlabs_catalog_sync.state.store.UnitOfWork` -- that class is T2.4's sync-cycle
    transaction idiom (bindings, envelopes, the watermark, together), and a run-history
    write is never part of that transaction: it happens *after* a cycle's own commit has
    already succeeded or failed, describing what happened, not deciding it. It follows the
    same construction shape as :class:`StateStore` instead (one ``Engine``, one
    ``sessionmaker``, blocking SQLAlchemy calls run through :func:`asyncio.to_thread`) so
    the two can share one physical database and one Alembic history without sharing a
    transaction boundary that does not belong between them.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory: sessionmaker[Session] = sessionmaker(
            bind=engine, expire_on_commit=False, future=True
        )

    @classmethod
    def from_store(cls, store: StateStore) -> RunRecorder:
        """Build a recorder sharing ``store``'s engine -- one database, one connection pool."""
        return cls(store.engine)

    async def _write(self, fn: Callable[[Session], _T]) -> _T:
        def _run() -> _T:
            with self._session_factory.begin() as session:
                return fn(session)

        return await asyncio.to_thread(_run)

    async def _read(self, fn: Callable[[Session], _T]) -> _T:
        def _run() -> _T:
            with self._session_factory() as session:
                return fn(session)

        return await asyncio.to_thread(_run)

    # -- writes ---------------------------------------------------------------------------

    async def start(
        self,
        *,
        pair: str,
        source_endpoint: str,
        target_endpoint: str,
        entity_type: EntityType,
        dry_run: bool,
        started_at: datetime,
    ) -> uuid.UUID:
        """Insert the ``RUNNING`` placeholder row for one cycle, before it does any work.

        ``create_enabled`` is not a parameter here: ``SyncLoop`` does not expose it as a
        public property the way it does ``dry_run`` (see the module docstring's "Wiring"
        section), so the row starts at its column default (``False``) and is corrected by
        :meth:`finish` from the real report -- a run reaped or failed before ``finish``
        never had a report to know it from anyway.
        """
        run_id = uuid.uuid4()

        def _write(session: Session) -> None:
            session.add(
                RunRow(
                    id=run_id,
                    pair=pair,
                    source_endpoint=source_endpoint,
                    target_endpoint=target_endpoint,
                    entity_type=entity_type.value,
                    status=RunRecordStatus.RUNNING.value,
                    dry_run=dry_run,
                    committed=False,
                    create_enabled=False,
                    watermark_advanced=False,
                    has_more=False,
                    pages=0,
                    started_at=started_at,
                    finished_at=None,
                    duration_seconds=None,
                    read_count=0,
                    created_count=0,
                    written_count=0,
                    unchanged_count=0,
                    no_op_count=0,
                    skipped_count=0,
                    orphaned_count=0,
                    filtered_count=0,
                    failed_count=0,
                    error_count=0,
                    quarantined_endpoints=[],
                )
            )

        await self._write(_write)
        return run_id

    async def finish(self, run_id: uuid.UUID, report: SyncRunReport) -> None:
        """Close ``run_id`` out from a real, completed :class:`SyncRunReport`.

        Every count column is read from the report's own counting methods
        (:meth:`SyncRunReport.count`, :attr:`SyncRunReport.read_count`) rather than
        recomputed from ``report.records`` here, so "counts reconcile with what the loop
        actually wrote" is true by construction: there is exactly one place that counts.

        Raises :class:`LookupError` if ``run_id`` was never :meth:`start`\\ ed, and
        :class:`RuntimeError` if it was already closed out (by :meth:`finish`,
        :meth:`fail`, or :meth:`reap_stale`) -- a run row transitions out of ``RUNNING``
        exactly once, so a second call is a caller bug, not a state this method silently
        overwrites.
        """

        def _write(session: Session) -> None:
            row = session.get(RunRow, run_id)
            if row is None:
                raise LookupError(f"no run row for run_id={run_id!s}")
            if row.status != RunRecordStatus.RUNNING.value:
                raise RuntimeError(
                    f"run {run_id!s} already finished with status {row.status!r}; "
                    "finish() must not be called twice for the same run"
                )

            row.status = report.status.value
            row.committed = report.committed
            row.create_enabled = report.create_enabled
            row.watermark_before = report.watermark_before
            row.watermark_after = report.watermark_after
            row.watermark_advanced = report.watermark_advanced
            row.has_more = report.has_more
            row.pages = report.pages
            row.finished_at = report.finished_at
            row.duration_seconds = report.duration_seconds

            row.read_count = report.read_count
            row.created_count = report.count(RecordOutcome.CREATED)
            row.written_count = report.count(RecordOutcome.WRITTEN)
            row.unchanged_count = report.count(RecordOutcome.UNCHANGED)
            row.no_op_count = report.count(RecordOutcome.NO_OP)
            row.skipped_count = report.count(RecordOutcome.SKIPPED)
            row.orphaned_count = report.count(RecordOutcome.ORPHANED)
            row.filtered_count = report.count(RecordOutcome.FILTERED)
            row.failed_count = report.count(RecordOutcome.FAILED)
            row.error_count = len(report.errors)
            row.quarantined_endpoints = list(report.quarantined_endpoints)

            for record in report.records:
                if not is_reportable(record):
                    continue
                item_id = uuid.uuid4()
                session.add(
                    RunItemRow(
                        id=item_id,
                        run_id=run_id,
                        native_key=record.native_key,
                        neutral_id=record.neutral_id,
                        display_name=record.display_name,
                        target_native_key=record.target_native_key,
                        outcome=record.outcome.value,
                        reason=None if record.reason is None else record.reason.value,
                        detail=record.detail,
                        # OrphanReport.endpoint is always self._source.name in sync/loop.py,
                        # the same value SyncRunReport.source_endpoint carries -- see
                        # runs.models's module docstring for why this is a soft reference,
                        # not a foreign key, into orphan_log.
                        endpoint=(
                            report.source_endpoint
                            if record.outcome is RecordOutcome.ORPHANED
                            else None
                        ),
                        held_watermark=record.holds_watermark,
                    )
                )
                for field_name in record.target_skipped_fields:
                    session.add(RunItemUnresolvedFieldRow(run_item_id=item_id, field=field_name))

            for error in report.errors:
                session.add(
                    RunErrorRow(
                        run_id=run_id,
                        kind=error.kind,
                        message=error.message,
                        endpoint=error.endpoint,
                        native_key=error.native_key,
                        operation=error.operation,
                        retryable=error.retryable,
                        fatal=error.fatal,
                    )
                )

        await self._write(_write)

    async def fail(
        self,
        run_id: uuid.UUID,
        *,
        message: str,
        finished_at: datetime,
        kind: str = "EngineError",
        endpoint: str | None = None,
    ) -> None:
        """Close ``run_id`` out as failed when the cycle raised before producing a report.

        For the case ``SyncRunReport.status is RunStatus.FAILED`` already covers -- a
        cycle that ran to completion and reported its own failure -- call :meth:`finish`
        with that report instead; this method is for an exception that escaped
        ``run_cycle`` entirely (see the module docstring). Same one-transition guarantee
        as :meth:`finish`: raises :class:`LookupError` if ``run_id`` is unknown, and
        :class:`RuntimeError` if it was already closed out.
        """

        def _write(session: Session) -> None:
            row = session.get(RunRow, run_id)
            if row is None:
                raise LookupError(f"no run row for run_id={run_id!s}")
            if row.status != RunRecordStatus.RUNNING.value:
                raise RuntimeError(
                    f"run {run_id!s} already finished with status {row.status!r}; "
                    "fail() must not be called on a run that already finished"
                )
            row.status = RunRecordStatus.FAILED.value
            row.committed = False
            row.finished_at = finished_at
            row.duration_seconds = None
            row.error_count = 1
            session.add(
                RunErrorRow(
                    run_id=run_id,
                    kind=kind,
                    message=message,
                    endpoint=endpoint,
                    native_key=None,
                    operation=None,
                    retryable=False,
                    fatal=True,
                )
            )

        await self._write(_write)

    async def reap_stale(
        self,
        *,
        now: datetime,
        older_than: timedelta | None = None,
        message: str = STALE_RUN_MESSAGE,
    ) -> tuple[uuid.UUID, ...]:
        """Close out every run still ``RUNNING`` as failed. See the module docstring.

        ``older_than`` is ``None`` by default -- a full, unconditional sweep, correct for
        the intended call site (once at process startup, before anything new is
        scheduled: nothing *this* process has started can legitimately be ``RUNNING`` yet,
        so every row this finds is left over from a process that no longer exists). Pass
        ``older_than`` only if calling this from somewhere other than that startup moment,
        where a cycle genuinely in flight from *this* process could otherwise be reaped
        out from under itself.

        Returns the ids of every run closed out this way, for the caller to log.
        """

        def _write(session: Session) -> tuple[uuid.UUID, ...]:
            stmt = select(RunRow).where(RunRow.status == RunRecordStatus.RUNNING.value)
            if older_than is not None:
                stmt = stmt.where(RunRow.started_at < now - older_than)
            rows = session.scalars(stmt).all()

            reaped: list[uuid.UUID] = []
            for row in rows:
                row.status = RunRecordStatus.FAILED.value
                row.committed = False
                row.finished_at = now
                row.duration_seconds = None
                row.error_count += 1
                session.add(
                    RunErrorRow(
                        run_id=row.id,
                        kind="RunReaped",
                        message=message,
                        endpoint=None,
                        native_key=None,
                        operation=None,
                        retryable=False,
                        fatal=True,
                    )
                )
                reaped.append(row.id)
            return tuple(reaped)

        return await self._write(_write)

    # -- reads ----------------------------------------------------------------------------

    async def get_run(self, run_id: uuid.UUID) -> RunRecord | None:
        """One run by id, or ``None``."""

        def _read(session: Session) -> RunRecord | None:
            row = session.get(RunRow, run_id)
            return None if row is None else _run_from_row(row)

        return await self._read(_read)

    async def list_runs(
        self,
        *,
        pair: str | None = None,
        entity_type: EntityType | None = None,
        status: RunRecordStatus | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        """Runs, most recently started first, optionally filtered.

        The console's run-list view (T12.7) is expected to build its own, richer query
        against ``runs`` directly -- this is the building block this task proves against,
        not that surface.
        """

        def _read(session: Session) -> list[RunRecord]:
            stmt = select(RunRow).order_by(RunRow.started_at.desc()).limit(limit)
            if pair is not None:
                stmt = stmt.where(RunRow.pair == pair)
            if entity_type is not None:
                stmt = stmt.where(RunRow.entity_type == entity_type.value)
            if status is not None:
                stmt = stmt.where(RunRow.status == status.value)
            rows = session.scalars(stmt).all()
            return [_run_from_row(row) for row in rows]

        return await self._read(_read)

    async def list_items(self, run_id: uuid.UUID) -> list[RunItemRecord]:
        """Every reportable item for one run -- decisions D2/D3's unresolved references
        (``unresolved_fields`` non-empty), D4's orphans (``outcome == RecordOutcome.ORPHANED``),
        and anything else that left work outstanding (``held_watermark``). See
        ``runs.models``'s module docstring for exactly which records these are.
        """

        def _read(session: Session) -> list[RunItemRecord]:
            item_rows = session.scalars(select(RunItemRow).where(RunItemRow.run_id == run_id)).all()
            item_ids = [row.id for row in item_rows]
            fields_by_item: dict[uuid.UUID, list[str]] = {item_id: [] for item_id in item_ids}
            if item_ids:
                field_rows = session.scalars(
                    select(RunItemUnresolvedFieldRow).where(
                        RunItemUnresolvedFieldRow.run_item_id.in_(item_ids)
                    )
                ).all()
                for field_row in field_rows:
                    fields_by_item[field_row.run_item_id].append(field_row.field)
            return [_item_from_row(row, tuple(fields_by_item[row.id])) for row in item_rows]

        return await self._read(_read)

    async def list_errors(self, run_id: uuid.UUID) -> list[RunErrorRecord]:
        """Every error one run hit, in the order they were recorded."""

        def _read(session: Session) -> list[RunErrorRecord]:
            rows = session.scalars(select(RunErrorRow).where(RunErrorRow.run_id == run_id)).all()
            return [_error_from_row(row) for row in rows]

        return await self._read(_read)
