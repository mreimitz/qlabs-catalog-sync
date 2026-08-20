"""Run history, run-issue and configuration change-log routes (WP12/T12.7).

Three questions an operator asks that nothing before this task could answer over HTTP:
*"what has this pair actually done"* (``GET /runs``, ``GET /runs/{run_id}``), *"what did
a given run leave unresolved"* (``GET /runs/{run_id}/issues``), and *"when did this
schema start syncing"* (``GET /config-changes`` -- the exact phrase the RM-06 decision's
Consequences section names as this route's acceptance criterion). All four are
read-only: nothing here edits or deletes a run, an item, an error or a change-log row.

Every read goes through the same sanctioned surfaces the rest of this API already
prefers -- :class:`~qlabs_catalog_sync.runs.recorder.RunRecorder` for single-run reads
(``get_run``/``list_items``/``list_errors``) and
:class:`~qlabs_catalog_sync.state.store.StateStore` for the orphan lifecycle
(``list_orphans``) -- **except for two places where no sanctioned read path exists yet**,
flagged here rather than papered over:

1. **Paginated, filtered run listing.** ``RunRecorder.list_runs`` takes ``limit`` and
   three equality filters but no cursor -- every call starts from the top of ``runs``, so
   it cannot serve a second page. Its own module docstring anticipates exactly this and
   says so explicitly: *"The console's run-list view (T12.7) is expected to build its own,
   richer query against runs directly -- this is the building block this task proves
   against, not that surface."* :func:`_list_runs_page` is that richer query -- a direct,
   read-only ``select(RunRow)`` with keyset pagination -- built with the recorder module's
   own explicit invitation, not as a workaround of it.

2. **The configuration change log has no read path on** :class:`~qlabs_catalog_sync.
   configstore.service.ConfigService` **at all.** T10.3 built ``config_changes`` as a
   write-only audit trail (``configstore/audit.py``'s ``record_create``/``record_update``/
   ``record_delete``/``record_reorder``) and gave ``ConfigService`` no
   ``list_config_changes`` or equivalent. This is a genuine gap, not a design choice this
   task can defend -- it is reported in this task's own report as a finding for whoever
   next owns ``configstore/service.py``. Lacking that method, and unable to add one here
   (``configstore/service.py`` is outside this task's owned paths),
   :func:`_list_config_changes_page` reads ``ConfigChangeRow`` directly through a private,
   read-only session factory bound to
   :attr:`~qlabs_catalog_sync.configstore.service.ConfigService.engine` -- the same
   *public* property ``routes/endpoints.py``'s healthcheck already reads state off of,
   and the same one-engine-one-sessionmaker-one-``asyncio.to_thread`` shape
   ``RunRecorder``/``ConfigService``/``StateStore`` all already use internally. This is
   the narrowest available substitute for a service method that should exist: a
   *read-only*, *select-only* query, kept in one place, over a table this module does not
   own the schema of. It is not a general licence for a route to reach around a service
   layer -- see the module docstrings on ``routes/endpoints.py``/``routes/pairs.py``/
   ``routes/selection.py`` for the alternative (call the service, let its typed errors
   propagate) this module uses everywhere a service method exists.

Both escape hatches are safe *because of*, not despite, decision C1: ``endpoints``,
``sync_pairs``, ``config_changes``, ``runs``, ``run_items``, ``run_errors`` and the T2.2
state-store tables all share **one** ``Base``/``Base.metadata`` and **one** Alembic
history (see each module's own docstring), which is exactly what a deployment's
``ConfigService`` and this route's queries being pointed at the same physical database
guarantees. :class:`~qlabs_catalog_sync.state.store.StateStore` itself accepts a bare
``Engine`` in its constructor for the same reason -- ``StateStore(config_service.engine)``
below is not a hack, it is that constructor used exactly as designed, over the one
database every one of these tables actually lives in.

Raw rows, not synthesized operations, for the change log
----------------------------------------------------------

``ConfigChangeRow`` writes **one row per changed field** for a multi-field update, all
sharing one post-write ``generation`` (``configstore/audit.py``'s own module docstring:
*"Recording every changed field as its own row is also what makes the log actually
answer its stated purpose... A single blob-diff row would still say 'something about
this pair changed on this date' but never let a caller ask about one field in
isolation."*). :class:`ConfigChangeOut` returns that shape **unchanged** -- one row per
field, ``generation`` carried on every row so a client that wants "one edit" groups by
it losslessly. The alternative -- synthesizing one row per ``generation`` server-side,
with a nested list of field changes -- was rejected: the schema draws no line between
"this generation happened to touch one field" and "this generation touched five", so a
synthetic operation shape would have to *invent* that distinction rather than read it,
and every caller who only wants to filter or search by one field (this route's own
``field=`` filter, and the very query the audit module's docstring cites as the point of
this design) would have to unpack the synthetic wrapper to get back to exactly the rows
this endpoint already returns. Grouping stays a client-side ``groupby(generation)`` over
an unmodified export -- always reversible, never a second lossy shape to keep in sync
with the first.

Distinguishing "no issues" from "issues not yet recorded"
-------------------------------------------------------------

``run_items``/``run_errors`` are populated by :meth:`RunRecorder.finish`/
:meth:`RunRecorder.fail`, never by :meth:`RunRecorder.start` -- a run still
``RunRecordStatus.RUNNING`` has written *nothing* to either table yet, not because it has
no issues but because it has not reached the point where issues get recorded at all.
:class:`RunIssuesOut.issues_recorded` is exactly ``status is not RunRecordStatus.RUNNING``
-- a console that renders an in-progress run's issue list as "no issues" would be
reporting a false negative; this field is what lets it render "still running" instead.

Distinguishing a swept-stale run from one that genuinely failed
---------------------------------------------------------------

:meth:`RunRecorder.reap_stale` closes out a crashed process's abandoned ``RUNNING`` row by
appending exactly one :class:`~qlabs_catalog_sync.runs.models.RunErrorRow` whose message
is the module's own exported :data:`~qlabs_catalog_sync.runs.recorder.STALE_RUN_MESSAGE`
constant -- *"Public so a caller (or a test) can recognize a reaped run without
string-matching a private literal"*, per that constant's own docstring. This is exactly
that caller. :class:`RunIssuesOut.swept_stale` (and :class:`RunDetailOut.swept_stale`) is
``True`` exactly when one of the run's errors carries that message -- never a private
string this module invented -- so a run genuinely failed by ``fail()``/``finish()`` (whose
errors carry the real connector/engine failure) and a run reaped after its process died
are distinguishable by an operator without inspecting raw error text.

No credential, ever
--------------------

Nothing in ``runs``/``run_items``/``run_errors`` can hold one -- see ``runs/models.py``'s
own module docstring and ``tests/runs/test_credentials.py``. ``config_changes`` can carry
an ``EndpointRow`` snapshot including ``secret_ref``, but that column is a *reference*
(``"env:QLIK_ACME"``), never a resolved value (C2, ``configstore/models.py``) -- the same
guarantee ``routes/endpoints.py``'s ``EndpointOut`` already relies on. This module adds no
field capable of holding a secret value, and ``tests/api/test_history.py`` proves no
configured credential's literal value ever reaches any response this module produces.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import uuid
from datetime import datetime
from typing import Final, cast

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from qlabs_catalog_sync.configstore.models import ConfigChangeRow
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.configstore.types import ChangeAction, ChangeEntityKind
from qlabs_catalog_sync.runs import (
    RunErrorRecord,
    RunItemRecord,
    RunRecord,
    RunRecorder,
    RunRecordStatus,
    RunRow,
)
from qlabs_catalog_sync.runs.recorder import STALE_RUN_MESSAGE
from qlabs_catalog_sync.state.store import OrphanRecord, StateStore
from qlabs_catalog_sync.sync.loop import RecordOutcome
from qlabs_catalog_sync_sdk.models import EntityType

from ..errors import API_ERROR_RESPONSES, APIError

__all__ = ["build_history_router"]

#: Default/maximum page size for every paginated route in this module. Unbounded run
#: history is exactly the failure mode the task description warns about ("a run list
#: route with no pagination is a route that eventually times out") -- 200 is generous for
#: an operator scrolling a console table and small enough that one page is never itself
#: the slow query.
DEFAULT_LIMIT: Final[int] = 50
MAX_LIMIT: Final[int] = 200

#: The neutral field names decisions D2 and D3 report through (``sync/loop.py``'s
#: ``RecordReport.target_skipped_fields`` docstring; ``DataProduct``/``Dataset`` in
#: ``qlabs_catalog_sync_sdk.models``). Used to bucket a run's issues the way an operator
#: actually asks about them, rather than as one undifferentiated list.
_DATASET_MEMBERS_FIELD: Final[str] = "dataset_refs"
_OWNERS_FIELD: Final[str] = "owners"

#: Module-level ``Query(...)`` singletons rather than inline defaults (ruff B008) --
#: mirrors ``routes/selection.py``'s own ``_SCOPE_QUERY``.
_LIMIT_QUERY: Final = Query(
    default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description=f"Page size, 1-{MAX_LIMIT}."
)
_CURSOR_QUERY: Final = Query(
    default=None, description="Opaque cursor from a previous page's next_cursor."
)
#: ``alias="status"`` so the query string says ``?status=`` while the Python parameter is
#: named ``run_status`` -- ``status`` is also this module's imported ``fastapi.status``
#: (HTTP status codes), and a same-named parameter would shadow it inside that route.
_STATUS_QUERY: Final = Query(default=None, alias="status")


# --------------------------------------------------------------------------------------
# Cursor encoding: an opaque token over (sort_key, id) -- keyset pagination, not
# OFFSET/LIMIT. OFFSET pagination silently skips or repeats rows when the underlying
# table gains or loses rows between two page requests, which is precisely "does not page
# deterministically under concurrent writes" -- the DoD's own words for this task. Keyset
# pagination has no such gap: page two is defined relative to the last row page one
# actually returned, never relative to a row count that can change underneath it.
# --------------------------------------------------------------------------------------


def _encode_cursor(sort_key: datetime, row_id: uuid.UUID) -> str:
    raw = f"{sort_key.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        sort_key_str, _, id_str = raw.partition("|")
        return datetime.fromisoformat(sort_key_str), uuid.UUID(id_str)
    except (ValueError, binascii.Error) as exc:
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_cursor",
            "cursor is malformed or does not belong to this listing",
            field="cursor",
        ) from exc


# --------------------------------------------------------------------------------------
# Response models -- runs
# --------------------------------------------------------------------------------------


class RunCountsOut(BaseModel):
    """Per-outcome counts for one run, mirroring ``RunRow``'s own count columns exactly
    (``runs/models.py``'s module docstring: "the DoD's 'counts reconcile with what the
    loop actually wrote', made a queryable column per outcome rather than a JSON blob").
    ``write`` is ``created + written``, derived here exactly as :attr:`RunRecord.write_count`
    derives it -- never a second stored value to drift from the two it is computed from.
    """

    model_config = ConfigDict(frozen=True)

    read: int
    created: int
    written: int
    write: int
    unchanged: int
    no_op: int
    skipped: int
    orphaned: int
    filtered: int
    failed: int
    error: int


class RunSummaryOut(BaseModel):
    """One ``runs`` row -- the shape ``GET /runs`` lists and ``GET /runs/{run_id}``
    extends with items and errors. ``in_progress`` is ``status is RunRecordStatus.RUNNING``,
    made explicit rather than left for a client to infer from the string value."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    pair: str
    source_endpoint: str
    target_endpoint: str
    entity_type: EntityType
    status: RunRecordStatus
    in_progress: bool
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
    counts: RunCountsOut
    quarantined_endpoints: list[str]

    @classmethod
    def of(cls, record: RunRecord) -> RunSummaryOut:
        return cls(
            id=record.id,
            pair=record.pair,
            source_endpoint=record.source_endpoint,
            target_endpoint=record.target_endpoint,
            entity_type=record.entity_type,
            status=record.status,
            in_progress=record.status is RunRecordStatus.RUNNING,
            dry_run=record.dry_run,
            committed=record.committed,
            create_enabled=record.create_enabled,
            watermark_before=record.watermark_before,
            watermark_after=record.watermark_after,
            watermark_advanced=record.watermark_advanced,
            has_more=record.has_more,
            pages=record.pages,
            started_at=record.started_at,
            finished_at=record.finished_at,
            duration_seconds=record.duration_seconds,
            counts=RunCountsOut(
                read=record.read_count,
                created=record.created_count,
                written=record.written_count,
                write=record.write_count,
                unchanged=record.unchanged_count,
                no_op=record.no_op_count,
                skipped=record.skipped_count,
                orphaned=record.orphaned_count,
                filtered=record.filtered_count,
                failed=record.failed_count,
                error=record.error_count,
            ),
            quarantined_endpoints=list(record.quarantined_endpoints),
        )


class RunListPage(BaseModel):
    """The explicit pagination contract every listing route in this module shares:
    newest-first (``started_at`` descending, ``id`` descending as a total-order
    tie-breaker -- see the cursor section above), a ``limit`` echoing what was actually
    applied, ``has_more``, and a ``next_cursor`` that is ``None`` exactly when there is no
    further page. A client never has to guess whether it has reached the end."""

    model_config = ConfigDict(frozen=True)

    items: list[RunSummaryOut]
    limit: int
    has_more: bool
    next_cursor: str | None


class RunItemOut(BaseModel):
    """One ``run_items`` row -- a record an operator would plausibly ask about, never
    every candidate the cycle looked at (``runs/models.py``'s module docstring)."""

    model_config = ConfigDict(frozen=True)

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
    unresolved_fields: list[str]

    @classmethod
    def of(cls, record: RunItemRecord) -> RunItemOut:
        return cls(
            id=record.id,
            run_id=record.run_id,
            native_key=record.native_key,
            neutral_id=record.neutral_id,
            display_name=record.display_name,
            target_native_key=record.target_native_key,
            outcome=record.outcome,
            reason=record.reason,
            detail=record.detail,
            endpoint=record.endpoint,
            held_watermark=record.held_watermark,
            unresolved_fields=list(record.unresolved_fields),
        )


class RunErrorOut(BaseModel):
    """One ``run_errors`` row. ``is_stale_sweep`` is ``True`` exactly when this error is
    :meth:`RunRecorder.reap_stale`'s own marker -- see the module docstring."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    run_id: uuid.UUID
    kind: str
    message: str
    endpoint: str | None
    native_key: str | None
    operation: str | None
    retryable: bool
    fatal: bool
    is_stale_sweep: bool

    @classmethod
    def of(cls, record: RunErrorRecord) -> RunErrorOut:
        return cls(
            id=record.id,
            run_id=record.run_id,
            kind=record.kind,
            message=record.message,
            endpoint=record.endpoint,
            native_key=record.native_key,
            operation=record.operation,
            retryable=record.retryable,
            fatal=record.fatal,
            is_stale_sweep=record.message == STALE_RUN_MESSAGE,
        )


class RunDetailOut(RunSummaryOut):
    """``GET /runs/{run_id}``: counts (inherited from :class:`RunSummaryOut`), every
    reportable item, and every error -- the DoD's "run detail includes counts, items and
    errors", literally."""

    items: list[RunItemOut]
    errors: list[RunErrorOut]
    swept_stale: bool

    @classmethod
    def of_detail(
        cls,
        record: RunRecord,
        *,
        items: list[RunItemRecord],
        errors: list[RunErrorRecord],
    ) -> RunDetailOut:
        summary = RunSummaryOut.of(record)
        return cls(
            **summary.model_dump(),
            items=[RunItemOut.of(item) for item in items],
            errors=[RunErrorOut.of(error) for error in errors],
            swept_stale=any(error.message == STALE_RUN_MESSAGE for error in errors),
        )


# --------------------------------------------------------------------------------------
# Response models -- run issues
# --------------------------------------------------------------------------------------


class RunOrphanIssueOut(BaseModel):
    """One D4 orphan this run reported, enriched from ``orphan_log`` -- the authoritative
    record (``runs/models.py``'s module docstring: "'this run's orphans' is a join, not a
    copy"). ``orphan_log_found`` is ``False`` only if the join could not find a matching
    ``orphan_log`` row (should not happen for a committed run; a dry run that reported an
    orphan without ever calling ``StateStore.unit_of_work`` is the one documented case it
    can), so this never *invents* lifecycle data the two sources could disagree about --
    an unmatched row surfaces its absence honestly instead of a fabricated guess.
    """

    model_config = ConfigDict(frozen=True)

    run_item_id: uuid.UUID
    neutral_id: uuid.UUID | None
    endpoint: str | None
    native_key: str | None
    display_name: str | None
    detail: str | None
    orphan_log_found: bool
    first_missing_at: datetime | None
    last_missing_at: datetime | None
    last_seen_at: datetime | None
    resolved_at: datetime | None
    still_open: bool

    @classmethod
    def of(cls, item: RunItemRecord, orphan: OrphanRecord | None) -> RunOrphanIssueOut:
        return cls(
            run_item_id=item.id,
            neutral_id=item.neutral_id,
            endpoint=item.endpoint,
            native_key=item.native_key,
            display_name=item.display_name,
            detail=item.detail,
            orphan_log_found=orphan is not None,
            first_missing_at=None if orphan is None else orphan.first_missing_at,
            last_missing_at=None if orphan is None else orphan.last_missing_at,
            last_seen_at=None if orphan is None else orphan.last_seen_at,
            resolved_at=None if orphan is None else orphan.resolved_at,
            still_open=orphan is not None and orphan.resolved_at is None,
        )


class RunIssuesOut(BaseModel):
    """The operator's actual question -- "what went wrong, and what could this run not
    resolve" -- answered as one response, not four separate lists a console would have to
    reassemble. See the module docstring for ``issues_recorded``/``swept_stale``.

    Categories are not mutually exclusive by construction: one write can report both an
    unresolved owner and an unresolved dataset member at once. ``other_outstanding`` is
    every remaining reportable item -- a failed record, or one holding the watermark for a
    reason that is neither D2 nor D3 (e.g. an unconfirmed identity binding) -- so every
    ``run_items`` row for this run appears in exactly the categories it actually belongs
    to, and no reportable item is silently dropped from the answer.
    """

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    status: RunRecordStatus
    in_progress: bool
    issues_recorded: bool
    swept_stale: bool
    has_issues: bool
    unresolved_dataset_members: list[RunItemOut]
    unresolvable_owners: list[RunItemOut]
    orphans: list[RunOrphanIssueOut]
    other_outstanding: list[RunItemOut]
    errors: list[RunErrorOut]


# --------------------------------------------------------------------------------------
# Response models -- configuration change log
# --------------------------------------------------------------------------------------


class ConfigChangeOut(BaseModel):
    """One raw ``config_changes`` row -- one changed field for an update, or the whole
    entity for a create/delete. See the module docstring for why this is never grouped
    into a synthetic "operation" server-side."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    entity_kind: ChangeEntityKind
    entity_id: str
    action: ChangeAction
    field: str | None
    old_value: JsonValue | None
    new_value: JsonValue | None
    actor: str
    changed_at: datetime
    generation: int

    @classmethod
    def of(cls, row: ConfigChangeRow) -> ConfigChangeOut:
        return cls(
            id=row.id,
            entity_kind=row.entity_kind,
            entity_id=row.entity_id,
            action=row.action,
            field=row.field,
            old_value=cast("JsonValue | None", row.old_value),
            new_value=cast("JsonValue | None", row.new_value),
            actor=row.actor,
            changed_at=row.changed_at,
            generation=row.generation,
        )


class ConfigChangeListPage(BaseModel):
    """Same explicit pagination contract as :class:`RunListPage`: newest-first
    (``changed_at`` descending, ``id`` descending as the tie-breaker -- a multi-field
    update's rows share one ``changed_at`` exactly, by construction, so the tie-breaker is
    not a corner case here, it is the common case for every update)."""

    model_config = ConfigDict(frozen=True)

    items: list[ConfigChangeOut]
    limit: int
    has_more: bool
    next_cursor: str | None


# --------------------------------------------------------------------------------------
# The router
# --------------------------------------------------------------------------------------


def build_history_router(recorder: RunRecorder, config_service: ConfigService) -> APIRouter:
    """Build the run-history, run-issues and config-change-log router.

    Mirrors ``api.routes.pairs.build_pairs_router``'s shape: a factory taking its
    dependencies explicitly, meant to be called once by ``api.app.create_app``. Not
    wired into ``create_app`` by this task (three WP12 route tasks ran in parallel and
    would have collided on that file) -- see this task's own report for the exact lines
    to add there.
    """
    # A private, read-only sessionmaker over config_service's own engine -- used only for
    # the two queries no sanctioned read path yet covers (see the module docstring).
    # Never used for a write: every ``session_factory()`` call site in this module opens a
    # plain (non-``begin()``) session and reads through it.
    session_factory: sessionmaker[Session] = sessionmaker(
        bind=config_service.engine, expire_on_commit=False, future=True
    )
    # StateStore accepts a bare Engine by design (its own docstring: "e.g. for Alembic or
    # diagnostics") -- state, configstore and runs share one Base.metadata / one physical
    # database (C1), so this is the same database every other read in this router (and in
    # ``recorder``) already runs against, wrapped in the sanctioned read API rather than a
    # raw query.
    state_store = StateStore(config_service.engine)

    router = APIRouter(tags=["history"])

    # ==== Runs ============================================================================

    async def _list_runs_page(
        *,
        pair: str | None,
        entity_type: EntityType | None,
        run_status: RunRecordStatus | None,
        limit: int,
        cursor: tuple[datetime, uuid.UUID] | None,
    ) -> tuple[list[RunRow], bool]:
        def _read() -> tuple[list[RunRow], bool]:
            stmt = select(RunRow).order_by(RunRow.started_at.desc(), RunRow.id.desc())
            if pair is not None:
                stmt = stmt.where(RunRow.pair == pair)
            if entity_type is not None:
                stmt = stmt.where(RunRow.entity_type == entity_type.value)
            if run_status is not None:
                stmt = stmt.where(RunRow.status == run_status.value)
            if cursor is not None:
                cursor_started_at, cursor_id = cursor
                stmt = stmt.where(
                    or_(
                        RunRow.started_at < cursor_started_at,
                        and_(
                            RunRow.started_at == cursor_started_at,
                            RunRow.id < cursor_id,
                        ),
                    )
                )
            stmt = stmt.limit(limit + 1)
            with session_factory() as session:
                rows = list(session.scalars(stmt).all())
            return rows[:limit], len(rows) > limit

        return await asyncio.to_thread(_read)

    @router.get(
        "/runs",
        response_model=RunListPage,
        responses=API_ERROR_RESPONSES,
        summary="List run history, newest first, paginated",
    )
    async def list_runs(
        pair: str | None = None,
        entity_type: EntityType | None = None,
        run_status: RunRecordStatus | None = _STATUS_QUERY,
        limit: int = _LIMIT_QUERY,
        cursor: str | None = _CURSOR_QUERY,
    ) -> RunListPage:
        decoded_cursor = None if cursor is None else _decode_cursor(cursor)
        rows, has_more = await _list_runs_page(
            pair=pair,
            entity_type=entity_type,
            run_status=run_status,
            limit=limit,
            cursor=decoded_cursor,
        )
        items = [RunSummaryOut.of(_run_record_from_row(row)) for row in rows]
        next_cursor = (
            _encode_cursor(rows[-1].started_at, rows[-1].id) if has_more and rows else None
        )
        return RunListPage(items=items, limit=limit, has_more=has_more, next_cursor=next_cursor)

    async def _require_run(run_id: uuid.UUID) -> RunRecord:
        record = await recorder.get_run(run_id)
        if record is None:
            raise APIError(
                status.HTTP_404_NOT_FOUND,
                "run_not_found",
                f"no run with id {run_id!s}",
                entity=str(run_id),
            )
        return record

    @router.get(
        "/runs/{run_id}",
        response_model=RunDetailOut,
        responses=API_ERROR_RESPONSES,
        summary="Read one run: counts, items and errors",
    )
    async def get_run(run_id: uuid.UUID) -> RunDetailOut:
        record = await _require_run(run_id)
        items = await recorder.list_items(run_id)
        errors = await recorder.list_errors(run_id)
        return RunDetailOut.of_detail(record, items=items, errors=errors)

    @router.get(
        "/runs/{run_id}/issues",
        response_model=RunIssuesOut,
        responses=API_ERROR_RESPONSES,
        summary=(
            "What this run could not resolve: unresolved dataset members (D2), "
            "unresolvable owners (D3), orphans (D4) and errors, as one answer"
        ),
    )
    async def get_run_issues(run_id: uuid.UUID) -> RunIssuesOut:
        record = await _require_run(run_id)
        items = await recorder.list_items(run_id)
        errors = await recorder.list_errors(run_id)

        dataset_member_items = [
            item for item in items if _DATASET_MEMBERS_FIELD in item.unresolved_fields
        ]
        owner_items = [item for item in items if _OWNERS_FIELD in item.unresolved_fields]
        orphan_items = [item for item in items if item.is_orphan]
        categorized_ids = {
            item.id for item in (*dataset_member_items, *owner_items, *orphan_items)
        }
        other_items = [item for item in items if item.id not in categorized_ids]

        orphan_advisories: list[RunOrphanIssueOut] = []
        if orphan_items:
            orphan_records = await state_store.list_orphans(
                record.source_endpoint, unresolved_only=False
            )
            by_key: dict[tuple[uuid.UUID, str], OrphanRecord] = {
                (orphan.neutral_id, orphan.endpoint): orphan for orphan in orphan_records
            }
            for item in orphan_items:
                key = (
                    (item.neutral_id, item.endpoint)
                    if item.neutral_id is not None and item.endpoint is not None
                    else None
                )
                orphan_advisories.append(
                    RunOrphanIssueOut.of(item, by_key.get(key) if key is not None else None)
                )

        swept_stale = any(error.message == STALE_RUN_MESSAGE for error in errors)
        issues_recorded = record.status is not RunRecordStatus.RUNNING
        has_issues = bool(
            dataset_member_items or owner_items or orphan_items or other_items or errors
        )

        return RunIssuesOut(
            run_id=run_id,
            status=record.status,
            in_progress=record.status is RunRecordStatus.RUNNING,
            issues_recorded=issues_recorded,
            swept_stale=swept_stale,
            has_issues=has_issues,
            unresolved_dataset_members=[RunItemOut.of(item) for item in dataset_member_items],
            unresolvable_owners=[RunItemOut.of(item) for item in owner_items],
            orphans=orphan_advisories,
            other_outstanding=[RunItemOut.of(item) for item in other_items],
            errors=[RunErrorOut.of(error) for error in errors],
        )

    # ==== Configuration change log =========================================================

    async def _list_config_changes_page(
        *,
        entity_kind: ChangeEntityKind | None,
        entity_id: str | None,
        action: ChangeAction | None,
        field: str | None,
        limit: int,
        cursor: tuple[datetime, uuid.UUID] | None,
    ) -> tuple[list[ConfigChangeRow], bool]:
        def _read() -> tuple[list[ConfigChangeRow], bool]:
            stmt = select(ConfigChangeRow).order_by(
                ConfigChangeRow.changed_at.desc(), ConfigChangeRow.id.desc()
            )
            if entity_kind is not None:
                stmt = stmt.where(ConfigChangeRow.entity_kind == entity_kind)
            if entity_id is not None:
                stmt = stmt.where(ConfigChangeRow.entity_id == entity_id)
            if action is not None:
                stmt = stmt.where(ConfigChangeRow.action == action)
            if field is not None:
                stmt = stmt.where(ConfigChangeRow.field == field)
            if cursor is not None:
                cursor_changed_at, cursor_id = cursor
                stmt = stmt.where(
                    or_(
                        ConfigChangeRow.changed_at < cursor_changed_at,
                        and_(
                            ConfigChangeRow.changed_at == cursor_changed_at,
                            ConfigChangeRow.id < cursor_id,
                        ),
                    )
                )
            stmt = stmt.limit(limit + 1)
            with session_factory() as session:
                rows = list(session.scalars(stmt).all())
            return rows[:limit], len(rows) > limit

        return await asyncio.to_thread(_read)

    @router.get(
        "/config-changes",
        response_model=ConfigChangeListPage,
        responses=API_ERROR_RESPONSES,
        summary='The configuration change log -- answers "when did this schema start syncing"',
    )
    async def list_config_changes(
        entity_kind: ChangeEntityKind | None = None,
        entity_id: str | None = None,
        action: ChangeAction | None = None,
        field: str | None = None,
        limit: int = _LIMIT_QUERY,
        cursor: str | None = _CURSOR_QUERY,
    ) -> ConfigChangeListPage:
        decoded_cursor = None if cursor is None else _decode_cursor(cursor)
        rows, has_more = await _list_config_changes_page(
            entity_kind=entity_kind,
            entity_id=entity_id,
            action=action,
            field=field,
            limit=limit,
            cursor=decoded_cursor,
        )
        items = [ConfigChangeOut.of(row) for row in rows]
        next_cursor = (
            _encode_cursor(rows[-1].changed_at, rows[-1].id) if has_more and rows else None
        )
        return ConfigChangeListPage(
            items=items, limit=limit, has_more=has_more, next_cursor=next_cursor
        )

    return router


def _run_record_from_row(row: RunRow) -> RunRecord:
    """``RunRow`` -> :class:`RunRecord`, the same conversion
    ``qlabs_catalog_sync.runs.recorder._run_from_row`` does (private there) -- duplicated
    rather than imported because that helper is not part of the module's public surface,
    and this router's own paginated query (see the module docstring) reads ``RunRow``
    directly rather than through :meth:`RunRecorder.list_runs`."""
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
