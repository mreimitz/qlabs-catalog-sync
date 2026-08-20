"""SQLAlchemy 2.0 declarative models for run history.

WP11 / T11.4. Three tables, sharing ``Base``/``Base.metadata`` with the T2.2 state-store
tables and the T10.1 configuration tables rather than defining a third declarative base
(one ``MetaData``, one Alembic history for the whole database — see
``qlabs_catalog_sync.state.models`` and ``qlabs_catalog_sync.configstore.models`` for why):

* ``runs`` — one row per :meth:`~qlabs_catalog_sync.sync.loop.SyncLoop.run_cycle` call,
  i.e. one row per (sync pair, entity type) cycle. A pair with three entity types
  produces three ``runs`` rows per fire, never one merged row — a cycle's counts, its
  watermark, and its status are already scoped to exactly one entity type
  (:class:`~qlabs_catalog_sync.sync.loop.SyncRunReport` is), and merging them here would
  either lose that scoping or force an artificial aggregate nobody asked for.

* ``run_items`` — one row per :class:`~qlabs_catalog_sync.sync.loop.RecordReport` **that
  is an issue an operator would plausibly act on**, not one row per candidate the cycle
  looked at. A cycle over a large metastore can filter thousands of objects out of scope
  (``RecordOutcome.FILTERED`` / ``SkipReason.NOT_SELECTED``); writing a row for every one
  of them would make ``run_items`` the size of the source catalog for no reader. A record
  becomes a row when *any* of the following holds — see
  :func:`qlabs_catalog_sync.runs.recorder.is_reportable`:

  - :attr:`RecordReport.holds_watermark` is true — the record left work outstanding
    (an unresolved binding, a stale target reference, an unresolved write conflict, a
    capability refusal, ...). This is precisely the set of skip reasons that are *not*
    :data:`qlabs_catalog_sync.sync.loop._TERMINAL_SKIP_REASONS`, without importing that
    private constant: the same "did this leave something unresolved" question, answered
    from the field the loop already computed it into.
  - ``outcome`` is ``ORPHANED`` (decision D4) or ``FAILED``.
  - :attr:`RecordReport.target_skipped_fields` is non-empty — the channel decisions D2
    (unresolved dataset members) and D3 (owner emails matching no Qlik user) report
    through, on records whose write otherwise succeeded.

  Everything else — every ``UNCHANGED``, every plain ``CREATED``/``WRITTEN`` with nothing
  withheld, every ``FILTERED`` — is represented only in ``runs``' own per-outcome count
  columns. "Why was this object not synced" stays answerable (query ``run_items``) without
  the table growing with the size of what a pair chose not to sync.

* ``run_item_unresolved_fields`` — one row per neutral field name in a ``run_items`` row's
  ``target_skipped_fields``. A separate, normalized child table rather than a JSON column:
  decisions D2 and D3 are the run's operator-facing *point*, and "show me everything this
  run could not resolve" must be a ``WHERE field = ...`` a database can answer, not a scan
  over a blob nobody can filter on.

* ``run_errors`` — one row per :class:`~qlabs_catalog_sync.sync.loop.ErrorReport` a cycle
  hit, normalized the same way and for the same reason (queryable by ``kind``/``endpoint``
  without unpacking JSON), rather than folded into ``runs`` as a blob.

Orphans are **not** re-persisted here. Decision D4's authoritative record is already
``orphan_log`` (T2.2, ``qlabs_catalog_sync.state.models.OrphanLogRow``) — first/last
missing, resolution. Duplicating those fields into ``run_items`` would create two places
that could disagree about whether an orphan is still open. A ``run_items`` row for an
``ORPHANED`` outcome instead carries only what identifies the corresponding
``orphan_log`` row — ``neutral_id`` and ``endpoint`` (the entity type is the run's own,
via ``run_items.run_id -> runs.entity_type``) — so "this run's orphans" is a join, not a
copy. This is a soft reference, not a foreign key: a dry-run cycle can report an
``OrphanReport`` (decision D4's *reporting* half) without ever calling
``StateStore.unit_of_work`` to persist it (dry runs write nothing, per
:attr:`SyncRunReport.dry_run` — see the recorder module docstring for why dry runs are
still recorded in ``runs``), so a hard ``FOREIGN KEY`` to ``orphan_log`` would reject
exactly the rows a previewed cycle needs to record. ``tests/runs`` proves the two only
ever agree where a committed run's orphans are concerned, and does not assert a formal
constraint the domain itself does not hold.

Every enum-shaped column here (``status``, ``outcome``, ``reason``) is stored as a plain,
length-bounded ``String`` and converted at the recorder boundary
(``qlabs_catalog_sync.runs.recorder``) — the same choice
``qlabs_catalog_sync.state.models`` makes for ``identity_map.entity_type`` and
``orphan_log.entity_type``, and for the same reason: a native DB enum is a migration
hazard on PostgreSQL and does not exist on SQLite at all. :class:`RunRecordStatus` is the
one enum minted here rather than reused from elsewhere, because it has one member
(``RUNNING``) that :class:`~qlabs_catalog_sync.sync.loop.RunStatus` cannot express — see
its docstring for why that member exists.

No column anywhere in this schema can hold a credential value — every column here is
run bookkeeping, a record outcome/reason code, or free-text diagnostic detail already
present in :class:`~qlabs_catalog_sync.sync.loop.SyncRunReport`. See
``tests/runs/test_credentials.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from qlabs_catalog_sync.state.models import Base
from qlabs_catalog_sync.state.types import UTCDateTime

__all__ = [
    "RunErrorRow",
    "RunItemRow",
    "RunItemUnresolvedFieldRow",
    "RunRecordStatus",
    "RunRow",
]


class RunRecordStatus(StrEnum):
    """The stored lifecycle of one ``runs`` row.

    ``OK``, ``PARTIAL``, ``FAILED`` and ``SKIPPED`` are exactly
    :class:`qlabs_catalog_sync.sync.loop.RunStatus`'s four members, by value — a finished
    run's status is always one of those, copied verbatim from
    :attr:`~qlabs_catalog_sync.sync.loop.SyncRunReport.status`, never re-decided here.

    ``RUNNING`` is the one value :class:`RunStatus` has no member for, because it is not a
    verdict the loop ever reaches: it exists only between
    :meth:`~qlabs_catalog_sync.runs.recorder.RunRecorder.start` and
    :meth:`~qlabs_catalog_sync.runs.recorder.RunRecorder.finish`/:meth:`~qlabs_catalog_sync.runs.recorder.RunRecorder.fail`,
    i.e. for exactly as long as a cycle is actually in flight. A row a reader finds still
    ``RUNNING`` long after it started is not a fifth kind of outcome — it is evidence the
    process that started it died before it could record one. See the recorder module
    docstring for what closes that row out.
    """

    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunRow(Base):
    """One ``run_cycle`` call: one (sync pair, entity type) cycle. See module docstring.

    ``finished_at`` is ``NULL`` for exactly as long as ``status`` is ``RunRecordStatus.RUNNING``
    — a reader does not need to interpret ``status`` to notice a run that never closed out,
    a plain ``WHERE finished_at IS NULL`` finds it directly. ``duration_seconds`` is
    ``NULL`` for a run :meth:`~qlabs_catalog_sync.runs.recorder.RunRecorder.fail`\\ ed or
    reaped without ever producing a real :class:`~qlabs_catalog_sync.sync.loop.SyncRunReport`
    — there is no genuine cycle duration to report, and inventing one (e.g. "time until we
    noticed") would be measuring the recorder, not the cycle.

    The nine ``*_count`` columns mirror :meth:`SyncRunReport.to_json`'s ``counts`` dict
    exactly (``read``, ``created``, ``written``, ``unchanged``, ``no_op``, ``skipped``,
    ``orphaned``, ``filtered``, ``failed``) — this is the DoD's "counts reconcile with what
    the loop actually wrote", made a queryable column per outcome rather than a JSON blob.
    ``write_count`` (``created_count + written_count``) is deliberately *not* its own
    column: a value trivially derived from two others that are themselves stored is a
    second place for those two to drift apart from, for no reader's benefit.
    """

    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_pair_entity_type_started_at", "pair", "entity_type", "started_at"),
        Index("ix_runs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    pair: Mapped[str] = mapped_column(String(128), nullable=False)
    source_endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    target_endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False)
    committed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    create_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    watermark_before: Mapped[str | None] = mapped_column(Text)
    watermark_after: Mapped[str | None] = mapped_column(Text)
    watermark_advanced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_more: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    read_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    written_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_op_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orphaned_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filtered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Low-cardinality (at most {source, target}), display-only debug context — never a
    # dimension anything filters by, unlike run_items/run_errors, so it stays a small JSON
    # list rather than earning its own child table. See the module docstring.
    quarantined_endpoints: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class RunItemRow(Base):
    """One reportable :class:`~qlabs_catalog_sync.sync.loop.RecordReport`. See module docstring.

    ``endpoint`` is populated for an ``ORPHANED`` row with the source endpoint the object
    vanished from — the other half (with the run's own ``entity_type``) of the
    ``orphan_log`` composite key this row soft-references. It is not otherwise meaningful
    for a non-orphan outcome and is left ``NULL``.
    """

    __tablename__ = "run_items"
    __table_args__ = (
        Index("ix_run_items_run_id", "run_id"),
        Index("ix_run_items_neutral_id_endpoint", "neutral_id", "endpoint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )

    native_key: Mapped[str] = mapped_column(Text, nullable=False)
    neutral_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    display_name: Mapped[str | None] = mapped_column(Text)
    target_native_key: Mapped[str | None] = mapped_column(Text)

    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text)
    endpoint: Mapped[str | None] = mapped_column(String(128))
    held_watermark: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RunItemUnresolvedFieldRow(Base):
    """One neutral field name from a ``run_items`` row's ``target_skipped_fields`` (D2, D3).

    ``(run_item_id, field)`` is unique: the loop's own ``target_skipped_fields`` tuple
    should never repeat a field name for one record, and the constraint turns "should
    never" into "cannot" at the schema the same way this codebase already prefers
    elsewhere (e.g. ``selection_rules``' ``(pair_id, scope, ordinal)``).
    """

    __tablename__ = "run_item_unresolved_fields"
    __table_args__ = (
        UniqueConstraint(
            "run_item_id", "field", name="uq_run_item_unresolved_fields_run_item_id_field"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("run_items.id", ondelete="CASCADE"), nullable=False
    )
    field: Mapped[str] = mapped_column(String(128), nullable=False)


class RunErrorRow(Base):
    """One :class:`~qlabs_catalog_sync.sync.loop.ErrorReport` a cycle hit. See module docstring."""

    __tablename__ = "run_errors"
    __table_args__ = (Index("ix_run_errors_run_id", "run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )

    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(128))
    native_key: Mapped[str | None] = mapped_column(Text)
    operation: Mapped[str | None] = mapped_column(String(64))
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fatal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
