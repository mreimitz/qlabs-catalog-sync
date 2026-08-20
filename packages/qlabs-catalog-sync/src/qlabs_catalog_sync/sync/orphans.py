"""Orphan lifecycle: confirm, resolve, and report source objects that vanished.

WP2 / T2.9. Decision D4 (never delete): the engine records every disappearance of a
previously synced source object and never removes or deactivates the corresponding
Qlik object. T2.4's ``sync.loop`` already does the immediate, unambiguous half of
this: a ``ChangeKind.DELETED`` change from ``list_changed``, or a ``NotFound`` on
``read``, becomes an orphan the same cycle (``SyncLoop._orphan``), through
``UnitOfWork.record_orphan``/``resolve_orphan``. That is not re-implemented here. This
module is everything on top of it:

* :func:`recheck_open_orphans` -- independently reconfirms every currently open orphan
  against the source, outside any ``list_changed`` page. This is the orphan
  *lifecycle*: a lone miss stays :attr:`OrphanConfidence.TENTATIVE` (see
  :class:`OrphanPolicy`) until a second, independent check finds the object still
  gone, and a reappearance -- a schema recreated, a transient permissions glitch that
  has since cleared, a paging bug that dropped it exactly once -- is resolved rather
  than left as a permanent orphan.
* :func:`reconcile_bindings` -- the general answer to "an object stopped appearing in
  ``list_changed`` and nothing raised". ``run_cycle`` only reacts to what one delta
  listing hands it; an object whose connector silently drops it from that listing
  forever (rather than deriving ``DELETED`` from its own snapshot the way
  ``qlabs_connector_databricks.changes`` does -- see that module's docstring) is never
  mentioned to the loop again. This checks a caller-supplied set of source-side
  identity bindings directly, by the same authoritative ``read`` the loop's own
  ``NotFound`` path already trusts, independent of any listing.
* :class:`OrphanPolicy` / :func:`summarize_orphans` -- turn the state store's raw,
  per-row ``orphan_log`` records into an operator-facing digest: which orphans are
  worth acting on, how long each has been missing, and what D4's guarantee ("nothing
  here removes a Qlik object") means the operator should actually do about it. "3
  orphans" is not actionable; a confidence level, an age, and a recommended next step
  are.

Every function in this module only ever calls :meth:`Connector.read`: never
``create``, ``update`` or ``delete``, on either the source or the target. Decision D4
holds by construction here, the same way it does in ``sync.loop`` -- there is no
branch in this module that could reach a write, let alone a delete.

Plan, then commit
------------------

:func:`reconcile_bindings` follows the same shape ``run_cycle`` does: every network
call (``source.read``, one per binding) happens first; the state-store writes this
pass decides on then land together in one :meth:`StateStore.unit_of_work`, so a
failure partway through reconciling a large endpoint commits nothing rather than half
of it.

A single miss is not a deletion
--------------------------------

``read`` can fail for reasons that have nothing to do with the object being gone --
a permissions error a connector mis-typed as ``NotFound``, a paging bug, an endpoint
having a bad moment. :class:`OrphanPolicy` defaults to requiring the *state store* to
have observed a binding missing on two independent checks (``first_missing_at !=
last_missing_at``) before calling it :attr:`OrphanConfidence.CONFIRMED` rather than
:attr:`OrphanConfidence.TENTATIVE`. This needs no new persisted counter --
``orphan_log`` already carries both timestamps, and a second miss is exactly what
advances ``last_missing_at`` past ``first_missing_at``
(``UnitOfWork.record_orphan``'s own documented behavior). Separately, any *other*
typed :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError`
:func:`reconcile_bindings` sees from ``read`` -- a ``TransientError``, an
``AuthError``, ... -- is never treated as a miss at all: it is reported as
:class:`InconclusiveCheck`, because a failed check is not evidence of anything.

Wiring this still needs
------------------------

Not built here -- the scheduler and ``state/`` are outside this task's owned paths:

* :func:`recheck_open_orphans` needs nothing beyond what :class:`StateStore` already
  exposes (``list_orphans``, ``get_binding``) and can be called today -- e.g. on its
  own cadence from the scheduler (T2.6, not yet built), alongside
  ``SyncLoop.run_cycle``.
* :func:`reconcile_bindings` needs its ``bindings`` argument from "every
  identity_map row for one endpoint", which nothing in this package currently
  exposes -- :class:`StateStore` only has point lookups (``find_binding`` by native
  key, ``get_binding`` by neutral id), not a bulk listing. A
  ``StateStore.list_bindings(endpoint, entity_type)`` read method needs to be added to
  ``state/store.py`` before this can run unattended over a whole endpoint; until then
  it is exercised directly (see ``tests/orphans/``) and by
  :func:`recheck_open_orphans`, whose bindings come from ``list_orphans`` instead of a
  bulk scan.
* Neither function wraps its own exceptions the way ``run_cycle`` wraps a whole cycle
  for the scheduler's sake -- an unclassified exception from ``source.read`` (or a bug
  here) propagates to the caller. Whatever eventually schedules these should guard
  each call the way the scheduler will guard ``run_cycle``, so one endpoint's failure
  does not take down reconciliation for every other pair.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from qlabs_catalog_sync.observability import bind_sync_context, get_logger
from qlabs_catalog_sync.state.store import IdentityBinding, OrphanRecord, StateStore
from qlabs_catalog_sync.sync.loop import OrphanReport
from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.exceptions import ConnectorError, NotFound
from qlabs_catalog_sync_sdk.models import EntityType

__all__ = [
    "InconclusiveCheck",
    "OrphanAdvisory",
    "OrphanConfidence",
    "OrphanDigest",
    "OrphanPolicy",
    "ReconciliationReport",
    "reconcile_bindings",
    "recheck_open_orphans",
    "summarize_orphans",
]

_LOG = get_logger("qlabs.catalog_sync.sync.orphans")


# ==========================================================================================
# Confidence policy
# ==========================================================================================


class OrphanConfidence(StrEnum):
    """How sure a digest is that an open orphan is a real deletion, not a blip."""

    TENTATIVE = "tentative"
    """Missing on exactly one observation. Could be a stale cache page, a transient
    permissions error a connector surfaced as ``NotFound`` instead of the
    ``TransientError`` it should have been, or a paging bug -- one miss alone does not
    justify telling an operator to go remove something in Qlik by hand."""

    CONFIRMED = "confirmed"
    """Missing on at least two independent observations (``last_missing_at >
    first_missing_at`` on the stored ``orphan_log`` row, under the default policy).
    Worth an operator's attention."""


@dataclass(frozen=True, slots=True)
class OrphanPolicy:
    """How a raw ``orphan_log`` row becomes an operator's confidence and next step.

    v1's default (``require_reconfirmation=True``) treats a lone miss as
    :attr:`OrphanConfidence.TENTATIVE`: D4 always *records* it (nothing here withholds
    that), but a digest only escalates it to :attr:`OrphanConfidence.CONFIRMED` once a
    second, independent check -- whichever of ``run_cycle``,
    :func:`recheck_open_orphans` or :func:`reconcile_bindings` runs next -- finds the
    object still gone. Set ``require_reconfirmation=False`` to treat every miss as
    confirmed immediately, matching how ``run_cycle``'s own D4 path already treats one
    ``NotFound``/``DELETED`` as sufficient to *record* (never to delete -- only ever to
    report).
    """

    require_reconfirmation: bool = True

    def confidence(self, record: OrphanRecord) -> OrphanConfidence:
        """Classify one open orphan row."""
        if self.require_reconfirmation and record.last_missing_at <= record.first_missing_at:
            return OrphanConfidence.TENTATIVE
        return OrphanConfidence.CONFIRMED

    def recommended_action(self, record: OrphanRecord, confidence: OrphanConfidence) -> str:
        """What an operator should actually do -- the engine will never act on its own."""
        if confidence is OrphanConfidence.TENTATIVE:
            return (
                "Seen missing once so far; will be rechecked before this counts as a "
                "confirmed deletion. No action needed yet."
            )
        target = f"native key {record.native_key!r}" if record.native_key else "this object"
        return (
            f"Missing at {record.endpoint!r} since {record.first_missing_at.isoformat()}, "
            "confirmed on a later, independent check. qlabs never deletes or deactivates "
            f"the corresponding Qlik object (decision D4) -- if the deletion at the source "
            f"was intentional, archive or remove {target} in Qlik by hand; if not, check "
            "the source connector's visibility or permissions for it."
        )


# ==========================================================================================
# Operator-facing digest
# ==========================================================================================


@dataclass(frozen=True, slots=True)
class OrphanAdvisory:
    """One open orphan, interpreted for an operator: how sure, how long, what to do."""

    neutral_id: uuid.UUID
    endpoint: str
    entity_type: EntityType
    native_key: str | None
    confidence: OrphanConfidence
    first_missing_at: datetime
    last_missing_at: datetime
    last_seen_at: datetime | None
    missing_for: timedelta
    """How long this object has been missing, as of the digest's ``generated_at``."""

    checked_ago: timedelta
    """How stale the last check is, as of the digest's ``generated_at``."""

    recommended_action: str

    def to_json(self) -> dict[str, Any]:
        """This advisory as plain JSON."""
        return {
            "neutral_id": str(self.neutral_id),
            "endpoint": self.endpoint,
            "entity_type": self.entity_type.value,
            "native_key": self.native_key,
            "confidence": self.confidence.value,
            "first_missing_at": self.first_missing_at.isoformat(),
            "last_missing_at": self.last_missing_at.isoformat(),
            "last_seen_at": None if self.last_seen_at is None else self.last_seen_at.isoformat(),
            "missing_for_seconds": self.missing_for.total_seconds(),
            "checked_ago_seconds": self.checked_ago.total_seconds(),
            "recommended_action": self.recommended_action,
        }


@dataclass(frozen=True, slots=True)
class OrphanDigest:
    """Every open orphan, prioritized -- what a CLI or dashboard renders instead of a
    bare count."""

    generated_at: datetime
    advisories: tuple[OrphanAdvisory, ...]

    @property
    def confirmed(self) -> tuple[OrphanAdvisory, ...]:
        """Advisories worth an operator's attention now."""
        return tuple(a for a in self.advisories if a.confidence is OrphanConfidence.CONFIRMED)

    @property
    def tentative(self) -> tuple[OrphanAdvisory, ...]:
        """Advisories seen missing once, awaiting a second, independent check."""
        return tuple(a for a in self.advisories if a.confidence is OrphanConfidence.TENTATIVE)

    def by_endpoint(self) -> dict[str, tuple[OrphanAdvisory, ...]]:
        """Advisories grouped by the source endpoint they vanished from."""
        grouped: dict[str, list[OrphanAdvisory]] = {}
        for advisory in self.advisories:
            grouped.setdefault(advisory.endpoint, []).append(advisory)
        return {endpoint: tuple(items) for endpoint, items in grouped.items()}

    def headline(self) -> str:
        """A one-line, human summary -- never just a count."""
        if not self.advisories:
            return "No orphans outstanding."
        endpoints = sorted(self.by_endpoint())
        return (
            f"{len(self.advisories)} orphan(s) across {len(endpoints)} endpoint(s) "
            f"({', '.join(endpoints)}): {len(self.confirmed)} confirmed, "
            f"{len(self.tentative)} tentative. qlabs never deletes or deactivates the "
            "corresponding Qlik object -- review confirmed orphans and act on them by hand."
        )

    def to_json(self) -> dict[str, Any]:
        """The whole digest as plain JSON."""
        return {
            "generated_at": self.generated_at.isoformat(),
            "headline": self.headline(),
            "confirmed_count": len(self.confirmed),
            "tentative_count": len(self.tentative),
            "advisories": [advisory.to_json() for advisory in self.advisories],
        }


def summarize_orphans(
    records: Sequence[OrphanRecord],
    *,
    now: datetime,
    policy: OrphanPolicy | None = None,
) -> OrphanDigest:
    """Turn raw ``orphan_log`` rows -- typically ``await store.list_orphans(endpoint)``
    -- into an operator-facing digest.

    Resolved rows are dropped even if the caller passed ``unresolved_only=False``: a
    digest is about what is still missing, not history.
    """
    active_policy = policy if policy is not None else OrphanPolicy()
    advisories = tuple(
        _advise(record, now=now, policy=active_policy)
        for record in records
        if record.resolved_at is None
    )
    return OrphanDigest(generated_at=now, advisories=advisories)


def _advise(record: OrphanRecord, *, now: datetime, policy: OrphanPolicy) -> OrphanAdvisory:
    confidence = policy.confidence(record)
    return OrphanAdvisory(
        neutral_id=record.neutral_id,
        endpoint=record.endpoint,
        entity_type=record.entity_type,
        native_key=record.native_key,
        confidence=confidence,
        first_missing_at=record.first_missing_at,
        last_missing_at=record.last_missing_at,
        last_seen_at=record.last_seen_at,
        missing_for=now - record.first_missing_at,
        checked_ago=now - record.last_missing_at,
        recommended_action=policy.recommended_action(record, confidence),
    )


# ==========================================================================================
# Reconciliation: checking bindings directly against the source
# ==========================================================================================


class _CheckStatus(StrEnum):
    """Internal: what one direct ``read`` against the source found."""

    PRESENT = "present"
    MISSING = "missing"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class _Check:
    status: _CheckStatus
    detail: str | None = None
    last_synced_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class InconclusiveCheck:
    """A binding whose recheck could not confirm presence or absence.

    Never treated as a miss: a ``TransientError``/``AuthError``/... from ``read``
    means the *check* failed, not that the object is gone -- counting it as a miss
    would be exactly the false alarm :class:`OrphanPolicy`'s confirmation step exists
    to avoid.
    """

    neutral_id: uuid.UUID
    endpoint: str
    entity_type: EntityType
    native_key: str
    detail: str | None

    def to_json(self) -> dict[str, Any]:
        """This inconclusive check as plain JSON."""
        return {
            "neutral_id": str(self.neutral_id),
            "endpoint": self.endpoint,
            "entity_type": self.entity_type.value,
            "native_key": self.native_key,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """What one reconciliation pass found -- the return value of
    :func:`reconcile_bindings` (and, through it, :func:`recheck_open_orphans`)."""

    endpoint: str
    checked_at: datetime
    checked: int
    newly_missing: tuple[OrphanReport, ...] = ()
    """Bindings found gone that were not already an open orphan."""

    still_missing: tuple[OrphanReport, ...] = ()
    """Bindings that were already an open orphan and are still gone."""

    resolved: tuple[uuid.UUID, ...] = ()
    """Neutral ids of bindings that were an open orphan and reappeared this pass."""

    inconclusive: tuple[InconclusiveCheck, ...] = ()
    """Bindings whose check itself failed. Not recorded as missing or resolved."""

    @property
    def missing(self) -> tuple[OrphanReport, ...]:
        """Every binding this pass found gone, new or reconfirmed."""
        return self.newly_missing + self.still_missing

    def to_json(self) -> dict[str, Any]:
        """This report as plain JSON."""
        return {
            "endpoint": self.endpoint,
            "checked_at": self.checked_at.isoformat(),
            "checked": self.checked,
            "newly_missing": [orphan.to_json() for orphan in self.newly_missing],
            "still_missing": [orphan.to_json() for orphan in self.still_missing],
            "resolved": [str(neutral_id) for neutral_id in self.resolved],
            "inconclusive": [check.to_json() for check in self.inconclusive],
        }


async def _last_synced_at(
    store: StateStore, neutral_id: uuid.UUID, endpoint: str
) -> datetime | None:
    """The most recent field-envelope ``last_synced_at`` the state store holds, if any.

    A more useful ``last_seen_at`` for a freshly-detected orphan than ``None``: it
    tells the operator when this object was last confirmed present, not just that it
    is gone now.
    """
    envelopes = await store.fetch_envelopes(neutral_id, endpoint)
    synced = [
        envelope.last_synced_at for envelope in envelopes.values() if envelope.last_synced_at
    ]
    return max(synced) if synced else None


async def _check_one(store: StateStore, source: Connector, binding: IdentityBinding) -> _Check:
    """Ask the source, directly, whether ``binding`` still exists. Never a write."""
    identity = binding.identity
    with bind_sync_context(
        endpoint=source.name,
        entity_type=identity.entity_type.value,
        neutral_id=str(binding.neutral_id),
    ):
        try:
            await source.read(identity)
        except NotFound:
            last_synced = await _last_synced_at(store, binding.neutral_id, source.name)
            return _Check(_CheckStatus.MISSING, last_synced_at=last_synced)
        except ConnectorError as exc:
            _LOG.warning(
                "orphans.reconcile.inconclusive",
                native_key=identity.native_key,
                kind=type(exc).__name__,
                detail=str(exc),
            )
            return _Check(_CheckStatus.INCONCLUSIVE, detail=f"{type(exc).__name__}: {exc}")
        return _Check(_CheckStatus.PRESENT)


async def reconcile_bindings(
    store: StateStore,
    source: Connector,
    bindings: Sequence[IdentityBinding],
    *,
    now: datetime,
) -> ReconciliationReport:
    """Directly confirm every one of ``bindings`` against ``source`` and record the
    result.

    This is the general answer to "an object stopped appearing in ``list_changed`` and
    nothing raised" (see the module docstring): it does not consult ``list_changed`` at
    all, so it catches whatever a connector's delta listing might silently drop.

    Every binding must belong to ``source`` (``binding.identity.endpoint ==
    source.name``); mixing endpoints in one call is a caller bug, not something this
    function can make sense of, and raises ``ValueError``.

    Calls only ``source.read`` -- never ``create``, ``update`` or ``delete`` on any
    connector.
    """
    if not bindings:
        return ReconciliationReport(endpoint=source.name, checked_at=now, checked=0)

    for binding in bindings:
        if binding.identity.endpoint != source.name:
            raise ValueError(
                f"binding for {binding.identity.native_key!r} belongs to endpoint "
                f"{binding.identity.endpoint!r}, not {source.name!r}"
            )

    # Plan phase: every network call up front, nothing persisted yet.
    already_open = {
        record.neutral_id: record
        for record in await store.list_orphans(source.name, unresolved_only=True)
    }
    checks = [(binding, await _check_one(store, source, binding)) for binding in bindings]

    newly_missing: list[OrphanReport] = []
    still_missing: list[OrphanReport] = []
    resolved: list[uuid.UUID] = []
    inconclusive: list[InconclusiveCheck] = []

    # Commit phase: one transaction for everything this pass decided.
    async with store.unit_of_work() as uow:
        for binding, check in checks:
            identity = binding.identity
            if check.status is _CheckStatus.MISSING:
                await uow.record_orphan(
                    binding.neutral_id,
                    source.name,
                    identity.entity_type,
                    native_key=identity.native_key,
                    last_seen_at=check.last_synced_at,
                    observed_at=now,
                )
                report = OrphanReport(
                    neutral_id=binding.neutral_id,
                    entity_type=identity.entity_type,
                    endpoint=source.name,
                    native_key=identity.native_key,
                    observed_at=now,
                )
                if binding.neutral_id in already_open:
                    still_missing.append(report)
                    _LOG.info("orphans.reconcile.still_missing", native_key=identity.native_key)
                else:
                    newly_missing.append(report)
                    _LOG.info("orphans.reconcile.newly_missing", native_key=identity.native_key)
            elif check.status is _CheckStatus.PRESENT:
                if binding.neutral_id in already_open:
                    await uow.resolve_orphan(
                        binding.neutral_id, source.name, identity.entity_type, now=now
                    )
                    resolved.append(binding.neutral_id)
                    _LOG.info("orphans.reconcile.resolved", native_key=identity.native_key)
            else:
                inconclusive.append(
                    InconclusiveCheck(
                        neutral_id=binding.neutral_id,
                        endpoint=source.name,
                        entity_type=identity.entity_type,
                        native_key=identity.native_key,
                        detail=check.detail,
                    )
                )

    return ReconciliationReport(
        endpoint=source.name,
        checked_at=now,
        checked=len(bindings),
        newly_missing=tuple(newly_missing),
        still_missing=tuple(still_missing),
        resolved=tuple(resolved),
        inconclusive=tuple(inconclusive),
    )


async def recheck_open_orphans(
    store: StateStore,
    source: Connector,
    *,
    entity_type: EntityType,
    now: datetime,
) -> ReconciliationReport:
    """Independently reconfirm every open orphan for ``entity_type`` at ``source``.

    The lifecycle half of D4: an orphan ``run_cycle`` already flagged sits open until
    something checks whether it is still gone. This is that check, run outside any
    ``list_changed`` page, so a reappearance -- a schema recreated, a transient
    permissions glitch that has since cleared, a paging bug that dropped the object
    exactly once -- is caught and resolved instead of the entity staying a permanent,
    stale orphan.

    Needs nothing :class:`StateStore` does not already expose: the bindings to recheck
    come from ``list_orphans``, not a bulk endpoint scan.
    """
    orphans = await store.list_orphans(source.name, unresolved_only=True)
    relevant = [orphan for orphan in orphans if orphan.entity_type is entity_type]

    bindings: list[IdentityBinding] = []
    for orphan in relevant:
        binding = await store.get_binding(orphan.neutral_id, source.name, entity_type)
        if binding is None:
            # Should not happen -- an orphan_log row always comes from a binding that
            # existed when it was recorded -- but a binding this function cannot find
            # is a binding it must not silently skip past without a trace.
            _LOG.warning(
                "orphans.recheck.binding_missing",
                neutral_id=str(orphan.neutral_id),
                native_key=orphan.native_key,
            )
            continue
        bindings.append(binding)

    return await reconcile_bindings(store, source, bindings, now=now)
