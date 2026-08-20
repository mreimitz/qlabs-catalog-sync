"""Upstream sync loop — the canonical cycle, committed as one transaction.

WP2 / T2.4. This is the product: everything else in this repository exists so that this
module can read one source catalog, decide the smallest honest set of writes, apply them
to Qlik, and record exactly what it did — repeatably, restartably, and without ever
deleting anything.

One :meth:`SyncLoop.run_cycle` call syncs **one ordered pair for one entity type**
(RS-07 section 2). The steps are RS-07's, in order:

1. ``list_changed`` on the source, from the stored watermark;
2. ``read`` each candidate into neutral envelopes;
3. resolve identity through the IdentityMap;
4. checksum-diff the fresh source envelopes against the last-known **target** envelopes;
5. write the minimal diff to Qlik;
6. persist the envelopes and advance the watermark **in one transaction**.

Nothing composed here is re-implemented: :func:`~qlabs_catalog_sync.diff.compute_field_diff`
owns the diff, :class:`~qlabs_catalog_sync.identity.IdentityResolver` owns identity,
:meth:`~qlabs_catalog_sync.state.store.StateStore.unit_of_work` owns the transaction, and
:mod:`qlabs_catalog_sync.observability` owns the metric names. This module is the
orchestration and the *decisions*, which is where the interesting parts are.

Plan, then commit
-----------------

A cycle runs in two phases. The **plan phase** does every network call — listing, reading,
diffing, writing — and reads the database only through point reads, accumulating what it
intends to persist (the one write it does not defer is described below). The **commit
phase** opens exactly one
:meth:`~qlabs_catalog_sync.state.store.StateStore.unit_of_work` and applies all of it:
bindings, envelopes, orphan records, and the watermark, together.

Doing it this way rather than holding the transaction open across the network calls is
deliberate. It gives the same all-or-nothing guarantee — the block is entered only once the
cycle has finished, so a failure anywhere before it commits nothing at all — while keeping
the state store's write lock held for microseconds instead of for the length of a Qlik
round trip. It is also why this module binds source identities with
:meth:`~qlabs_catalog_sync.state.store.UnitOfWork.bind_identity` rather than calling
:meth:`~qlabs_catalog_sync.identity.IdentityResolver.register_source`: that helper opens
its *own* transaction, which would put identity allocation outside the cycle's boundary and
make "a failed cycle changed nothing" untrue. Everything the resolver offers on the *read*
side is used as-is.

Because the plan phase reads the database but its own writes are still pending, the cycle
also carries small in-flight indexes (:class:`_Mutations`) of the identities it has minted
and the envelopes it has queued. Without them, a source that reports two changes for one
object in a single listing would mint two neutral ids for it, or create it twice.

The two writes are not symmetric, and cannot be. A write that reached Qlik cannot be rolled
back by a database rollback. That is precisely why
:attr:`~qlabs_catalog_sync_sdk.contract.WriteOutcome.NO_OP` exists: after a crash between
"Qlik accepted the PATCH" and "the state store committed", the next cycle re-reads,
re-diffs against the *stale* stored envelope, sends the same write again, and the target
answers ``NO_OP`` — at which point the envelopes are persisted and the two sides converge.
Replay is safe by construction, not by luck.

There is exactly one deliberate exception to the deferral, and it follows from that same
asymmetry: a **create** is committed to the identity map the moment it succeeds
(:meth:`SyncLoop._anchor_created_object`), not at the end of the cycle. An update replayed
against an object the engine can still find is a no-op, but a create whose binding was
rolled back is a new object nobody has a record of — replaying it would put a second copy
in the customer's tenant. Everything else the failed cycle planned is still discarded.

The watermark, and what holds it back
-------------------------------------

The watermark advances only to a position the source itself proposed
(:attr:`~qlabs_catalog_sync_sdk.contract.ListChangedResult.next_watermark`), only on a
committed cycle, and only past pages in which **every** candidate reached a terminal state:
written, created, unchanged, a no-op, orphaned, or deliberately filtered out. A candidate
that was skipped with work still outstanding — no confirmed Qlik counterpart, a stale
binding, a capability refusal, an unresolved conflict — holds the watermark where it is, and
:attr:`SyncRunReport.watermark_held_by` names it. The cost is re-listing those pages next
cycle; the benefit is that nothing is ever silently left behind, and re-reading is free (an
unchanged checksum short-circuits before any write).

``has_more`` is honored the same way: pages are fetched in order from the position the
previous page proposed, while the *committable* position only ever moves past pages that
were fully terminal — so the watermark can never run ahead of unprocessed work.

Decisions this loop enforces
----------------------------

* **D4 — never delete.** ``delete`` is not called anywhere in this module, and there is no
  code path that could. A source object that vanished (reported ``DELETED``, or gone when
  ``read`` is attempted) is recorded through
  :meth:`~qlabs_catalog_sync.state.store.UnitOfWork.record_orphan` and surfaced in the run
  report; one that reappears is resolved.
* **D7 — activation is opt-in.** Unless the pair sets ``activation_opt_in``, the neutral
  ``status`` field is withheld from every data-product write and reported as withheld.
  Activation makes a product discoverable tenant-wide, so it never happens as a side effect
  of syncing. This is applied unconditionally, *before* any injected policy, so a policy
  cannot weaken it.
* **D1 — the pair's selector decides scope.** A candidate whose native key is a Unity
  Catalog path outside the pair's ``catalog.schema`` patterns is filtered out and reported.
* **Identity is never invented.** A confirmed binding at the target is the only licence to
  write to an existing object. An unconfirmed binding is not. A missing one is not: with
  ``create_missing`` off (the default) the record is skipped and reported, and with it on
  the loop creates a *new* target object and binds the neutral id to the native key the
  target itself returned — which matches nothing and can therefore claim nothing. Binding
  an *existing* target object to a source object stays where T7.1 put it: bootstrap
  proposal plus human confirmation. Creating blindly on a tenant that already holds data
  products would duplicate every one of them, which is why the default is off.
* **Upstream only.** The source connector is only ever read from; ``create``/``update`` are
  called on ``target`` and nowhere else.

Typed exceptions, and what each one does
----------------------------------------

Reactions are the ones the SDK documents, not invented ones:

* :class:`~qlabs_catalog_sync_sdk.exceptions.TransientError` — retried with backoff,
  honoring ``retry_after_seconds``; a ``retry_after_seconds`` hint also counts a
  rate-limit. Exhausting the retries fails the cycle (nothing commits) rather than skipping
  the record, because an endpoint that is persistently erroring must block, not be quietly
  stepped over.
* :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError` — the endpoint is quarantined: the
  cycle stops immediately, nothing commits, and
  :attr:`SyncRunReport.quarantined_endpoints` names it so the caller can stop scheduling it
  while the other pairs keep running.
* :class:`~qlabs_catalog_sync_sdk.exceptions.NotFound` — skipped, never retried. From
  ``read`` it means the source object vanished, which is the orphan path (D4); from the
  target it means the stored binding is stale, which is reported and holds the watermark.
* :class:`~qlabs_catalog_sync_sdk.exceptions.ConflictError` — re-read the target, re-diff
  against what it actually holds now, and retry the write once (RS-07 section 2, step 6). A
  second conflict is reported, not fought.
* :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError` — never retried. The plan was
  wrong, so the record is skipped, reported as an error, and holds the watermark until a
  human fixes the manifest or the configuration.

The run report is a value
-------------------------

:class:`SyncRunReport` is the cycle's result, not a side effect of logging: what was read,
skipped, written and created; which fields the target's manifest could not carry and why;
which fields the target reported as not written; which objects orphaned; which errors
happened. T2.8's dry run serializes exactly this (:meth:`SyncRunReport.to_json`) as its
machine-readable plan, and T8.1's pilot asserts against it. ``dry_run=True`` runs the whole
plan phase, applies **nothing** — not to Qlik and not to the state store — and returns the
same shape.

Seams left open on purpose
--------------------------

* **T2.5, the manual-edit policy.** ``write_policy`` takes any object satisfying
  :class:`WritePolicy`. It is consulted once per planned entity write, after the diff and
  before the write, with the plan and the stored target envelopes in hand, and returns the
  fields to withhold with a reason each. It is ``async`` so an implementation may re-read
  the target to find out whether a value was hand-edited. Source-wins — v1's default — is
  what happens with no policy at all.
* **T2.9, orphans.** The orphan records this loop writes and reports are the raw material;
  the reporting and escalation policy on top of them is that task's.
* **T2.8, the CLI.** ``dry_run`` and :meth:`SyncRunReport.to_json` are its entry points.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Protocol, TypeVar, runtime_checkable

from pydantic import JsonValue

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.diff import DiffPlan, DroppedField, compute_field_diff
from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.observability import (
    METRIC_CYCLE_DURATION_SECONDS,
    METRIC_ERRORS_TOTAL,
    METRIC_RATE_LIMITED_TOTAL,
    METRIC_READS_TOTAL,
    METRIC_SKIPS_TOTAL,
    METRIC_WRITES_APPLIED_TOTAL,
    METRIC_WRITES_PLANNED_TOTAL,
    HealthRegistry,
    bind_sync_context,
    get_logger,
)
from qlabs_catalog_sync.state.store import StateStore, UnitOfWork
from qlabs_catalog_sync_sdk.config import MetricsHandle
from qlabs_catalog_sync_sdk.contract import (
    ChangeRef,
    Connector,
    ListChangedResult,
    Watermark,
    WriteOutcome,
    WriteResult,
)
from qlabs_catalog_sync_sdk.exceptions import (
    AuthError,
    CapabilityError,
    ConflictError,
    ConnectorError,
    NotFound,
    TransientError,
)
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    EntityType,
    FieldDiff,
    FieldEnvelope,
    IdentityRef,
    NeutralEntity,
)

__all__ = [
    "ACTIVATION_FIELD",
    "ACTIVATION_WITHHELD_REASON",
    "ErrorReport",
    "OrphanReport",
    "RecordOutcome",
    "RecordReport",
    "RunStatus",
    "SkipReason",
    "SyncLoop",
    "SyncRunReport",
    "WithheldField",
    "WritePolicy",
    "WriteReview",
]

_LOG = get_logger("qlabs.catalog_sync.sync.loop")

#: The neutral field that maps to Qlik activation (decision D7). Withheld from every write
#: unless the pair opts in, because activating a data product makes it discoverable
#: tenant-wide.
ACTIVATION_FIELD: Final[str] = "status"

#: The reason code recorded on a field withheld by the D7 activation rule.
ACTIVATION_WITHHELD_REASON: Final[str] = "activation_not_opted_in"

_T = TypeVar("_T")


def _utc_now() -> datetime:
    """The default clock: timezone-aware UTC, which the state store requires."""
    return datetime.now(UTC)


async def _default_sleep(seconds: float) -> None:
    """The default backoff sleep. Injectable so tests never actually wait."""
    await asyncio.sleep(seconds)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


# ==========================================================================================
# Run report
# ==========================================================================================


class RecordOutcome(StrEnum):
    """What happened to one candidate object in this cycle."""

    CREATED = "created"
    """A new object was created at the target and bound to the neutral id."""

    WRITTEN = "written"
    """An existing target object was updated."""

    UNCHANGED = "unchanged"
    """Every checksum matched, so the write path was never entered. The idempotent skip."""

    NO_OP = "no_op"
    """A write was sent and the target reported it already matched. This is what a replayed
    cycle looks like when the previous run's writes landed but its transaction did not."""

    SKIPPED = "skipped"
    """Deliberately not written. :attr:`RecordReport.reason` says why."""

    ORPHANED = "orphaned"
    """The object is gone at the source. Recorded in the orphan log; never deleted at the
    target (decision D4)."""

    FILTERED = "filtered"
    """Outside this pair's ``catalog.schema`` selector, so it is not this pair's business."""

    FAILED = "failed"
    """The cycle could not finish this record. A failed record fails the whole cycle."""


class SkipReason(StrEnum):
    """Machine-readable reason a record was skipped. Stable: T2.8 and T8.1 assert on these."""

    NO_TARGET_BINDING = "no_target_binding"
    """No IdentityMap binding at the target. Either identity bootstrap has not been
    confirmed for this object, or creation is not enabled for this run."""

    UNCONFIRMED_TARGET_BINDING = "unconfirmed_target_binding"
    """A binding exists but no human has confirmed it. An unconfirmed proposal is not a
    licence to write (T7.1)."""

    NOTHING_TO_WRITE = "nothing_to_write"
    """Fields differ, but every one of them was withheld by policy (D7 activation, or the
    injected manual-edit policy) or dropped by the target's manifest."""

    TARGET_NOT_FOUND = "target_not_found"
    """The bound target object does not exist any more. The binding is stale; v1 neither
    deletes it nor rebinds it automatically."""

    CAPABILITY_REFUSED = "capability_refused"
    """The target's manifest refused the operation. A planning bug, never retried."""

    CONFLICT_UNRESOLVED = "conflict_unresolved"
    """The target was changed underneath us twice: the re-read/re-diff retry also lost."""

    DELETED_UNKNOWN_OBJECT = "deleted_unknown_object"
    """The source reported a deletion for something never bound here. Nothing was ever
    synced for it, so there is nothing to orphan and nothing to carry onward."""

    NO_SOURCE_ENVELOPES = "no_source_envelopes"
    """The source connector returned an entity with an empty provenance sidecar, so there is
    nothing to diff. A connector bug, surfaced rather than read as "in sync"."""

    NOT_SELECTED = "not_selected"
    """Filtered out by the pair's ``catalog.schema`` patterns (decision D1)."""


#: Skip reasons that leave no work outstanding, and therefore do not hold the watermark
#: back. Everything else does: the object was not synced, and advancing past it would lose
#: it until the source happens to change again.
_TERMINAL_SKIP_REASONS: Final[frozenset[SkipReason]] = frozenset(
    {
        SkipReason.NOT_SELECTED,
        SkipReason.NOTHING_TO_WRITE,
        SkipReason.DELETED_UNKNOWN_OBJECT,
    }
)


class RunStatus(StrEnum):
    """The cycle's overall verdict."""

    OK = "ok"
    """Everything reached a terminal state and the watermark advanced."""

    PARTIAL = "partial"
    """Committed, but work is outstanding, so the watermark was held where it was."""

    FAILED = "failed"
    """Nothing was committed. The watermark is exactly where it was."""

    SKIPPED = "skipped"
    """The cycle did not run: this pair or these endpoints do not sync this entity type."""


@dataclass(frozen=True, slots=True)
class WithheldField:
    """A field the diff wanted to write that engine policy held back.

    Distinct from :class:`~qlabs_catalog_sync.diff.DroppedField`, which is the *target's*
    manifest refusing a field. This is the engine choosing not to send one it could have.
    """

    field: str
    reason: str
    """A stable code — :data:`ACTIVATION_WITHHELD_REASON` for D7, or whatever an injected
    :class:`WritePolicy` returns."""

    def to_json(self) -> dict[str, Any]:
        """This record as plain JSON."""
        return {"field": self.field, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class RecordReport:
    """What the cycle did with one candidate object, and why."""

    native_key: str
    entity_type: EntityType
    outcome: RecordOutcome
    neutral_id: uuid.UUID | None = None
    display_name: str | None = None
    target_native_key: str | None = None
    reason: SkipReason | None = None
    detail: str | None = None
    was_read: bool = False
    """True when the source served a full ``read`` for this candidate."""

    changed_fields: tuple[str, ...] = ()
    """Neutral field names the diff planned to write, before policy withholding."""

    written_fields: tuple[str, ...] = ()
    """Neutral field names the target reported as actually written."""

    dropped: tuple[DroppedField, ...] = ()
    """Fields that differ but the target's manifest cannot carry, with the reason."""

    withheld: tuple[WithheldField, ...] = ()
    """Fields the engine held back (D7 activation, manual-edit policy)."""

    target_skipped_fields: tuple[str, ...] = ()
    """Fields the target reported as *not* written, from ``WriteResult.skipped_fields``.

    This is the channel decisions D2 and D3 use — an unresolved Qlik dataset member or an
    owner email with no matching Qlik user is omitted and reported here, never invented —
    and it is also where a connector reports a field it found already matching. The
    connector's own explanation is in :attr:`detail`; the engine carries both through
    verbatim rather than guessing which meaning applied."""

    holds_watermark: bool = False
    """True when this record leaves work outstanding, which pins the watermark."""

    def to_json(self) -> dict[str, Any]:
        """This record as plain JSON, for the dry-run plan file."""
        return {
            "native_key": self.native_key,
            "entity_type": self.entity_type.value,
            "outcome": self.outcome.value,
            "neutral_id": None if self.neutral_id is None else str(self.neutral_id),
            "display_name": self.display_name,
            "target_native_key": self.target_native_key,
            "reason": None if self.reason is None else self.reason.value,
            "detail": self.detail,
            "was_read": self.was_read,
            "changed_fields": list(self.changed_fields),
            "written_fields": list(self.written_fields),
            "dropped": [
                {
                    "field": dropped.field,
                    "reason": dropped.reason.value,
                    "capability_mode": (
                        None
                        if dropped.capability_mode is None
                        else dropped.capability_mode.value
                    ),
                    "native_path": dropped.native_path,
                }
                for dropped in self.dropped
            ],
            "withheld": [withheld.to_json() for withheld in self.withheld],
            "target_skipped_fields": list(self.target_skipped_fields),
            "holds_watermark": self.holds_watermark,
        }


@dataclass(frozen=True, slots=True)
class OrphanReport:
    """A source object that vanished. Recorded, reported, never deleted (decision D4)."""

    neutral_id: uuid.UUID
    entity_type: EntityType
    endpoint: str
    native_key: str | None
    observed_at: datetime

    def to_json(self) -> dict[str, Any]:
        """This orphan as plain JSON."""
        return {
            "neutral_id": str(self.neutral_id),
            "entity_type": self.entity_type.value,
            "endpoint": self.endpoint,
            "native_key": self.native_key,
            "observed_at": _iso(self.observed_at),
        }


@dataclass(frozen=True, slots=True)
class ErrorReport:
    """One error the cycle hit, classified by the SDK exception type that carried it."""

    kind: str
    """The exception class name — ``"TransientError"``, ``"AuthError"``, ... — or
    ``"EngineError"`` for a failure that was not a typed connector error."""

    message: str
    endpoint: str | None = None
    native_key: str | None = None
    operation: str | None = None
    retryable: bool = False
    fatal: bool = False
    """True when this error stopped the cycle rather than one record."""

    def to_json(self) -> dict[str, Any]:
        """This error as plain JSON."""
        return {
            "kind": self.kind,
            "message": self.message,
            "endpoint": self.endpoint,
            "native_key": self.native_key,
            "operation": self.operation,
            "retryable": self.retryable,
            "fatal": self.fatal,
        }


@dataclass(frozen=True, slots=True)
class SyncRunReport:
    """Everything one cycle did — the loop's actual return value.

    Built as a value rather than as log lines because three downstream tasks consume it:
    T2.8 serializes it as the dry-run plan (:meth:`to_json`), T2.9 reads the orphans out of
    it, and T8.1 asserts the pilot against it field by field.
    """

    pair: str
    source_endpoint: str
    target_endpoint: str
    entity_type: EntityType
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    dry_run: bool = False
    committed: bool = False
    create_enabled: bool = False
    watermark_before: str | None = None
    watermark_after: str | None = None
    watermark_advanced: bool = False
    watermark_held_by: tuple[str, ...] = ()
    has_more: bool = False
    pages: int = 0
    records: tuple[RecordReport, ...] = ()
    orphans: tuple[OrphanReport, ...] = ()
    errors: tuple[ErrorReport, ...] = ()
    quarantined_endpoints: tuple[str, ...] = ()

    # -- counts -------------------------------------------------------------------------

    def count(self, outcome: RecordOutcome) -> int:
        """How many records ended with ``outcome``."""
        return sum(1 for record in self.records if record.outcome is outcome)

    def records_with(self, reason: SkipReason) -> tuple[RecordReport, ...]:
        """Every record skipped for ``reason``."""
        return tuple(record for record in self.records if record.reason is reason)

    @property
    def read_count(self) -> int:
        """Candidates the source served a full ``read`` for."""
        return sum(1 for record in self.records if record.was_read)

    @property
    def write_count(self) -> int:
        """Records whose write actually changed the target."""
        return self.count(RecordOutcome.CREATED) + self.count(RecordOutcome.WRITTEN)

    @property
    def ok(self) -> bool:
        """True only for a cycle that committed with nothing outstanding."""
        return self.status is RunStatus.OK

    @property
    def dropped_fields(self) -> tuple[tuple[str, DroppedField], ...]:
        """``(native_key, dropped)`` for every field the target's manifest could not carry."""
        return tuple(
            (record.native_key, dropped) for record in self.records for dropped in record.dropped
        )

    @property
    def withheld_fields(self) -> tuple[tuple[str, WithheldField], ...]:
        """``(native_key, withheld)`` for every field engine policy held back."""
        return tuple(
            (record.native_key, held) for record in self.records for held in record.withheld
        )

    @property
    def target_skipped_fields(self) -> tuple[tuple[str, str], ...]:
        """``(native_key, field)`` for every field the target reported as not written.

        Decisions D2 and D3 surface here: a data-product member the target could not
        resolve, or an owner with no matching user, is reported rather than invented.
        """
        return tuple(
            (record.native_key, name)
            for record in self.records
            for name in record.target_skipped_fields
        )

    def to_json(self) -> dict[str, Any]:
        """The whole report as plain JSON — T2.8's machine-readable plan."""
        return {
            "pair": self.pair,
            "source_endpoint": self.source_endpoint,
            "target_endpoint": self.target_endpoint,
            "entity_type": self.entity_type.value,
            "status": self.status.value,
            "dry_run": self.dry_run,
            "committed": self.committed,
            "create_enabled": self.create_enabled,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "duration_seconds": self.duration_seconds,
            "watermark": {
                "before": self.watermark_before,
                "after": self.watermark_after,
                "advanced": self.watermark_advanced,
                "held_by": list(self.watermark_held_by),
                "has_more": self.has_more,
                "pages": self.pages,
            },
            "counts": {
                "read": self.read_count,
                "created": self.count(RecordOutcome.CREATED),
                "written": self.count(RecordOutcome.WRITTEN),
                "unchanged": self.count(RecordOutcome.UNCHANGED),
                "no_op": self.count(RecordOutcome.NO_OP),
                "skipped": self.count(RecordOutcome.SKIPPED),
                "orphaned": self.count(RecordOutcome.ORPHANED),
                "filtered": self.count(RecordOutcome.FILTERED),
                "failed": self.count(RecordOutcome.FAILED),
            },
            "records": [record.to_json() for record in self.records],
            "orphans": [orphan.to_json() for orphan in self.orphans],
            "errors": [error.to_json() for error in self.errors],
            "quarantined_endpoints": list(self.quarantined_endpoints),
        }


# ==========================================================================================
# The manual-edit seam (T2.5)
# ==========================================================================================


@dataclass(frozen=True, slots=True)
class WriteReview:
    """Everything a :class:`WritePolicy` needs to judge one planned entity write."""

    pair: SyncPairConfig
    entity_type: EntityType
    neutral_id: uuid.UUID
    target_ref: IdentityRef
    plan: DiffPlan
    """The computed diff, already shaped by the target's capability manifest."""

    source_envelopes: Mapping[str, FieldEnvelope[JsonValue]]
    stored_target_envelopes: Mapping[str, FieldEnvelope[JsonValue]]
    """What the engine last knew the target held. A target value that differs from this
    *now* is a manual edit — which is the whole question T2.5 answers."""

    target: Connector
    """The target connector, so a policy may re-read the live value before deciding."""


@runtime_checkable
class WritePolicy(Protocol):
    """The seam T2.5's manual-edit policy plugs into.

    Consulted once per planned entity write, after the diff and before anything is sent.
    Returns a mapping of neutral field name to a stable reason code for every field that
    must **not** be written; an empty mapping writes the diff as planned, which is v1's
    source-wins default.

    ``async`` on purpose: deciding whether a Qlik value was hand-edited needs the target's
    current value, and that is a read.
    """

    async def withhold(self, review: WriteReview) -> Mapping[str, str]:
        """Fields to hold back, mapped to why."""
        ...


# ==========================================================================================
# Pending state mutations (accumulated during the plan phase, applied in one transaction)
# ==========================================================================================


@dataclass(frozen=True, slots=True)
class _PendingBinding:
    neutral_id: uuid.UUID
    identity: IdentityRef
    confirmed: bool


@dataclass(frozen=True, slots=True)
class _PendingEnvelope:
    neutral_id: uuid.UUID
    endpoint: str
    entity_type: EntityType
    field: str
    envelope: FieldEnvelope[JsonValue]


@dataclass(frozen=True, slots=True)
class _PendingOrphan:
    neutral_id: uuid.UUID
    endpoint: str
    entity_type: EntityType
    native_key: str | None
    last_seen_at: datetime | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class _PendingOrphanResolution:
    neutral_id: uuid.UUID
    endpoint: str
    entity_type: EntityType


@dataclass(slots=True)
class _Mutations:
    """What the plan phase decided to persist, plus the in-flight indexes it reads back.

    The indexes exist because the plan phase's own writes are not visible to the state
    store's point reads until the commit phase runs. A source that reports two changes for
    the same object in one listing must not mint two neutral ids for it, must not create it
    twice, and must not re-diff it against a baseline that ignores the write just made.
    """

    bindings: list[_PendingBinding] = field(default_factory=list)
    envelopes: list[_PendingEnvelope] = field(default_factory=list)
    orphans: list[_PendingOrphan] = field(default_factory=list)
    resolved_orphans: list[_PendingOrphanResolution] = field(default_factory=list)

    source_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    """Source native key -> the neutral id minted for it this cycle."""

    target_refs: dict[uuid.UUID, IdentityRef] = field(default_factory=dict)
    """Neutral id -> the target identity created for it this cycle."""

    envelope_index: dict[uuid.UUID, dict[str, FieldEnvelope[JsonValue]]] = field(
        default_factory=dict
    )
    """Neutral id -> the target envelopes queued for it this cycle."""

    @property
    def is_empty(self) -> bool:
        """True when this cycle decided to change nothing in the state store."""
        return not (self.bindings or self.envelopes or self.orphans or self.resolved_orphans)


class _CycleAborted(Exception):
    """Internal: a failure that ends the cycle without committing anything."""

    def __init__(self, error: ErrorReport, *, quarantine: str | None = None) -> None:
        super().__init__(error.message)
        self.error = error
        self.quarantine = quarantine


# ==========================================================================================
# The loop
# ==========================================================================================


class SyncLoop:
    """One configured source-to-target pair, cycled one entity type at a time.

    Construct it once per pair and call :meth:`run_cycle` on a cadence (T2.6's scheduler
    does exactly that). The loop holds no per-cycle state of its own: everything durable
    lives in the state store, and everything ephemeral lives in the cycle's report.

    :param pair: The pair being synced. Supplies the ``catalog.schema`` selector (D1), the
        target space, the activation opt-in (D7), and the pair name every log line, metric
        and watermark row is keyed by.
    :param source: The source connector. Only ever read from.
    :param target: The Qlik connector — the sole write target in v1.
    :param store: The state store. The watermark, the last-known target envelopes, the
        identity map and the orphan log all live here.
    :param resolver: Identity resolution. Used for its read surface; the loop performs its
        own binding writes so they land inside the cycle's single transaction.
    :param metrics: Any :class:`~qlabs_catalog_sync_sdk.config.MetricsHandle` — in
        production T2.7's ``PrometheusMetrics``. Only the metric names T2.7 already
        declares are emitted.
    :param health: Optional health registry; endpoints are marked healthy or degraded as
        cycles succeed or fail, which is what makes ``/healthz`` reflect reality.
    :param write_policy: T2.5's manual-edit policy, or ``None`` for v1's source-wins
        default. Applied *after* the unconditional D7 activation rule.
    :param native_update_paths: Optional per-entity-type neutral-field to native-path
        mapping. Supply it only when it is complete; omitted, the target connector's own
        translation remains the authoritative gate on its update-path enum.
    :param create_missing: Whether a source object with no target binding may be created at
        the target. **Off by default** — see the module docstring.
    :param dry_run: Plan everything, apply nothing (no writes, no state).
    :param preflight: Run ``healthcheck`` on both endpoints before the cycle. An unhealthy
        endpoint quarantines the pair for this cycle instead of failing mid-write.
    :param max_pages: Safety bound on paged listings within one cycle.
    :param retry_attempts: Extra attempts after a :class:`TransientError`, on top of the
        first.
    :param retry_backoff_seconds: Base delay for exponential backoff, used when the error
        carries no ``retry_after_seconds`` hint.
    :param clock: Timestamp source; must return timezone-aware UTC.
    :param sleep: Backoff sleep, injectable so tests never actually wait.
    """

    def __init__(
        self,
        *,
        pair: SyncPairConfig,
        source: Connector,
        target: Connector,
        store: StateStore,
        resolver: IdentityResolver,
        metrics: MetricsHandle | None = None,
        health: HealthRegistry | None = None,
        write_policy: WritePolicy | None = None,
        native_update_paths: Mapping[EntityType, Mapping[str, str]] | None = None,
        create_missing: bool = False,
        dry_run: bool = False,
        preflight: bool = True,
        max_pages: int = 100,
        retry_attempts: int = 2,
        retry_backoff_seconds: float = 0.5,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = _default_sleep,
    ) -> None:
        if source.name == target.name:
            raise ValueError(
                f"sync pair {pair.name!r}: source and target are the same endpoint "
                f"({source.name!r}); a pair must have two distinct endpoints"
            )
        self._pair = pair
        self._source = source
        self._target = target
        self._store = store
        self._resolver = resolver
        self._metrics = metrics
        self._health = health
        self._write_policy = write_policy
        self._native_update_paths: Mapping[EntityType, Mapping[str, str]] = (
            native_update_paths if native_update_paths is not None else {}
        )
        self._create_missing = create_missing
        self._dry_run = dry_run
        self._preflight = preflight
        self._max_pages = max_pages
        self._retry_attempts = retry_attempts
        self._retry_backoff = retry_backoff_seconds
        self._clock = clock
        self._sleep = sleep

    # -- properties -----------------------------------------------------------------------

    @property
    def pair(self) -> SyncPairConfig:
        """The pair this loop syncs."""
        return self._pair

    @property
    def source_endpoint(self) -> str:
        """The source endpoint key."""
        return self._source.name

    @property
    def target_endpoint(self) -> str:
        """The target endpoint key."""
        return self._target.name

    @property
    def dry_run(self) -> bool:
        """True when this loop plans without applying anything."""
        return self._dry_run

    # -- the cycle ------------------------------------------------------------------------

    async def run_cycle(self, entity_type: EntityType) -> SyncRunReport:
        """Run one sync cycle for ``entity_type`` and return what it did.

        Never raises for an endpoint failure: a failed cycle comes back as a report with
        :attr:`RunStatus.FAILED`, ``committed=False`` and the error described, so the
        scheduler keeps running and the CLI can print what went wrong. Programming errors in
        the engine itself surface the same way, logged with their traceback.
        """
        started_at = self._clock()
        started_perf = time.perf_counter()
        records: list[RecordReport] = []
        orphans: list[OrphanReport] = []
        errors: list[ErrorReport] = []
        quarantined: list[str] = []
        mutations = _Mutations()
        pages = 0
        has_more = False
        committed = False
        watermark_advanced = False

        with bind_sync_context(
            pair=self._pair.name,
            endpoint=self._source.name,
            entity_type=entity_type.value,
        ):
            since = await self._load_watermark(entity_type)
            committable = since

            unsupported = self._unsupported_reason(entity_type)
            if unsupported is not None:
                _LOG.warning("sync.cycle.not_scheduled", detail=unsupported)
                errors.append(
                    ErrorReport(
                        kind="EngineError",
                        message=unsupported,
                        endpoint=self._target.name,
                        operation="capabilities",
                        fatal=True,
                    )
                )
                return self._finish(
                    entity_type=entity_type,
                    status=RunStatus.SKIPPED,
                    started_at=started_at,
                    started_perf=started_perf,
                    since=since,
                    committable=since,
                    watermark_advanced=False,
                    committed=False,
                    pages=0,
                    has_more=False,
                    records=records,
                    orphans=orphans,
                    errors=errors,
                    quarantined=quarantined,
                    observe=False,
                )

            _LOG.info("sync.cycle.start", dry_run=self._dry_run, watermark=since.kind.value)
            status = RunStatus.OK

            try:
                await self._preflight_check()
                open_orphans = await self._open_orphan_ids(entity_type)
                cursor = since
                all_terminal = True

                while pages < self._max_pages:
                    page = await self._list_changed(entity_type, cursor)
                    pages += 1
                    cursor = page.next_watermark
                    has_more = page.has_more

                    page_terminal = True
                    for change in page.changes:
                        record = await self._process(
                            change,
                            entity_type=entity_type,
                            mutations=mutations,
                            orphans=orphans,
                            errors=errors,
                            open_orphans=open_orphans,
                        )
                        records.append(record)
                        if record.holds_watermark:
                            page_terminal = False

                    if all_terminal and page_terminal:
                        committable = page.next_watermark
                    else:
                        all_terminal = False

                    if not page.has_more:
                        break

                status = (
                    RunStatus.PARTIAL
                    if any(record.holds_watermark for record in records)
                    else RunStatus.OK
                )

                if self._dry_run:
                    _LOG.info("sync.cycle.dry_run", records=len(records))
                else:
                    await self._commit(entity_type, mutations, committable, status)
                    committed = True
                    watermark_advanced = committable != since

            except _CycleAborted as aborted:
                errors.append(aborted.error)
                status = RunStatus.FAILED
                committed = False
                watermark_advanced = False
                if aborted.quarantine is not None:
                    quarantined.append(aborted.quarantine)
                    self._mark_degraded(aborted.quarantine, aborted.error.message)
                self._count_error(aborted.error.endpoint)
                _LOG.error(
                    "sync.cycle.aborted",
                    kind=aborted.error.kind,
                    detail=aborted.error.message,
                    endpoint=aborted.error.endpoint,
                )
            except Exception as exc:
                # A bug in the engine must surface as a failed cycle, not kill the
                # scheduler thread that runs every other pair.
                errors.append(
                    ErrorReport(
                        kind="EngineError",
                        message=f"{type(exc).__name__}: {exc}",
                        endpoint=self._source.name,
                        fatal=True,
                    )
                )
                status = RunStatus.FAILED
                committed = False
                watermark_advanced = False
                self._count_error(self._source.name)
                _LOG.exception("sync.cycle.error")

            if status is not RunStatus.FAILED:
                self._mark_healthy(self._source.name)
                self._mark_healthy(self._target.name)

            return self._finish(
                entity_type=entity_type,
                status=status,
                started_at=started_at,
                started_perf=started_perf,
                since=since,
                committable=committable,
                watermark_advanced=watermark_advanced,
                committed=committed,
                pages=pages,
                has_more=has_more,
                records=records,
                orphans=orphans,
                errors=errors,
                quarantined=quarantined,
            )

    # -- one record -------------------------------------------------------------------------

    async def _process(
        self,
        change: ChangeRef,
        *,
        entity_type: EntityType,
        mutations: _Mutations,
        orphans: list[OrphanReport],
        errors: list[ErrorReport],
        open_orphans: set[uuid.UUID],
    ) -> RecordReport:
        """Plan (and, unless this is a dry run, apply) one candidate change."""
        native_key = change.ref.native_key

        if not self._selects(change):
            return RecordReport(
                native_key=native_key,
                entity_type=entity_type,
                outcome=RecordOutcome.FILTERED,
                display_name=change.display_name,
                reason=SkipReason.NOT_SELECTED,
                detail=(
                    "outside this pair's catalog.schema selector "
                    f"({', '.join(self._pair.catalog_schema_patterns)})"
                ),
            )

        known_id = await self._source_neutral_id(change, mutations)

        if change.is_delete:
            return self._orphan(
                change,
                entity_type=entity_type,
                neutral_id=known_id,
                mutations=mutations,
                orphans=orphans,
            )

        neutral_id = known_id if known_id is not None else uuid.uuid4()

        with bind_sync_context(neutral_id=str(neutral_id)):
            try:
                entity = await self._read(change.ref)
            except NotFound:
                return self._orphan(
                    change,
                    entity_type=entity_type,
                    neutral_id=known_id,
                    mutations=mutations,
                    orphans=orphans,
                )

            self._count_read(entity_type)
            if known_id is None:
                # RS-07 step 3: allocate a neutral id and record the *source*-side mapping.
                # Safe unattended precisely because it binds a key to an id the engine
                # minted for it -- it matches nothing and can therefore claim nothing.
                mutations.bindings.append(
                    _PendingBinding(neutral_id=neutral_id, identity=change.ref, confirmed=True)
                )
                mutations.source_ids[native_key] = neutral_id
            if neutral_id in open_orphans:
                open_orphans.discard(neutral_id)
                mutations.resolved_orphans.append(
                    _PendingOrphanResolution(
                        neutral_id=neutral_id,
                        endpoint=self._source.name,
                        entity_type=entity_type,
                    )
                )

            source_envelopes = dict(entity.field_envelopes)
            if not source_envelopes:
                return self._skip(
                    change,
                    entity_type,
                    neutral_id,
                    SkipReason.NO_SOURCE_ENVELOPES,
                    detail=(
                        f"connector {self._source.name!r} returned no field envelopes for "
                        f"{native_key!r}, so there is nothing to diff"
                    ),
                )

            counterpart = await self._target_ref(neutral_id, entity_type, mutations)
            if counterpart is None:
                return await self._create_or_skip(
                    change,
                    entity=entity,
                    entity_type=entity_type,
                    neutral_id=neutral_id,
                    source_envelopes=source_envelopes,
                    mutations=mutations,
                    errors=errors,
                )

            return await self._update(
                change,
                entity_type=entity_type,
                neutral_id=neutral_id,
                target_ref=counterpart,
                source_envelopes=source_envelopes,
                mutations=mutations,
                errors=errors,
            )

    async def _source_neutral_id(
        self, change: ChangeRef, mutations: _Mutations
    ) -> uuid.UUID | None:
        """The neutral id already bound to this source key, in the store or this cycle."""
        pending = mutations.source_ids.get(change.ref.native_key)
        if pending is not None:
            return pending
        return await self._resolver.resolve_neutral_id(change.ref)

    async def _target_ref(
        self, neutral_id: uuid.UUID, entity_type: EntityType, mutations: _Mutations
    ) -> IdentityRef | None:
        """The target identity this neutral id may be written to, or ``None``.

        A binding awaiting human confirmation deliberately answers ``None`` here and is
        reported by the caller: an unconfirmed bootstrap proposal is not a licence to write
        (T7.1). Objects created earlier in this same cycle are found in the in-flight index
        rather than being created a second time.
        """
        created = mutations.target_refs.get(neutral_id)
        if created is not None:
            return created
        binding = await self._resolver.counterpart(neutral_id, self._target.name, entity_type)
        if binding is None or not binding.confirmed:
            return None
        return binding.identity

    async def _unconfirmed_binding_detail(
        self, neutral_id: uuid.UUID, entity_type: EntityType
    ) -> str | None:
        """A description of an unconfirmed binding, when one is what blocked the write."""
        binding = await self._resolver.counterpart(neutral_id, self._target.name, entity_type)
        if binding is None or binding.confirmed:
            return None
        return (
            f"binding to {binding.identity.native_key!r} at {self._target.name!r} is not "
            "confirmed; confirm it in the identity review before this object is written"
        )

    # -- the write paths ---------------------------------------------------------------------

    async def _update(
        self,
        change: ChangeRef,
        *,
        entity_type: EntityType,
        neutral_id: uuid.UUID,
        target_ref: IdentityRef,
        source_envelopes: Mapping[str, FieldEnvelope[JsonValue]],
        mutations: _Mutations,
        errors: list[ErrorReport],
    ) -> RecordReport:
        """Diff against the last-known target state and, if anything differs, write it."""
        stored = await self._stored_target_envelopes(neutral_id, mutations)
        try:
            plan = self._diff(entity_type, source_envelopes, stored)
        except CapabilityError as exc:
            return self._capability_skip(change, entity_type, neutral_id, exc, errors, target_ref)

        if plan.is_empty:
            # RS-07 step 8: the short-circuit. Nothing reaches the write path at all.
            self._count_skip(entity_type)
            return RecordReport(
                native_key=change.ref.native_key,
                entity_type=entity_type,
                outcome=RecordOutcome.UNCHANGED,
                neutral_id=neutral_id,
                display_name=change.display_name,
                target_native_key=target_ref.native_key,
                was_read=True,
                dropped=plan.dropped,
            )

        withheld = await self._withhold(
            entity_type=entity_type,
            neutral_id=neutral_id,
            target_ref=target_ref,
            plan=plan,
            source_envelopes=source_envelopes,
            stored=stored,
        )
        effective = self._effective_diff(plan, withheld)
        planned_fields = tuple(plan.changed_field_names)

        if not effective.changes:
            self._count_skip(entity_type)
            return RecordReport(
                native_key=change.ref.native_key,
                entity_type=entity_type,
                outcome=RecordOutcome.SKIPPED,
                neutral_id=neutral_id,
                display_name=change.display_name,
                target_native_key=target_ref.native_key,
                reason=SkipReason.NOTHING_TO_WRITE,
                detail="every changed field was withheld by policy or dropped by the manifest",
                was_read=True,
                changed_fields=planned_fields,
                dropped=plan.dropped,
                withheld=withheld,
            )

        self._count_planned(entity_type)
        if self._dry_run:
            return RecordReport(
                native_key=change.ref.native_key,
                entity_type=entity_type,
                outcome=RecordOutcome.WRITTEN,
                neutral_id=neutral_id,
                display_name=change.display_name,
                target_native_key=target_ref.native_key,
                was_read=True,
                changed_fields=planned_fields,
                written_fields=tuple(effective.field_names),
                dropped=plan.dropped,
                withheld=withheld,
            )

        try:
            result, applied = await self._apply_update(
                target_ref, effective, entity_type, source_envelopes, withheld
            )
        except NotFound as exc:
            self._record_error(errors, exc, change.ref.native_key, "update")
            return self._skip(
                change,
                entity_type,
                neutral_id,
                SkipReason.TARGET_NOT_FOUND,
                detail=(
                    f"{target_ref.native_key!r} no longer exists at {self._target.name!r}; "
                    "the stored binding is stale (v1 neither deletes nor rebinds it)"
                ),
                target_native_key=target_ref.native_key,
            )
        except ConflictError as exc:
            self._record_error(errors, exc, change.ref.native_key, "update")
            return self._skip(
                change,
                entity_type,
                neutral_id,
                SkipReason.CONFLICT_UNRESOLVED,
                detail=(
                    f"{target_ref.native_key!r} changed underneath the re-read and re-diff "
                    f"retry: {exc.message}"
                ),
                target_native_key=target_ref.native_key,
            )
        except CapabilityError as exc:
            return self._capability_skip(change, entity_type, neutral_id, exc, errors, target_ref)

        persisted = self._fields_to_persist(result, applied)
        self._queue_envelopes(
            mutations, neutral_id, entity_type, persisted, source_envelopes, result
        )

        if result.outcome is WriteOutcome.NO_OP:
            self._count_skip(entity_type)
            outcome = RecordOutcome.NO_OP
        else:
            self._count_applied(entity_type)
            outcome = RecordOutcome.WRITTEN

        return RecordReport(
            native_key=change.ref.native_key,
            entity_type=entity_type,
            outcome=outcome,
            neutral_id=neutral_id,
            display_name=change.display_name,
            target_native_key=target_ref.native_key,
            detail=result.detail,
            was_read=True,
            changed_fields=planned_fields,
            written_fields=tuple(result.written_fields),
            dropped=plan.dropped,
            withheld=withheld,
            target_skipped_fields=tuple(result.skipped_fields),
        )

    async def _apply_update(
        self,
        target_ref: IdentityRef,
        diff: FieldDiff,
        entity_type: EntityType,
        source_envelopes: Mapping[str, FieldEnvelope[JsonValue]],
        withheld: Sequence[WithheldField],
    ) -> tuple[WriteResult, FieldDiff]:
        """Send ``diff``; on a concurrency conflict, re-read, re-diff and retry once.

        RS-07 section 2, step 6. The retry is never a blind resend: the target's *current*
        state is read back and the diff recomputed against it, so what goes out the second
        time reflects whatever changed underneath us. The withheld set is reapplied, so D7
        and the manual-edit policy still hold on the retry.
        """
        try:
            result = await self._call(
                "update", self._target.name, lambda: self._target.update(target_ref, diff)
            )
            return result, diff
        except ConflictError as first:
            _LOG.warning(
                "sync.write.conflict",
                native_key=target_ref.native_key,
                expected_revision=first.expected_revision,
                actual_revision=first.actual_revision,
            )
            fresh = await self._call(
                "read", self._target.name, lambda: self._target.read(target_ref)
            )
            replan = self._diff(entity_type, source_envelopes, dict(fresh.field_envelopes))
            retry = self._effective_diff(replan, withheld)
            if not retry.changes:
                return (
                    WriteResult.no_op(
                        target_ref,
                        source_revision=replan.expected_revision,
                        detail="target already matched after the conflict re-read",
                    ),
                    retry,
                )
            result = await self._call(
                "update", self._target.name, lambda: self._target.update(target_ref, retry)
            )
            return result, retry

    async def _create_or_skip(
        self,
        change: ChangeRef,
        *,
        entity: NeutralEntity,
        entity_type: EntityType,
        neutral_id: uuid.UUID,
        source_envelopes: Mapping[str, FieldEnvelope[JsonValue]],
        mutations: _Mutations,
        errors: list[ErrorReport],
    ) -> RecordReport:
        """No usable counterpart at the target: create one, or refuse to guess and report."""
        unconfirmed = await self._unconfirmed_binding_detail(neutral_id, entity_type)
        if unconfirmed is not None:
            return self._skip(
                change,
                entity_type,
                neutral_id,
                SkipReason.UNCONFIRMED_TARGET_BINDING,
                detail=unconfirmed,
            )

        if not self._create_missing:
            return self._skip(
                change,
                entity_type,
                neutral_id,
                SkipReason.NO_TARGET_BINDING,
                detail=(
                    f"no confirmed binding for {change.ref.native_key!r} at "
                    f"{self._target.name!r}; run identity bootstrap and confirm the match, "
                    "or enable creation for this pair"
                ),
            )

        candidate = entity.model_copy(update={"neutral_id": neutral_id, "identities": []})
        withheld: tuple[WithheldField, ...] = ()
        if isinstance(candidate, DataProduct):
            if not candidate.placement:
                # The pair's target_space is where a newly created product belongs; never
                # override a placement the source actually reported.
                candidate = candidate.model_copy(update={"placement": self._pair.target_space})
            if not self._pair.activation_opt_in and candidate.status is not None:
                # D7 again, and for the same reason: without the opt-in the engine does not
                # tell the target what lifecycle state to be in, on create any more than on
                # update. Stripping the field is the create-path equivalent of withholding
                # it from a diff -- there is no diff here to withhold it from.
                candidate = candidate.model_copy(update={"status": None})
                withheld = (
                    WithheldField(field=ACTIVATION_FIELD, reason=ACTIVATION_WITHHELD_REASON),
                )

        # Ask the manifest whether the target would accept this create *before* planning it.
        # A dry run exists so an operator can trust the plan; one that promises creates the
        # apply then refuses is the exact failure it is meant to prevent. Note the test is
        # not "is the entity type supported" — Qlik supports datasets, it simply declares
        # every dataset field read-only (D2: datasets are resolved, never created). So the
        # rule is the connector's own: an entity with no writable field cannot be created.
        if not self._target_can_create(entity_type):
            # Phrased to match what the connector's own gate says on the apply path, so an
            # operator reading a dry-run plan and an apply report sees one explanation.
            refusal = CapabilityError(
                f"connector {self._target.name!r} declares every {entity_type.value!r} field "
                "read-only, so it can never create one",
                endpoint=self._target.name,
                entity_type=entity_type.value,
                capability_mode="ro",
                operation="create",
            )
            return self._capability_skip(change, entity_type, neutral_id, refusal, errors, None)

        self._count_planned(entity_type)
        if self._dry_run:
            return RecordReport(
                native_key=change.ref.native_key,
                entity_type=entity_type,
                outcome=RecordOutcome.CREATED,
                neutral_id=neutral_id,
                display_name=change.display_name,
                was_read=True,
                changed_fields=tuple(sorted(source_envelopes)),
                withheld=withheld,
            )

        try:
            result = await self._call(
                "create", self._target.name, lambda: self._target.create(candidate)
            )
        except CapabilityError as exc:
            return self._capability_skip(change, entity_type, neutral_id, exc, errors, None)

        # The engine minted this object, so the binding is not a bootstrap match and needs
        # no human confirmation: it points at the native key the target itself returned.
        mutations.bindings.append(
            _PendingBinding(neutral_id=neutral_id, identity=result.ref, confirmed=True)
        )
        mutations.target_refs[neutral_id] = result.ref
        await self._anchor_created_object(neutral_id, change.ref, result.ref)
        held = {item.field for item in withheld}
        persisted = tuple(
            name
            for name in result.written_fields
            if name in source_envelopes and name not in held
        )
        self._queue_envelopes(
            mutations, neutral_id, entity_type, persisted, source_envelopes, result
        )
        self._count_applied(entity_type)

        return RecordReport(
            native_key=change.ref.native_key,
            entity_type=entity_type,
            outcome=RecordOutcome.CREATED,
            neutral_id=neutral_id,
            display_name=change.display_name,
            target_native_key=result.ref.native_key,
            detail=result.detail,
            was_read=True,
            changed_fields=tuple(sorted(source_envelopes)),
            written_fields=persisted,
            withheld=withheld,
            target_skipped_fields=tuple(result.skipped_fields),
        )

    async def _anchor_created_object(
        self, neutral_id: uuid.UUID, source_ref: IdentityRef, target_ref: IdentityRef
    ) -> None:
        """Commit the two bindings for a just-created target object, immediately.

        This is the one write the cycle does **not** defer to its single transaction, and
        the reason is the asymmetry the module docstring describes: a create at the target
        cannot be undone by a database rollback. The binding is the only record of where
        the engine put the object, so it is committed at the moment it becomes true. Losing
        it would strand the new object and make the next cycle create a second copy of it —
        real damage in the customer's tenant, traded against a rollback that could not have
        removed the first copy anyway.

        Both sides are anchored, not just the target: without the source binding the next
        cycle would mint a different neutral id and never find the counterpart. Both writes
        are idempotent upserts and are queued for the cycle's own commit as well, so the
        normal path stays a single transaction.
        """
        now = self._clock()
        async with self._store.unit_of_work() as uow:
            await uow.bind_identity(neutral_id, source_ref, confirmed=True, now=now)
            await uow.bind_identity(neutral_id, target_ref, confirmed=True, now=now)
        _LOG.info(
            "sync.create.anchored",
            native_key=source_ref.native_key,
            target_native_key=target_ref.native_key,
        )

    # -- diffing and policy --------------------------------------------------------------------

    def _diff(
        self,
        entity_type: EntityType,
        source_envelopes: Mapping[str, FieldEnvelope[JsonValue]],
        target_envelopes: Mapping[str, FieldEnvelope[JsonValue]],
    ) -> DiffPlan:
        """The minimal write set.

        The arguments to :func:`~qlabs_catalog_sync.diff.compute_field_diff` are
        keyword-only on purpose: swapping source and target would invert the sync direction
        silently, so this is the one call site that has to get it right.
        """
        return compute_field_diff(
            entity_type=entity_type,
            source_envelopes=source_envelopes,
            target_envelopes=target_envelopes,
            manifest=self._target.capabilities(),
            native_update_paths=self._native_update_paths.get(entity_type),
            endpoint=self._target.name,
        )

    async def _withhold(
        self,
        *,
        entity_type: EntityType,
        neutral_id: uuid.UUID,
        target_ref: IdentityRef,
        plan: DiffPlan,
        source_envelopes: Mapping[str, FieldEnvelope[JsonValue]],
        stored: Mapping[str, FieldEnvelope[JsonValue]],
    ) -> tuple[WithheldField, ...]:
        """Fields the engine will not send, with a reason each.

        D7 first and unconditionally, then whatever the injected policy adds. The order
        matters: a policy cannot re-enable activation by leaving it out.
        """
        withheld: dict[str, str] = {}
        if (
            entity_type is EntityType.DATA_PRODUCT
            and not self._pair.activation_opt_in
            and plan.change_for(ACTIVATION_FIELD) is not None
        ):
            withheld[ACTIVATION_FIELD] = ACTIVATION_WITHHELD_REASON

        if self._write_policy is not None:
            review = WriteReview(
                pair=self._pair,
                entity_type=entity_type,
                neutral_id=neutral_id,
                target_ref=target_ref,
                plan=plan,
                source_envelopes=source_envelopes,
                stored_target_envelopes=stored,
                target=self._target,
            )
            for name, reason in (await self._write_policy.withhold(review)).items():
                withheld.setdefault(name, reason)

        return tuple(WithheldField(field=name, reason=withheld[name]) for name in sorted(withheld))

    @staticmethod
    def _effective_diff(plan: DiffPlan, withheld: Sequence[WithheldField]) -> FieldDiff:
        """``plan`` minus the withheld fields, keeping the concurrency guard intact."""
        held = {item.field for item in withheld}
        return FieldDiff(
            entity_type=plan.entity_type,
            changes=[change for change in plan.changes if change.field not in held],
            expected_revision=plan.expected_revision,
        )

    # -- persistence ------------------------------------------------------------------------

    async def _stored_target_envelopes(
        self, neutral_id: uuid.UUID, mutations: _Mutations
    ) -> dict[str, FieldEnvelope[JsonValue]]:
        """The diff baseline: what the engine last knew the target held.

        Committed rows first, then anything this same cycle already queued for the object,
        so a second change for one object in one listing diffs against the write just made
        instead of re-sending it.
        """
        stored = dict(await self._store.fetch_envelopes(neutral_id, self._target.name))
        stored.update(mutations.envelope_index.get(neutral_id, {}))
        return stored

    @staticmethod
    def _fields_to_persist(result: WriteResult, diff: FieldDiff) -> tuple[str, ...]:
        """Which fields the state store may now claim the target holds.

        Only what was actually written. A field the connector skipped (an unresolved Qlik
        dataset member, decision D2) is *not* persisted, so the next cycle diffs it again
        and reports it again rather than the engine quietly believing it landed.

        A ``NO_OP`` writes nothing but proves the target already matches every field in the
        diff, which is exactly the convergence case after an interrupted cycle — so the
        whole diff is persisted.
        """
        if result.outcome is WriteOutcome.NO_OP:
            return tuple(diff.field_names)
        if result.written_fields:
            return tuple(result.written_fields)
        return tuple(diff.field_names)

    def _queue_envelopes(
        self,
        mutations: _Mutations,
        neutral_id: uuid.UUID,
        entity_type: EntityType,
        fields: Sequence[str],
        source_envelopes: Mapping[str, FieldEnvelope[JsonValue]],
        result: WriteResult,
    ) -> None:
        """Queue the target-side envelopes for the fields that were written.

        The value and checksum are the source's — that is now what the target holds, and it
        is what the next cycle's checksum comparison must match. ``source_revision`` is
        replaced by the token the *target* returned, because that is the value a later
        ``if-match`` is guarded against (the diff engine reads ``expected_revision``
        straight off these rows).
        """
        now = self._clock()
        index = mutations.envelope_index.setdefault(neutral_id, {})
        for name in fields:
            envelope = source_envelopes.get(name)
            if envelope is None:
                continue
            persisted = envelope.model_copy(
                update={"source_revision": result.source_revision, "last_synced_at": now}
            )
            mutations.envelopes.append(
                _PendingEnvelope(
                    neutral_id=neutral_id,
                    endpoint=self._target.name,
                    entity_type=entity_type,
                    field=name,
                    envelope=persisted,
                )
            )
            index[name] = persisted

    async def _commit(
        self,
        entity_type: EntityType,
        mutations: _Mutations,
        watermark: Watermark,
        status: RunStatus,
    ) -> None:
        """Apply every state change and the watermark in **one** transaction.

        This is the whole restart-safety story: one session, one transaction, no partial
        commit. Anything that raised before this point committed nothing at all, and
        anything that raises inside it rolls the lot back — including the watermark, which
        is why a crash can never advance past unpersisted work.
        """
        now = self._clock()
        async with self._store.unit_of_work() as uow:
            await self._apply(uow, mutations, now)
            await uow.advance_watermark(
                self._pair.name,
                self._source.name,
                entity_type,
                watermark.model_dump_json(),
                status=status.value,
                run_at=now,
            )

    @staticmethod
    async def _apply(uow: UnitOfWork, mutations: _Mutations, now: datetime) -> None:
        """Write the accumulated mutations through one unit of work, in dependency order."""
        for binding in mutations.bindings:
            await uow.bind_identity(
                binding.neutral_id, binding.identity, confirmed=binding.confirmed, now=now
            )
        for pending in mutations.envelopes:
            await uow.upsert_field_envelope(
                pending.neutral_id,
                pending.endpoint,
                pending.entity_type,
                pending.field,
                pending.envelope,
                now=now,
            )
        for orphan in mutations.orphans:
            await uow.record_orphan(
                orphan.neutral_id,
                orphan.endpoint,
                orphan.entity_type,
                native_key=orphan.native_key,
                last_seen_at=orphan.last_seen_at,
                observed_at=orphan.observed_at,
            )
        for resolution in mutations.resolved_orphans:
            await uow.resolve_orphan(
                resolution.neutral_id, resolution.endpoint, resolution.entity_type, now=now
            )

    # -- watermark ---------------------------------------------------------------------------

    async def _load_watermark(self, entity_type: EntityType) -> Watermark:
        """The stored resume position, or a full-scan start.

        A token that cannot be parsed, or that belongs to a different stream, falls back to
        :meth:`Watermark.initial` with a warning rather than crashing: a full rescan is
        always safe here — every unchanged checksum short-circuits before any write — and
        refusing to run would be the worse failure.
        """
        state = await self._store.get_watermark(self._pair.name, self._source.name, entity_type)
        initial = Watermark.initial(self._source.name, entity_type)
        if state is None or state.watermark_token is None:
            return initial
        try:
            stored = Watermark.model_validate_json(state.watermark_token)
        except ValueError:
            _LOG.warning("sync.watermark.unreadable", detail="falling back to a full scan")
            return initial
        if not stored.same_stream_as(initial):
            _LOG.warning(
                "sync.watermark.wrong_stream",
                stored_stream=stored.stream_key,
                expected_stream=initial.stream_key,
            )
            return initial
        return stored

    # -- connector calls, with the documented reactions ----------------------------------------

    async def _preflight_check(self) -> None:
        """Health-check both endpoints; an unhealthy one quarantines the pair this cycle."""
        if not self._preflight:
            return
        for connector in (self._source, self._target):
            status = await self._call("healthcheck", connector.name, connector.healthcheck)
            if status.should_quarantine:
                raise _CycleAborted(
                    ErrorReport(
                        kind="HealthCheck",
                        message=status.reason or "endpoint reported itself unhealthy",
                        endpoint=connector.name,
                        operation="healthcheck",
                        fatal=True,
                    ),
                    quarantine=connector.name,
                )

    async def _list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        return await self._call(
            "list_changed",
            self._source.name,
            lambda: self._source.list_changed(entity_type, since),
        )

    async def _read(self, ref: IdentityRef) -> NeutralEntity:
        return await self._call("read", self._source.name, lambda: self._source.read(ref))

    async def _call(
        self, operation: str, endpoint: str, func: Callable[[], Awaitable[_T]]
    ) -> _T:
        """Invoke one connector call, applying the SDK's documented error reactions.

        ``TransientError`` is retried with backoff (honoring ``retry_after_seconds``, which
        also counts a rate-limit) and aborts the cycle once the attempts are spent.
        ``AuthError`` aborts immediately and quarantines the endpoint. Every other typed
        error is re-raised for the caller to classify per record: ``NotFound``,
        ``ConflictError`` and ``CapabilityError`` each mean something different depending on
        which call they came out of, and deciding that here would flatten the distinction.
        """
        attempt = 0
        while True:
            try:
                return await func()
            except AuthError as exc:
                raise _CycleAborted(
                    self._error_report(exc, operation=operation, endpoint=endpoint, fatal=True),
                    quarantine=endpoint,
                ) from exc
            except TransientError as exc:
                if exc.retry_after_seconds is not None:
                    self._count_rate_limited(endpoint)
                attempt += 1
                if attempt > self._retry_attempts:
                    raise _CycleAborted(
                        self._error_report(exc, operation=operation, endpoint=endpoint, fatal=True)
                    ) from exc
                delay = (
                    exc.retry_after_seconds
                    if exc.retry_after_seconds is not None
                    else self._retry_backoff * (2 ** (attempt - 1))
                )
                _LOG.warning(
                    "sync.retry",
                    operation=operation,
                    endpoint=endpoint,
                    attempt=attempt,
                    delay_seconds=delay,
                )
                await self._sleep(delay)

    # -- small builders ------------------------------------------------------------------------

    def _orphan(
        self,
        change: ChangeRef,
        *,
        entity_type: EntityType,
        neutral_id: uuid.UUID | None,
        mutations: _Mutations,
        orphans: list[OrphanReport],
    ) -> RecordReport:
        """Decision D4: record the disappearance, report it, and delete nothing."""
        if neutral_id is None:
            return RecordReport(
                native_key=change.ref.native_key,
                entity_type=entity_type,
                outcome=RecordOutcome.SKIPPED,
                display_name=change.display_name,
                reason=SkipReason.DELETED_UNKNOWN_OBJECT,
                detail="never bound here, so nothing was ever synced for it",
            )
        observed_at = self._clock()
        mutations.orphans.append(
            _PendingOrphan(
                neutral_id=neutral_id,
                endpoint=self._source.name,
                entity_type=entity_type,
                native_key=change.ref.native_key,
                last_seen_at=change.last_modified_at,
                observed_at=observed_at,
            )
        )
        orphans.append(
            OrphanReport(
                neutral_id=neutral_id,
                entity_type=entity_type,
                endpoint=self._source.name,
                native_key=change.ref.native_key,
                observed_at=observed_at,
            )
        )
        _LOG.info("sync.orphan", native_key=change.ref.native_key)
        return RecordReport(
            native_key=change.ref.native_key,
            entity_type=entity_type,
            outcome=RecordOutcome.ORPHANED,
            neutral_id=neutral_id,
            display_name=change.display_name,
            detail="gone at the source; recorded as an orphan and never deleted at the target",
        )

    def _skip(
        self,
        change: ChangeRef,
        entity_type: EntityType,
        neutral_id: uuid.UUID | None,
        reason: SkipReason,
        *,
        detail: str,
        target_native_key: str | None = None,
    ) -> RecordReport:
        """A record deliberately not written. Every call site here is after a successful read."""
        self._count_skip(entity_type)
        _LOG.info("sync.record.skipped", native_key=change.ref.native_key, reason=reason.value)
        return RecordReport(
            native_key=change.ref.native_key,
            entity_type=entity_type,
            outcome=RecordOutcome.SKIPPED,
            neutral_id=neutral_id,
            display_name=change.display_name,
            target_native_key=target_native_key,
            reason=reason,
            detail=detail,
            was_read=True,
            holds_watermark=reason not in _TERMINAL_SKIP_REASONS,
        )

    def _capability_skip(
        self,
        change: ChangeRef,
        entity_type: EntityType,
        neutral_id: uuid.UUID,
        exc: CapabilityError,
        errors: list[ErrorReport],
        target_ref: IdentityRef | None,
    ) -> RecordReport:
        """A capability refusal is a planning bug: loud, never retried, never forgotten."""
        self._record_error(errors, exc, change.ref.native_key, exc.operation or "update")
        return self._skip(
            change,
            entity_type,
            neutral_id,
            SkipReason.CAPABILITY_REFUSED,
            detail=exc.message,
            target_native_key=None if target_ref is None else target_ref.native_key,
        )

    def _unsupported_reason(self, entity_type: EntityType) -> str | None:
        """Why this cycle must not run at all, or ``None`` when it may."""
        if entity_type not in self._pair.entity_types:
            return (
                f"sync pair {self._pair.name!r} does not sync {entity_type.value!r} "
                f"(configured: {[e.value for e in self._pair.entity_types]})"
            )
        for connector in (self._source, self._target):
            if not connector.capabilities().supports(entity_type):
                return (
                    f"connector {connector.name!r} does not support {entity_type.value!r}, "
                    "so this pair and entity type combination is never scheduled"
                )
        return None

    def _target_can_create(self, entity_type: EntityType) -> bool:
        """Whether the target's manifest declares any writable field for ``entity_type``.

        Mirrors the rule the Qlik writer applies in its own capability gate, so the plan a
        dry run emits and the writes an apply makes agree. An entity the manifest does not
        support at all is also not creatable.
        """
        manifest = self._target.capabilities()
        if not manifest.supports(entity_type):
            return False
        capability = getattr(manifest, "entity_capability", None)
        if capability is None:  # a bespoke manifest: fall back to the contract's own guard
            return True
        declared = capability(entity_type)
        if declared is None:
            return False
        return any(manifest.is_writable(entity_type, field) for field in declared.fields)

    def _selects(self, change: ChangeRef) -> bool:
        """Decision D1: is this object inside the pair's ``catalog.schema`` selector?

        The selector speaks Unity Catalog, so it is applied to whichever key is shaped like
        one: ``catalog.schema`` for a schema-as-data-product, ``catalog.schema.table`` for a
        dataset.

        ``native_key`` is **not** that key for the MVP's only source. Databricks keys on the
        stable ``schema_id``/``table_id`` — a UUID, because names are renameable and a
        rename must not break identity — and carries the dotted name in
        ``secondary_keys["full_name"]``. Matching ``native_key`` alone therefore found no dot
        in any Databricks change and selected everything, so a pair scoped to one catalog
        silently synced every catalog in the metastore. The dotted name is preferred here and
        ``native_key`` is the fallback, so a connector that does key on the qualified name
        still works.

        A key with no dotted path anywhere — a Qlik id, an opaque handle — is not something
        these patterns can describe, and inventing a match or a refusal for it would be worse
        than leaving it to the pair's other scoping (its endpoints and entity types).
        """
        for candidate in (change.ref.secondary_keys.get("full_name"), change.ref.native_key):
            if not candidate:
                continue
            parts = candidate.split(".")
            if len(parts) >= 2:
                return self._pair.matches(parts[0], parts[1])
        return True

    async def _open_orphan_ids(self, entity_type: EntityType) -> set[uuid.UUID]:
        """Neutral ids currently recorded missing at the source, so a reappearance resolves."""
        orphans = await self._store.list_orphans(self._source.name, unresolved_only=True)
        return {orphan.neutral_id for orphan in orphans if orphan.entity_type is entity_type}

    def _finish(
        self,
        *,
        entity_type: EntityType,
        status: RunStatus,
        started_at: datetime,
        started_perf: float,
        since: Watermark,
        committable: Watermark,
        watermark_advanced: bool,
        committed: bool,
        pages: int,
        has_more: bool,
        records: list[RecordReport],
        orphans: list[OrphanReport],
        errors: list[ErrorReport],
        quarantined: list[str],
        observe: bool = True,
    ) -> SyncRunReport:
        """Assemble the report, emit the cycle metric, and log the one-line summary."""
        duration = time.perf_counter() - started_perf
        if observe:
            self._observe(
                METRIC_CYCLE_DURATION_SECONDS,
                duration,
                pair=self._pair.name,
                entity_type=entity_type.value,
            )
        report = SyncRunReport(
            pair=self._pair.name,
            source_endpoint=self._source.name,
            target_endpoint=self._target.name,
            entity_type=entity_type,
            status=status,
            started_at=started_at,
            finished_at=self._clock(),
            duration_seconds=duration,
            dry_run=self._dry_run,
            committed=committed,
            create_enabled=self._create_missing,
            watermark_before=None if since.is_initial else since.model_dump_json(),
            watermark_after=None if committable.is_initial else committable.model_dump_json(),
            watermark_advanced=watermark_advanced,
            watermark_held_by=tuple(r.native_key for r in records if r.holds_watermark),
            has_more=has_more,
            pages=pages,
            records=tuple(records),
            orphans=tuple(orphans),
            errors=tuple(errors),
            quarantined_endpoints=tuple(quarantined),
        )
        _LOG.info(
            "sync.cycle.finished",
            status=status.value,
            committed=committed,
            watermark_advanced=watermark_advanced,
            read=report.read_count,
            written=report.write_count,
            skipped=report.count(RecordOutcome.SKIPPED),
            orphaned=report.count(RecordOutcome.ORPHANED),
            duration_seconds=round(duration, 4),
        )
        return report

    # -- error and metric plumbing ---------------------------------------------------------------

    @staticmethod
    def _error_report(
        exc: ConnectorError,
        *,
        operation: str,
        endpoint: str,
        native_key: str | None = None,
        fatal: bool = False,
    ) -> ErrorReport:
        return ErrorReport(
            kind=type(exc).__name__,
            message=exc.message,
            endpoint=exc.endpoint or endpoint,
            native_key=native_key,
            operation=operation,
            retryable=type(exc).retryable,
            fatal=fatal,
        )

    def _record_error(
        self, errors: list[ErrorReport], exc: ConnectorError, native_key: str, operation: str
    ) -> None:
        errors.append(
            self._error_report(
                exc, operation=operation, endpoint=self._target.name, native_key=native_key
            )
        )
        self._count_error(exc.endpoint or self._target.name)

    def _increment(self, name: str, **labels: str) -> None:
        if self._metrics is not None:
            self._metrics.increment(name, value=1.0, **labels)

    def _observe(self, name: str, value: float, **labels: str) -> None:
        if self._metrics is not None:
            self._metrics.observe(name, value, **labels)

    def _count_read(self, entity_type: EntityType) -> None:
        self._increment(
            METRIC_READS_TOTAL,
            pair=self._pair.name,
            endpoint=self._source.name,
            entity_type=entity_type.value,
        )

    def _count_planned(self, entity_type: EntityType) -> None:
        self._increment(
            METRIC_WRITES_PLANNED_TOTAL, pair=self._pair.name, entity_type=entity_type.value
        )

    def _count_applied(self, entity_type: EntityType) -> None:
        self._increment(
            METRIC_WRITES_APPLIED_TOTAL, pair=self._pair.name, entity_type=entity_type.value
        )

    def _count_skip(self, entity_type: EntityType) -> None:
        self._increment(METRIC_SKIPS_TOTAL, pair=self._pair.name, entity_type=entity_type.value)

    def _count_rate_limited(self, endpoint: str) -> None:
        self._increment(METRIC_RATE_LIMITED_TOTAL, endpoint=endpoint)

    def _count_error(self, endpoint: str | None) -> None:
        self._increment(
            METRIC_ERRORS_TOTAL, pair=self._pair.name, endpoint=endpoint or self._source.name
        )

    def _mark_healthy(self, endpoint: str) -> None:
        if self._health is not None:
            self._health.mark_healthy(endpoint)

    def _mark_degraded(self, endpoint: str, reason: str) -> None:
        if self._health is not None:
            self._health.mark_degraded(endpoint, reason=reason)
