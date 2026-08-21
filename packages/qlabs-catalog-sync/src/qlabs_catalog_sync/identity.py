"""Identity resolution: the IdentityMap over the state store, plus bootstrap matching.

WP7 / T7.1. Two mechanisms, deliberately kept apart because only one of them is safe to
run unattended:

* **The stored map is the only matcher.** :meth:`IdentityResolver.resolve` looks a
  ``neutralId`` up by ``(endpoint, entity_type, tenant_id, native_key)`` and by nothing
  else. Once an entity is bound, a rename on either side changes no lookup and triggers
  no re-matching — the map is consulted, names are not (RS-03 section 4: "thereafter
  match by the stored IdentityMap only").
* **Bootstrap matching is a proposal generator, never a binder.**
  :meth:`IdentityResolver.bootstrap` matches source objects against *existing* target
  objects by natural key (name + type + parent path), writes what it found to a review
  file, and **performs zero database writes**. A binding appears in ``identity_map``
  only when a human puts it there through :meth:`IdentityResolver.confirm` or
  :meth:`IdentityResolver.apply_review_file`.

The resulting invariant, which the tests assert directly: *no sync run can create a
binding to a pre-existing target object.* Proposals live in a file, not in the database,
so there is no row for a run to promote and no flag for a run to flip.

Why a UC schema's parent path is its catalog: D1 of the RM-01 MVP decision makes
``catalog.schema`` the data product, so the natural key of that data product is
``name="schema"``, ``parent_path=("catalog",)``.

## Natural-key comparison rules

A natural key compares as ``(entity_type, name, parent_path)``. ``entity_type`` is an
enum and compares exactly. ``name`` and each ``parent_path`` segment are compared after:

1. **Unicode NFC normalization** — canonical equivalence only. ``é`` written as one code
   point and as ``e`` + combining acute are the *same character* by Unicode's own
   definition, so treating them as different names would be a bug. NFKC is deliberately
   *not* used: it also folds compatibility characters (full-width forms, roman numerals,
   superscripts), which are different characters that merely look alike, and folding them
   would be a guess about typography rather than a fact about the string. (See step 3 for
   the narrow set of compatibility characters case folding does fold on its own.)
2. **Leading/trailing whitespace stripped** — trailing whitespace in a catalog display
   name is invisible to the human doing the review and is almost always a paste artifact.
   Whitespace *inside* a name is left exactly as written: ``"Sales  Orders"`` does not
   match ``"Sales Orders"``. Collapsing internal runs would be a typography guess too.
3. **Case folded** — Databricks object names are case-insensitive for resolution
   (``main.Sales`` and ``main.sales`` are one schema), so a case-sensitive comparison
   would miss real matches. Note this cuts the other way as well and that is intended:
   two Qlik products named ``Sales`` and ``sales`` become an *ambiguous* proposal, not a
   silent pick of the exactly-cased one. Case is not evidence, and an exact-case
   tie-break is precisely the coin flip this module refuses to make. (Case folding
   applies to *names*. Databricks tag **keys** are case-sensitive and are not part of a
   natural key; nothing here touches them.) ``str.casefold`` is Unicode *full* case
   folding, so it also folds the handful of cased compatibility characters whose folding
   is defined as a case mapping — notably the ``ﬁ`` ligature, which folds to ``fi``. That
   is part of case folding, not the compatibility pass NFKC would add: full-width forms,
   roman numerals and superscripts stay distinct.

Empty parent-path segments are dropped, so ``()`` and ``("",)`` are the same path. A name
that normalizes to the empty string never matches anything — it carries no evidence.

Matching is boolean, not scored: a candidate either has the same comparable natural key
or it does not. That is what makes "two equally good candidates" a structural fact rather
than a threshold, and why an ambiguous proposal can never collapse into a lucky winner.

## What is never a candidate

A target object that is already bound to some other neutral id is excluded from the
candidate set and named in the proposal's rationale. Offering it would only manufacture a
conflict at confirmation time, and the human needs to see *why* an obvious name match was
not offered.

## Concurrency and races

Confirmation pre-checks the map with point reads and then binds inside one
:meth:`~qlabs_catalog_sync.state.store.StateStore.unit_of_work`. If reality changes
between the check and the write, the state store's
``uq_identity_map_endpoint_entity_type_tenant_id_native_key`` constraint rejects the
write, the transaction rolls back whole, and the ``IntegrityError`` surfaces as a
:class:`~qlabs_catalog_sync_sdk.exceptions.ConflictError`. The pre-checks buy a good error
message; the unique constraint plus the single transaction is what actually guarantees the
map cannot be corrupted.

This module owns no schema. It reads and writes ``identity_map`` exclusively through
:class:`~qlabs_catalog_sync.state.store.StateStore` (plus two read-only enumeration
queries the store's point-read surface does not offer), and adds no migration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import unicodedata
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TypeVar

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qlabs_catalog_sync.state.models import IdentityMapRow
from qlabs_catalog_sync.state.store import IdentityBinding, StateStore
from qlabs_catalog_sync_sdk.exceptions import ConflictError, ConnectorError, NotFound
from qlabs_catalog_sync_sdk.models import EntityType, IdentityRef

__all__ = [
    "REVIEW_FILE_KIND",
    "REVIEW_FILE_VERSION",
    "ApplyReport",
    "BootstrapReport",
    "CatalogObject",
    "ConfirmationOutcome",
    "ConfirmationResult",
    "IdentityResolver",
    "IdentityReview",
    "MatchProposal",
    "NaturalKey",
    "ParentPathRule",
    "ProposalStatus",
    "ReviewDecision",
    "normalize_component",
]

REVIEW_FILE_VERSION: Final = 1
"""Schema version of the identity review file. Bumped only on a breaking shape change."""

REVIEW_FILE_KIND: Final = "qlabs-catalog-sync/identity-review"
"""Discriminator written into the review file so a wrong file is rejected, not parsed."""

_REVIEW_INSTRUCTIONS: Final = (
    "This file is the identity bootstrap review queue. Nothing in it is bound until a "
    "human confirms it.",
    'To accept a proposal: set its "decision" to "confirm". For an "ambiguous" proposal '
    'you must also set "chosen_native_key" to exactly one of its candidates.',
    'To refuse a proposal for good: set its "decision" to "reject". Re-running bootstrap '
    "will not propose it again while the candidates are unchanged.",
    'Only "decision", "chosen_native_key" and "note" are yours to edit; every other field '
    "is regenerated by the next bootstrap run.",
    "Apply the file with the CLI's identity-confirm command. If the catalogs changed "
    'since this file was written, the affected decision is reset to "pending" and the old '
    'one is recorded in "superseded_decision" rather than applied to different objects.',
)

_LOG = structlog.get_logger("qlabs.catalog_sync.identity")

_EnumT = TypeVar("_EnumT", bound=StrEnum)


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class ParentPathRule(StrEnum):
    """How the parent path participates in natural-key comparison."""

    EXACT = "exact"
    """Parent paths must be equal after normalization. The default, and the honest rule
    whenever both sides express containment in the same vocabulary (Databricks catalog
    to Databricks catalog, Qlik space to Qlik space)."""

    IGNORE = "ignore"
    """Compare on ``entity_type`` + ``name`` only.

    Use this — and only this — when the candidate list is already scoped to exactly one
    target container (for example every data product in one Qlik space). Inside a single
    container the parent path is constant, so it discriminates nothing, while requiring a
    Databricks *catalog* name to equal a Qlik *space* name would reject every true match.
    Ambiguity detection is unaffected: two candidates with the same name in that container
    are still an ambiguous proposal, never a pick."""


class ProposalStatus(StrEnum):
    """What bootstrap found for one source object. Never a confidence score."""

    PROPOSED = "proposed"
    """Exactly one unbound target candidate shares the natural key."""

    AMBIGUOUS = "ambiguous"
    """Two or more unbound target candidates share the natural key equally. A human picks;
    this module will not."""

    UNMATCHED = "unmatched"
    """No unbound target candidate shares the natural key."""

    BOUND = "bound"
    """The source object already resolves to a target binding through the stored map. Kept
    in the file as a record; bootstrap never re-matches it by name."""


class ReviewDecision(StrEnum):
    """The human's verdict on a proposal. Only a human ever sets anything but ``PENDING``."""

    PENDING = "pending"
    CONFIRM = "confirm"
    REJECT = "reject"


class ConfirmationOutcome(StrEnum):
    """What actually happened when a decision was applied."""

    BOUND = "bound"
    """A new binding was written."""

    ALREADY_BOUND = "already_bound"
    """The binding this decision asks for is exactly the one already stored — confirming
    twice is a no-op, not a rebind and not an error."""

    REJECTED = "rejected"
    """Recorded as refused; nothing was bound."""

    SKIPPED = "skipped"
    """Nothing to do (still pending, or already applied)."""

    FAILED = "failed"
    """The decision contradicted the stored map or the recorded candidates. Nothing was
    written. Only :meth:`IdentityResolver.apply_review_file` produces this; the targeted
    :meth:`IdentityResolver.confirm` raises instead."""


# --------------------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------------------


def normalize_component(value: str) -> str:
    """Normalize one natural-key component (a name or a parent-path segment).

    NFC, strip, casefold — see the module docstring for why each step is there and why
    NFKC and internal-whitespace collapsing are not.
    """
    return unicodedata.normalize("NFC", value).strip().casefold()


@dataclass(frozen=True, slots=True)
class NaturalKey:
    """A human-meaningful key: name + entity type + parent path.

    The bootstrap mechanism's *only* evidence, and never used again once a binding
    exists. ``parent_path`` is ordered outermost-first — a Unity Catalog schema
    ``main.sales`` as a data product is ``NaturalKey("sales", DATA_PRODUCT, ("main",))``
    per D1.
    """

    name: str
    entity_type: EntityType
    parent_path: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        """The key as a person reads it, e.g. ``main/sales``. Raw, never normalized."""
        parent = "/".join(self.parent_path)
        return f"{parent}/{self.name}" if parent else self.name

    def comparable(self, rule: ParentPathRule) -> tuple[str, str, tuple[str, ...]]:
        """The normalized tuple two natural keys are compared on."""
        name = normalize_component(self.name)
        if rule is ParentPathRule.IGNORE:
            return (self.entity_type.value, name, ())
        segments = tuple(
            normalized
            for normalized in (normalize_component(part) for part in self.parent_path)
            if normalized
        )
        return (self.entity_type.value, name, segments)


@dataclass(frozen=True, slots=True)
class CatalogObject:
    """One object at one endpoint: its native identity plus its natural key.

    Used for both sides of a bootstrap run. The caller (the sync loop, or the CLI) builds
    these from what a connector read; this module never talks to a connector.
    """

    identity: IdentityRef
    natural_key: NaturalKey

    def __post_init__(self) -> None:
        if self.identity.entity_type is not self.natural_key.entity_type:
            raise ValueError(
                "CatalogObject entity types disagree: identity says "
                f"{self.identity.entity_type!r}, natural key says "
                f"{self.natural_key.entity_type!r}"
            )

    @property
    def key(self) -> str:
        """Stable, human-readable id for this object at its endpoint.

        ``endpoint|entity_type|tenant_id|native_key``. The first three fields never
        contain ``|`` (``entity_type`` is an enum; ``endpoint`` and ``tenant_id`` are
        configured identifiers), so the composition is unambiguous even if a native key
        happens to contain one.
        """
        return (
            f"{self.identity.endpoint}|{self.identity.entity_type.value}"
            f"|{self.identity.tenant_id}|{self.identity.native_key}"
        )


@dataclass(frozen=True, slots=True)
class MatchProposal:
    """One source object's bootstrap result, and the human's verdict on it.

    A proposal is a *record*, not a binding. ``applied_at``/``applied_neutral_id`` are the
    only fields that mean something was written to the map, and only
    :meth:`IdentityResolver.confirm` sets them.
    """

    proposal_id: str
    status: ProposalStatus
    decision: ReviewDecision
    source: CatalogObject
    target_endpoint: str
    target_tenant_id: str
    candidates: tuple[CatalogObject, ...]
    candidates_fingerprint: str
    rationale: str
    first_proposed_at: datetime
    last_seen_at: datetime
    chosen_native_key: str | None = None
    excluded_bound_candidates: tuple[str, ...] = ()
    decided_at: datetime | None = None
    applied_at: datetime | None = None
    applied_neutral_id: uuid.UUID | None = None
    superseded_decision: str | None = None
    note: str | None = None

    @property
    def needs_human(self) -> bool:
        """True while a person still has to look at this entry."""
        return self.decision is ReviewDecision.PENDING and self.status in (
            ProposalStatus.PROPOSED,
            ProposalStatus.AMBIGUOUS,
        )

    def candidate(self, native_key: str) -> CatalogObject | None:
        """The candidate with this exact native key, if it is one of this proposal's."""
        for candidate in self.candidates:
            if candidate.identity.native_key == native_key:
                return candidate
        return None


@dataclass(frozen=True, slots=True)
class IdentityReview:
    """The parsed review file."""

    version: int
    generated_at: datetime
    proposals: tuple[MatchProposal, ...]

    def get(self, proposal_id: str) -> MatchProposal | None:
        """The proposal with this id, if present."""
        for proposal in self.proposals:
            if proposal.proposal_id == proposal_id:
                return proposal
        return None

    @property
    def pending(self) -> tuple[MatchProposal, ...]:
        """Every proposal still waiting on a person."""
        return tuple(proposal for proposal in self.proposals if proposal.needs_human)


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """What one :meth:`IdentityResolver.bootstrap` run found. Nothing here was bound."""

    review_path: Path
    proposed: tuple[MatchProposal, ...]
    ambiguous: tuple[MatchProposal, ...]
    unmatched: tuple[MatchProposal, ...]
    already_bound: tuple[MatchProposal, ...]
    superseded: tuple[str, ...]

    @property
    def considered(self) -> int:
        """How many source objects this run looked at."""
        return (
            len(self.proposed) + len(self.ambiguous) + len(self.unmatched) + len(self.already_bound)
        )

    @property
    def needs_human(self) -> tuple[MatchProposal, ...]:
        """Everything a person must act on, proposals and ambiguities together."""
        return tuple(
            proposal
            for proposal in (*self.proposed, *self.ambiguous)
            if proposal.decision is ReviewDecision.PENDING
        )


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    """The outcome of applying one decision."""

    proposal_id: str
    outcome: ConfirmationOutcome
    detail: str
    neutral_id: uuid.UUID | None = None
    target_native_key: str | None = None


@dataclass(frozen=True, slots=True)
class ApplyReport:
    """The outcome of applying every confirmed decision in the review file."""

    review_path: Path
    results: tuple[ConfirmationResult, ...]

    @property
    def bound(self) -> tuple[ConfirmationResult, ...]:
        """Decisions that produced a new binding."""
        return tuple(r for r in self.results if r.outcome is ConfirmationOutcome.BOUND)

    @property
    def failed(self) -> tuple[ConfirmationResult, ...]:
        """Decisions that contradicted reality and were not applied."""
        return tuple(r for r in self.results if r.outcome is ConfirmationOutcome.FAILED)

    @property
    def ok(self) -> bool:
        """True when nothing failed."""
        return not self.failed


# --------------------------------------------------------------------------------------
# Fingerprinting and JSON codec
# --------------------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _fingerprint(candidates: Sequence[CatalogObject], rule: ParentPathRule) -> str:
    """A stable digest of the candidate set, order-independent.

    Covers each candidate's endpoint, tenant, type, native key and *normalized* natural
    key. Two runs that see the same objects produce the same fingerprint even if the
    connector listed them in a different order; a run that sees a renamed, added or
    removed candidate does not. That is what lets a stale human decision be detected
    instead of applied to different objects.
    """
    lines = sorted(
        json.dumps(
            [
                candidate.identity.endpoint,
                candidate.identity.tenant_id,
                candidate.identity.entity_type.value,
                candidate.identity.native_key,
                candidate.natural_key.comparable(rule)[1],
                list(candidate.natural_key.comparable(rule)[2]),
            ],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for candidate in candidates
    )
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _dt_to_json(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _dt_from_json(raw: object, where: str) -> datetime:
    text = _expect_str(raw, where)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConnectorError(f"identity review file: {where} is not an RFC3339 timestamp") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _expect_mapping(raw: object, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConnectorError(f"identity review file: {where} must be an object")
    return {str(key): value for key, value in raw.items()}


def _expect_list(raw: object, where: str) -> list[Any]:
    if not isinstance(raw, list):
        raise ConnectorError(f"identity review file: {where} must be an array")
    return list(raw)


def _expect_str(raw: object, where: str) -> str:
    if not isinstance(raw, str):
        raise ConnectorError(f"identity review file: {where} must be a string")
    return raw


def _optional_str(raw: object, where: str) -> str | None:
    if raw is None:
        return None
    return _expect_str(raw, where)


def _enum_from_json(enum_cls: type[_EnumT], raw: object, where: str) -> _EnumT:
    text = _expect_str(raw, where)
    try:
        return enum_cls(text)
    except ValueError as exc:
        allowed = ", ".join(sorted(member.value for member in enum_cls))
        raise ConnectorError(
            f"identity review file: {where} is {text!r}; allowed values are {allowed}"
        ) from exc


def _object_to_json(obj: CatalogObject) -> dict[str, Any]:
    return {
        "endpoint": obj.identity.endpoint,
        "tenant_id": obj.identity.tenant_id,
        "entity_type": obj.identity.entity_type.value,
        "native_key": obj.identity.native_key,
        "name": obj.natural_key.name,
        "parent_path": list(obj.natural_key.parent_path),
        "secondary_keys": dict(obj.identity.secondary_keys),
    }


def _object_from_json(raw: object, where: str) -> CatalogObject:
    data = _expect_mapping(raw, where)
    entity_type = _enum_from_json(EntityType, data.get("entity_type"), f"{where}.entity_type")
    secondary = _expect_mapping(data.get("secondary_keys", {}), f"{where}.secondary_keys")
    parent_path = tuple(
        _expect_str(part, f"{where}.parent_path[]")
        for part in _expect_list(data.get("parent_path", []), f"{where}.parent_path")
    )
    identity = IdentityRef(
        endpoint=_expect_str(data.get("endpoint"), f"{where}.endpoint"),
        entity_type=entity_type,
        native_key=_expect_str(data.get("native_key"), f"{where}.native_key"),
        tenant_id=_expect_str(data.get("tenant_id"), f"{where}.tenant_id"),
        secondary_keys={
            key: _expect_str(value, f"{where}.secondary_keys.{key}")
            for key, value in secondary.items()
        },
    )
    natural_key = NaturalKey(
        name=_expect_str(data.get("name"), f"{where}.name"),
        entity_type=entity_type,
        parent_path=parent_path,
    )
    return CatalogObject(identity=identity, natural_key=natural_key)


def _proposal_to_json(proposal: MatchProposal) -> dict[str, Any]:
    """Serialize one proposal. Key order is chosen for reading, not for the machine."""
    return {
        "proposal_id": proposal.proposal_id,
        "status": proposal.status.value,
        "decision": proposal.decision.value,
        "chosen_native_key": proposal.chosen_native_key,
        "note": proposal.note,
        "rationale": proposal.rationale,
        "source": _object_to_json(proposal.source),
        "target_endpoint": proposal.target_endpoint,
        "target_tenant_id": proposal.target_tenant_id,
        "candidates": [_object_to_json(candidate) for candidate in proposal.candidates],
        "excluded_bound_candidates": list(proposal.excluded_bound_candidates),
        "candidates_fingerprint": proposal.candidates_fingerprint,
        "first_proposed_at": _dt_to_json(proposal.first_proposed_at),
        "last_seen_at": _dt_to_json(proposal.last_seen_at),
        "decided_at": None if proposal.decided_at is None else _dt_to_json(proposal.decided_at),
        "applied_at": None if proposal.applied_at is None else _dt_to_json(proposal.applied_at),
        "applied_neutral_id": (
            None if proposal.applied_neutral_id is None else str(proposal.applied_neutral_id)
        ),
        "superseded_decision": proposal.superseded_decision,
    }


def _proposal_from_json(raw: object, index: int) -> MatchProposal:
    where = f"proposals[{index}]"
    data = _expect_mapping(raw, where)
    applied_neutral_id = _optional_str(
        data.get("applied_neutral_id"), f"{where}.applied_neutral_id"
    )
    try:
        neutral_id = None if applied_neutral_id is None else uuid.UUID(applied_neutral_id)
    except ValueError as exc:
        raise ConnectorError(
            f"identity review file: {where}.applied_neutral_id is not a UUID"
        ) from exc
    decided_at = data.get("decided_at")
    applied_at = data.get("applied_at")
    return MatchProposal(
        proposal_id=_expect_str(data.get("proposal_id"), f"{where}.proposal_id"),
        status=_enum_from_json(ProposalStatus, data.get("status"), f"{where}.status"),
        decision=_enum_from_json(ReviewDecision, data.get("decision"), f"{where}.decision"),
        source=_object_from_json(data.get("source"), f"{where}.source"),
        target_endpoint=_expect_str(data.get("target_endpoint"), f"{where}.target_endpoint"),
        target_tenant_id=_expect_str(data.get("target_tenant_id"), f"{where}.target_tenant_id"),
        candidates=tuple(
            _object_from_json(candidate, f"{where}.candidates[{position}]")
            for position, candidate in enumerate(
                _expect_list(data.get("candidates", []), f"{where}.candidates")
            )
        ),
        candidates_fingerprint=_expect_str(
            data.get("candidates_fingerprint"), f"{where}.candidates_fingerprint"
        ),
        rationale=_expect_str(data.get("rationale", ""), f"{where}.rationale"),
        first_proposed_at=_dt_from_json(
            data.get("first_proposed_at"), f"{where}.first_proposed_at"
        ),
        last_seen_at=_dt_from_json(data.get("last_seen_at"), f"{where}.last_seen_at"),
        chosen_native_key=_optional_str(
            data.get("chosen_native_key"), f"{where}.chosen_native_key"
        ),
        excluded_bound_candidates=tuple(
            _expect_str(key, f"{where}.excluded_bound_candidates[]")
            for key in _expect_list(
                data.get("excluded_bound_candidates", []), f"{where}.excluded_bound_candidates"
            )
        ),
        decided_at=None if decided_at is None else _dt_from_json(decided_at, f"{where}.decided_at"),
        applied_at=None if applied_at is None else _dt_from_json(applied_at, f"{where}.applied_at"),
        applied_neutral_id=neutral_id,
        superseded_decision=_optional_str(
            data.get("superseded_decision"), f"{where}.superseded_decision"
        ),
        note=_optional_str(data.get("note"), f"{where}.note"),
    )


def _review_to_json(review: IdentityReview) -> dict[str, Any]:
    return {
        "kind": REVIEW_FILE_KIND,
        "version": review.version,
        "generated_at": _dt_to_json(review.generated_at),
        "instructions": list(_REVIEW_INSTRUCTIONS),
        "proposals": [
            _proposal_to_json(proposal)
            for proposal in sorted(review.proposals, key=lambda p: p.proposal_id)
        ],
    }


def _review_from_json(raw: object) -> IdentityReview:
    data = _expect_mapping(raw, "<root>")
    kind = data.get("kind")
    if kind != REVIEW_FILE_KIND:
        raise ConnectorError(
            f"identity review file: kind is {kind!r}, expected {REVIEW_FILE_KIND!r} — "
            "this does not look like an identity review file"
        )
    version = data.get("version")
    if version != REVIEW_FILE_VERSION:
        raise ConnectorError(
            f"identity review file: version is {version!r}, this build reads "
            f"version {REVIEW_FILE_VERSION}"
        )
    proposals = tuple(
        _proposal_from_json(proposal, index)
        for index, proposal in enumerate(_expect_list(data.get("proposals", []), "proposals"))
    )
    seen: set[str] = set()
    for proposal in proposals:
        if proposal.proposal_id in seen:
            raise ConnectorError(
                f"identity review file: duplicate proposal_id {proposal.proposal_id!r}"
            )
        seen.add(proposal.proposal_id)
    return IdentityReview(
        version=REVIEW_FILE_VERSION,
        generated_at=_dt_from_json(data.get("generated_at"), "generated_at"),
        proposals=proposals,
    )


# --------------------------------------------------------------------------------------
# The resolver
# --------------------------------------------------------------------------------------


class IdentityResolver:
    """Identity resolution over the state store, plus the bootstrap review workflow.

    ``review_path`` is a plain parameter, not config: this module must stay usable from
    the sync loop, the CLI and a test without any of them agreeing on a config schema.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        review_path: Path,
        parent_path_rule: ParentPathRule = ParentPathRule.EXACT,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._review_path = Path(review_path)
        self._parent_path_rule = parent_path_rule
        self._clock = clock

    @property
    def review_path(self) -> Path:
        """Where the review file lives."""
        return self._review_path

    # -- resolution through the stored map only --------------------------------------

    async def resolve(self, identity: IdentityRef) -> IdentityBinding | None:
        """The binding for this native key, or ``None`` when the map has never seen it.

        Looks up ``(endpoint, entity_type, tenant_id, native_key)``. Nothing about names
        enters this path, so a rename that preserves the native key resolves unchanged and
        a rename that changes it is simply an unknown key — never a silent rebind.

        Tenant scoping is not advisory: ``tenant_id`` is part of the lookup, so a native
        key from one tenant can never resolve to another tenant's binding.
        """
        return await self._store.find_binding(
            endpoint=identity.endpoint,
            entity_type=identity.entity_type,
            tenant_id=identity.tenant_id,
            native_key=identity.native_key,
        )

    async def resolve_neutral_id(self, identity: IdentityRef) -> uuid.UUID | None:
        """:meth:`resolve`, reduced to the neutral id the sync loop usually wants."""
        binding = await self.resolve(identity)
        return None if binding is None else binding.neutral_id

    async def counterpart(
        self, neutral_id: uuid.UUID, endpoint: str, entity_type: EntityType
    ) -> IdentityBinding | None:
        """The binding this neutral id has at ``endpoint``, if any.

        This is how the sync loop finds the Qlik object a Databricks object writes to.
        """
        return await self._store.get_binding(neutral_id, endpoint, entity_type)

    async def register_source(self, identity: IdentityRef) -> IdentityBinding:
        """Ensure a neutral id exists for this *source* native key, and return its binding.

        This is RS-07 step 3's "if absent, allocate a ``neutralId`` and record the
        source-side mapping". It is safe to do unattended precisely because it binds a
        source native key to an id the engine mints for it — it matches nothing and can
        therefore claim nothing. It is not, and must not be confused with, a bootstrap
        match: nothing in this method looks at a name.

        Idempotent, including under a concurrent writer: a losing racer re-reads and
        returns the binding the winner created.
        """
        existing = await self.resolve(identity)
        if existing is not None:
            return existing

        neutral_id = uuid.uuid4()
        now = self._clock()
        try:
            async with self._store.unit_of_work() as uow:
                await uow.bind_identity(neutral_id, identity, confirmed=True, now=now)
        except IntegrityError:
            raced = await self.resolve(identity)
            if raced is None:
                raise
            return raced

        _LOG.info(
            "identity.source_registered",
            endpoint=identity.endpoint,
            tenant_id=identity.tenant_id,
            entity_type=identity.entity_type.value,
            native_key=identity.native_key,
            neutral_id=str(neutral_id),
        )
        bound = await self.resolve(identity)
        if bound is None:  # pragma: no cover - the write committed, so this cannot happen
            raise ConnectorError(
                "identity: binding vanished immediately after it was written",
                endpoint=identity.endpoint,
                entity_type=identity.entity_type.value,
            )
        return bound

    # -- enumeration (the store offers point reads only) -----------------------------

    async def list_bindings(
        self,
        *,
        endpoint: str | None = None,
        entity_type: EntityType | None = None,
        tenant_id: str | None = None,
        confirmed_only: bool = False,
    ) -> tuple[IdentityBinding, ...]:
        """Every stored binding, optionally filtered. Read-only; ordered deterministically."""

        def _read() -> tuple[IdentityBinding, ...]:
            stmt = select(IdentityMapRow)
            if endpoint is not None:
                stmt = stmt.where(IdentityMapRow.endpoint == endpoint)
            if entity_type is not None:
                stmt = stmt.where(IdentityMapRow.entity_type == entity_type.value)
            if tenant_id is not None:
                stmt = stmt.where(IdentityMapRow.tenant_id == tenant_id)
            if confirmed_only:
                stmt = stmt.where(IdentityMapRow.confirmed.is_(True))
            stmt = stmt.order_by(
                IdentityMapRow.endpoint,
                IdentityMapRow.entity_type,
                IdentityMapRow.tenant_id,
                IdentityMapRow.native_key,
            )
            with Session(self._store.engine) as session:
                return tuple(_binding_of(row) for row in session.scalars(stmt).all())

        return await asyncio.to_thread(_read)

    async def _bound_native_keys(
        self, endpoint: str, entity_type: EntityType, tenant_id: str, native_keys: Sequence[str]
    ) -> set[str]:
        """Which of ``native_keys`` are already bound at this endpoint+tenant."""
        if not native_keys:
            return set()

        def _read() -> set[str]:
            stmt = select(IdentityMapRow.native_key).where(
                IdentityMapRow.endpoint == endpoint,
                IdentityMapRow.entity_type == entity_type.value,
                IdentityMapRow.tenant_id == tenant_id,
                IdentityMapRow.native_key.in_(list(dict.fromkeys(native_keys))),
            )
            with Session(self._store.engine) as session:
                return set(session.scalars(stmt).all())

        return await asyncio.to_thread(_read)

    # -- the review file --------------------------------------------------------------

    async def load_review(self) -> IdentityReview:
        """Read the review file. An absent file is an empty review, not an error."""

        def _read() -> IdentityReview:
            if not self._review_path.exists():
                return IdentityReview(
                    version=REVIEW_FILE_VERSION, generated_at=self._clock(), proposals=()
                )
            text = self._review_path.read_text(encoding="utf-8")
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConnectorError(
                    f"identity review file {self._review_path} is not valid JSON: {exc}"
                ) from exc
            return _review_from_json(raw)

        return await asyncio.to_thread(_read)

    async def save_review(self, review: IdentityReview) -> Path:
        """Write the review file atomically (temp file + rename), creating parents."""

        def _write() -> Path:
            payload = json.dumps(_review_to_json(review), indent=2, ensure_ascii=False) + "\n"
            self._review_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._review_path.with_name(self._review_path.name + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._review_path)
            return self._review_path

        return await asyncio.to_thread(_write)

    async def list_proposals(
        self,
        *,
        status: ProposalStatus | None = None,
        decision: ReviewDecision | None = None,
        needs_human: bool = False,
    ) -> tuple[MatchProposal, ...]:
        """List proposals from the review file, filtered. The CLI's ``list`` operation.

        Ordered by ``proposal_id``, the same order the file is written in, so a listing
        and the file always read the same way.
        """
        review = await self.load_review()
        selected = [
            proposal
            for proposal in review.proposals
            if (status is None or proposal.status is status)
            and (decision is None or proposal.decision is decision)
            and (not needs_human or proposal.needs_human)
        ]
        return tuple(sorted(selected, key=lambda p: p.proposal_id))

    # -- bootstrap --------------------------------------------------------------------

    async def bootstrap(
        self,
        *,
        source_objects: Sequence[CatalogObject],
        target_candidates: Sequence[CatalogObject],
        target_endpoint: str,
        target_tenant_id: str,
        parent_path_rule: ParentPathRule | None = None,
    ) -> BootstrapReport:
        """Propose bindings for source objects that have none, and bind nothing.

        Writes only the review file — this method performs **no** database write, so
        there is no code path by which running a sync cycle can create or promote a
        binding. Re-running it is idempotent: proposals are merged by
        ``proposal_id``, a human's ``decision``/``chosen_native_key``/``note`` survive,
        and nothing is duplicated.

        If the catalogs changed since the file was written, a non-pending decision whose
        candidate set no longer matches is reset to ``pending`` and the old verdict is
        preserved in ``superseded_decision``. A decision is never applied to a set of
        objects other than the one it was made about.
        """
        rule = parent_path_rule if parent_path_rule is not None else self._parent_path_rule
        now = self._clock()

        candidates_by_key = self._index_candidates(
            target_candidates, target_endpoint, target_tenant_id, rule
        )
        bound_target_keys = await self._bound_targets(
            target_candidates, target_endpoint, target_tenant_id
        )

        existing = await self.load_review()
        merged: dict[str, MatchProposal] = {p.proposal_id: p for p in existing.proposals}
        superseded: list[str] = []
        fresh_by_status: dict[ProposalStatus, list[MatchProposal]] = {
            status: [] for status in ProposalStatus
        }

        for source in self._deduplicate(source_objects):
            fresh = await self._propose(
                source=source,
                candidates_by_key=candidates_by_key,
                bound_target_keys=bound_target_keys,
                target_endpoint=target_endpoint,
                target_tenant_id=target_tenant_id,
                rule=rule,
                now=now,
            )
            previous = merged.get(fresh.proposal_id)
            combined, was_superseded = _merge(previous, fresh)
            merged[combined.proposal_id] = combined
            if was_superseded:
                superseded.append(combined.proposal_id)
            fresh_by_status[combined.status].append(combined)

        review = IdentityReview(
            version=REVIEW_FILE_VERSION,
            generated_at=now,
            proposals=tuple(sorted(merged.values(), key=lambda p: p.proposal_id)),
        )
        path = await self.save_review(review)

        report = BootstrapReport(
            review_path=path,
            proposed=tuple(fresh_by_status[ProposalStatus.PROPOSED]),
            ambiguous=tuple(fresh_by_status[ProposalStatus.AMBIGUOUS]),
            unmatched=tuple(fresh_by_status[ProposalStatus.UNMATCHED]),
            already_bound=tuple(fresh_by_status[ProposalStatus.BOUND]),
            superseded=tuple(superseded),
        )
        _LOG.info(
            "identity.bootstrap_completed",
            target_endpoint=target_endpoint,
            target_tenant_id=target_tenant_id,
            considered=report.considered,
            proposed=len(report.proposed),
            ambiguous=len(report.ambiguous),
            unmatched=len(report.unmatched),
            already_bound=len(report.already_bound),
            superseded=len(report.superseded),
            review_path=str(path),
        )
        for proposal in report.ambiguous:
            _LOG.warning(
                "identity.ambiguous_match",
                proposal_id=proposal.proposal_id,
                natural_key=proposal.source.natural_key.display,
                candidates=[c.identity.native_key for c in proposal.candidates],
            )
        return report

    @staticmethod
    def _deduplicate(source_objects: Sequence[CatalogObject]) -> list[CatalogObject]:
        """Collapse identical repeats; refuse contradictory ones rather than pick."""
        seen: dict[str, CatalogObject] = {}
        for source in source_objects:
            previous = seen.get(source.key)
            if previous is None:
                seen[source.key] = source
            elif previous != source:
                raise ValueError(
                    f"two different source objects share the native key {source.key!r}: "
                    f"{previous.natural_key.display!r} and {source.natural_key.display!r}"
                )
        return list(seen.values())

    def _index_candidates(
        self,
        target_candidates: Sequence[CatalogObject],
        target_endpoint: str,
        target_tenant_id: str,
        rule: ParentPathRule,
    ) -> dict[tuple[str, str, tuple[str, ...]], list[CatalogObject]]:
        index: dict[tuple[str, str, tuple[str, ...]], list[CatalogObject]] = {}
        for candidate in target_candidates:
            if candidate.identity.endpoint != target_endpoint:
                raise ValueError(
                    f"candidate {candidate.identity.native_key!r} is at endpoint "
                    f"{candidate.identity.endpoint!r}, not the target endpoint "
                    f"{target_endpoint!r}"
                )
            if candidate.identity.tenant_id != target_tenant_id:
                raise ValueError(
                    f"candidate {candidate.identity.native_key!r} is in tenant "
                    f"{candidate.identity.tenant_id!r}, not the target tenant "
                    f"{target_tenant_id!r}"
                )
            comparable = candidate.natural_key.comparable(rule)
            if not comparable[1]:
                continue
            index.setdefault(comparable, []).append(candidate)
        return index

    async def _bound_targets(
        self,
        target_candidates: Sequence[CatalogObject],
        target_endpoint: str,
        target_tenant_id: str,
    ) -> set[str]:
        bound: set[str] = set()
        by_type: dict[EntityType, list[str]] = {}
        for candidate in target_candidates:
            by_type.setdefault(candidate.identity.entity_type, []).append(
                candidate.identity.native_key
            )
        for entity_type, keys in by_type.items():
            bound |= await self._bound_native_keys(
                target_endpoint, entity_type, target_tenant_id, keys
            )
        return bound

    async def _propose(
        self,
        *,
        source: CatalogObject,
        candidates_by_key: dict[tuple[str, str, tuple[str, ...]], list[CatalogObject]],
        bound_target_keys: set[str],
        target_endpoint: str,
        target_tenant_id: str,
        rule: ParentPathRule,
        now: datetime,
    ) -> MatchProposal:
        source_binding = await self.resolve(source.identity)
        if source_binding is not None:
            counterpart = await self.counterpart(
                source_binding.neutral_id, target_endpoint, source.identity.entity_type
            )
            if counterpart is not None:
                return MatchProposal(
                    proposal_id=source.key,
                    status=ProposalStatus.BOUND,
                    decision=ReviewDecision.PENDING,
                    source=source,
                    target_endpoint=target_endpoint,
                    target_tenant_id=target_tenant_id,
                    candidates=(),
                    candidates_fingerprint=_fingerprint((), rule),
                    rationale=(
                        f"Already bound: neutral id {source_binding.neutral_id} maps to "
                        f"{target_endpoint} {counterpart.identity.native_key!r}. Natural-key "
                        "matching is a bootstrap mechanism only and was not run."
                    ),
                    first_proposed_at=now,
                    last_seen_at=now,
                    applied_at=source_binding.confirmed_at,
                    applied_neutral_id=source_binding.neutral_id,
                )

        comparable = source.natural_key.comparable(rule)
        if not comparable[1]:
            return self._unmatched(
                source,
                target_endpoint,
                target_tenant_id,
                rule,
                now,
                rationale=(
                    f"Source name {source.natural_key.name!r} normalizes to an empty string, "
                    "which is no evidence of anything; no candidate was considered."
                ),
            )

        all_matches = candidates_by_key.get(comparable, [])
        excluded = tuple(
            sorted(
                candidate.identity.native_key
                for candidate in all_matches
                if candidate.identity.native_key in bound_target_keys
            )
        )
        matches = tuple(
            sorted(
                (
                    candidate
                    for candidate in all_matches
                    if candidate.identity.native_key not in bound_target_keys
                ),
                key=lambda candidate: candidate.identity.native_key,
            )
        )
        rule_note = (
            "name and type only (parent path ignored: candidates are scoped to one "
            "target container)"
            if rule is ParentPathRule.IGNORE
            else "name, type and parent path"
        )

        if not matches:
            excluded_note = (
                ""
                if not excluded
                else (
                    f" {len(excluded)} name match(es) were excluded because they are already "
                    f"bound to another source object: {', '.join(excluded)}."
                )
            )
            return self._unmatched(
                source,
                target_endpoint,
                target_tenant_id,
                rule,
                now,
                excluded=excluded,
                rationale=(
                    f"No unbound {target_endpoint} {source.identity.entity_type.value} shares "
                    f"the natural key {source.natural_key.display!r} (compared on {rule_note}, "
                    f"NFC-normalized, whitespace-trimmed, case-insensitively).{excluded_note}"
                ),
            )

        status = ProposalStatus.PROPOSED if len(matches) == 1 else ProposalStatus.AMBIGUOUS
        if status is ProposalStatus.PROPOSED:
            rationale = (
                f"Exactly one {target_endpoint} {source.identity.entity_type.value} shares the "
                f"natural key {source.natural_key.display!r} (compared on {rule_note}, "
                f"NFC-normalized, whitespace-trimmed, case-insensitively): "
                f"{matches[0].identity.native_key!r} named {matches[0].natural_key.name!r}. "
                "Confirm to bind it; nothing is bound until you do."
            )
        else:
            rationale = (
                f"{len(matches)} {target_endpoint} {source.identity.entity_type.value} objects "
                f"share the natural key {source.natural_key.display!r} equally well (compared on "
                f"{rule_note}, NFC-normalized, whitespace-trimmed, case-insensitively): "
                f"{', '.join(repr(m.identity.native_key) for m in matches)}. There is no "
                "tie-break — set chosen_native_key to the right one, or reject."
            )
        if excluded:
            rationale += (
                f" Additionally excluded as already bound to another source object: "
                f"{', '.join(excluded)}."
            )

        return MatchProposal(
            proposal_id=source.key,
            status=status,
            decision=ReviewDecision.PENDING,
            source=source,
            target_endpoint=target_endpoint,
            target_tenant_id=target_tenant_id,
            candidates=matches,
            candidates_fingerprint=_fingerprint(matches, rule),
            rationale=rationale,
            first_proposed_at=now,
            last_seen_at=now,
            excluded_bound_candidates=excluded,
        )

    @staticmethod
    def _unmatched(
        source: CatalogObject,
        target_endpoint: str,
        target_tenant_id: str,
        rule: ParentPathRule,
        now: datetime,
        *,
        rationale: str,
        excluded: tuple[str, ...] = (),
    ) -> MatchProposal:
        return MatchProposal(
            proposal_id=source.key,
            status=ProposalStatus.UNMATCHED,
            decision=ReviewDecision.PENDING,
            source=source,
            target_endpoint=target_endpoint,
            target_tenant_id=target_tenant_id,
            candidates=(),
            candidates_fingerprint=_fingerprint((), rule),
            rationale=rationale,
            first_proposed_at=now,
            last_seen_at=now,
            excluded_bound_candidates=excluded,
        )

    # -- confirmation ------------------------------------------------------------------

    async def confirm(
        self, proposal_id: str, *, candidate_native_key: str | None = None
    ) -> ConfirmationResult:
        """Bind one proposal after an explicit human decision. The CLI's ``confirm``.

        ``candidate_native_key`` is required for an ambiguous proposal and optional for a
        single-candidate one (where it is checked against that candidate if given). The
        binding is written and the review file updated to record what was applied.

        Raises :class:`~qlabs_catalog_sync_sdk.exceptions.NotFound` when the proposal or
        the named candidate does not exist, and
        :class:`~qlabs_catalog_sync_sdk.exceptions.ConflictError` when confirming would
        contradict the stored map. Confirming the same thing twice returns
        :attr:`ConfirmationOutcome.ALREADY_BOUND` and changes nothing.
        """
        review = await self.load_review()
        proposal = review.get(proposal_id)
        if proposal is None:
            raise NotFound(
                f"no identity proposal {proposal_id!r} in {self._review_path}",
                native_key=proposal_id,
            )
        result, updated = await self._apply_confirm(proposal, candidate_native_key)
        await self.save_review(_with_proposal(review, updated, self._clock()))
        return result

    async def reject(self, proposal_id: str, *, reason: str | None = None) -> ConfirmationResult:
        """Record that a proposal must not be bound. Writes nothing to the map.

        A rejected proposal is not re-proposed by later bootstrap runs while its candidate
        set is unchanged; if the candidates change, the rejection is superseded and the
        entry comes back as pending, because it is a verdict on objects that no longer
        exist as they were.
        """
        review = await self.load_review()
        proposal = review.get(proposal_id)
        if proposal is None:
            raise NotFound(
                f"no identity proposal {proposal_id!r} in {self._review_path}",
                native_key=proposal_id,
            )
        if proposal.applied_at is not None:
            raise ConflictError(
                f"proposal {proposal_id!r} was already applied and cannot be rejected; "
                "it is a stored binding now",
                endpoint=proposal.target_endpoint,
                entity_type=proposal.source.identity.entity_type.value,
            )
        now = self._clock()
        updated = replace(
            proposal,
            decision=ReviewDecision.REJECT,
            decided_at=now,
            chosen_native_key=None,
            note=reason if reason is not None else proposal.note,
        )
        await self.save_review(_with_proposal(review, updated, now))
        _LOG.info("identity.proposal_rejected", proposal_id=proposal_id)
        return ConfirmationResult(
            proposal_id=proposal_id,
            outcome=ConfirmationOutcome.REJECTED,
            detail="recorded as rejected; nothing was bound",
        )

    async def apply_review_file(self) -> ApplyReport:
        """Apply every ``confirm`` decision in the review file.

        This is how a human's edits to the file become bindings. Each proposal is applied
        independently: one that contradicts reality is reported as
        :attr:`ConfirmationOutcome.FAILED` with the reason and the rest still apply, so a
        single stale entry cannot block a whole review. The file is written once, at the
        end, with what was applied.
        """
        review = await self.load_review()
        results: list[ConfirmationResult] = []
        updated_proposals: dict[str, MatchProposal] = {}

        for proposal in review.proposals:
            if proposal.decision is not ReviewDecision.CONFIRM:
                continue
            if proposal.applied_at is not None:
                results.append(
                    ConfirmationResult(
                        proposal_id=proposal.proposal_id,
                        outcome=ConfirmationOutcome.SKIPPED,
                        detail="already applied",
                        neutral_id=proposal.applied_neutral_id,
                        target_native_key=proposal.chosen_native_key,
                    )
                )
                continue
            try:
                result, updated = await self._apply_confirm(proposal, proposal.chosen_native_key)
            except ConnectorError as exc:
                _LOG.warning(
                    "identity.confirmation_failed",
                    proposal_id=proposal.proposal_id,
                    reason=exc.message,
                )
                results.append(
                    ConfirmationResult(
                        proposal_id=proposal.proposal_id,
                        outcome=ConfirmationOutcome.FAILED,
                        detail=exc.message,
                    )
                )
                continue
            results.append(result)
            updated_proposals[updated.proposal_id] = updated

        if updated_proposals:
            now = self._clock()
            review = IdentityReview(
                version=REVIEW_FILE_VERSION,
                generated_at=now,
                proposals=tuple(
                    updated_proposals.get(proposal.proposal_id, proposal)
                    for proposal in review.proposals
                ),
            )
            await self.save_review(review)

        return ApplyReport(review_path=self._review_path, results=tuple(results))

    async def _apply_confirm(
        self, proposal: MatchProposal, candidate_native_key: str | None
    ) -> tuple[ConfirmationResult, MatchProposal]:
        """Bind one proposal. Raises on anything that contradicts the stored map."""
        entity_type = proposal.source.identity.entity_type
        chosen = self._choose_candidate(proposal, candidate_native_key)

        source_binding = await self.resolve(proposal.source.identity)
        neutral_id = uuid.uuid4() if source_binding is None else source_binding.neutral_id

        counterpart = await self.counterpart(neutral_id, proposal.target_endpoint, entity_type)
        if counterpart is not None:
            if counterpart.identity.native_key == chosen.identity.native_key:
                now = self._clock()
                return (
                    ConfirmationResult(
                        proposal_id=proposal.proposal_id,
                        outcome=ConfirmationOutcome.ALREADY_BOUND,
                        detail="this exact binding is already stored; nothing changed",
                        neutral_id=neutral_id,
                        target_native_key=chosen.identity.native_key,
                    ),
                    replace(
                        proposal,
                        status=ProposalStatus.BOUND,
                        decision=ReviewDecision.CONFIRM,
                        chosen_native_key=chosen.identity.native_key,
                        decided_at=proposal.decided_at or now,
                        applied_at=proposal.applied_at or now,
                        applied_neutral_id=neutral_id,
                    ),
                )
            raise ConflictError(
                f"proposal {proposal.proposal_id!r} would bind neutral id {neutral_id} to "
                f"{proposal.target_endpoint} {chosen.identity.native_key!r}, but it is already "
                f"bound to {counterpart.identity.native_key!r}. Rebinding is never automatic.",
                endpoint=proposal.target_endpoint,
                entity_type=entity_type.value,
            )

        occupant = await self._store.find_binding(
            endpoint=chosen.identity.endpoint,
            entity_type=entity_type,
            tenant_id=chosen.identity.tenant_id,
            native_key=chosen.identity.native_key,
        )
        if occupant is not None and occupant.neutral_id != neutral_id:
            raise ConflictError(
                f"{proposal.target_endpoint} {chosen.identity.native_key!r} is already bound to "
                f"neutral id {occupant.neutral_id}; it cannot also represent "
                f"{proposal.source.identity.native_key!r}",
                endpoint=proposal.target_endpoint,
                entity_type=entity_type.value,
            )

        now = self._clock()
        try:
            async with self._store.unit_of_work() as uow:
                if source_binding is None:
                    await uow.bind_identity(
                        neutral_id, proposal.source.identity, confirmed=True, now=now
                    )
                await uow.bind_identity(neutral_id, chosen.identity, confirmed=True, now=now)
        except IntegrityError as exc:
            raise ConflictError(
                f"binding {proposal.proposal_id!r} lost a race against another writer; the "
                "transaction was rolled back and nothing was written. Re-run bootstrap and "
                "review the current state.",
                endpoint=proposal.target_endpoint,
                entity_type=entity_type.value,
                cause=exc,
            ) from exc

        _LOG.info(
            "identity.binding_confirmed",
            proposal_id=proposal.proposal_id,
            neutral_id=str(neutral_id),
            source_endpoint=proposal.source.identity.endpoint,
            source_native_key=proposal.source.identity.native_key,
            target_endpoint=proposal.target_endpoint,
            target_native_key=chosen.identity.native_key,
        )
        return (
            ConfirmationResult(
                proposal_id=proposal.proposal_id,
                outcome=ConfirmationOutcome.BOUND,
                detail=(
                    f"bound {proposal.source.identity.native_key!r} to "
                    f"{chosen.identity.native_key!r}"
                ),
                neutral_id=neutral_id,
                target_native_key=chosen.identity.native_key,
            ),
            replace(
                proposal,
                status=ProposalStatus.BOUND,
                decision=ReviewDecision.CONFIRM,
                chosen_native_key=chosen.identity.native_key,
                decided_at=proposal.decided_at or now,
                applied_at=now,
                applied_neutral_id=neutral_id,
            ),
        )

    @staticmethod
    def _choose_candidate(
        proposal: MatchProposal, candidate_native_key: str | None
    ) -> CatalogObject:
        if candidate_native_key is not None:
            chosen = proposal.candidate(candidate_native_key)
            if chosen is None:
                raise NotFound(
                    f"{candidate_native_key!r} is not a candidate of proposal "
                    f"{proposal.proposal_id!r}; its candidates are "
                    f"{[c.identity.native_key for c in proposal.candidates]}",
                    endpoint=proposal.target_endpoint,
                    entity_type=proposal.source.identity.entity_type.value,
                    native_key=candidate_native_key,
                )
            return chosen
        if proposal.status is ProposalStatus.AMBIGUOUS:
            raise ConflictError(
                f"proposal {proposal.proposal_id!r} is ambiguous between "
                f"{[c.identity.native_key for c in proposal.candidates]}; a candidate must be "
                "named explicitly. This module does not pick.",
                endpoint=proposal.target_endpoint,
                entity_type=proposal.source.identity.entity_type.value,
            )
        if len(proposal.candidates) != 1:
            raise ConflictError(
                f"proposal {proposal.proposal_id!r} has no candidate to bind "
                f"(status {proposal.status.value})",
                endpoint=proposal.target_endpoint,
                entity_type=proposal.source.identity.entity_type.value,
            )
        return proposal.candidates[0]


# --------------------------------------------------------------------------------------
# Module-private helpers
# --------------------------------------------------------------------------------------


def _binding_of(row: IdentityMapRow) -> IdentityBinding:
    return IdentityBinding(
        neutral_id=row.neutral_id,
        identity=IdentityRef(
            endpoint=row.endpoint,
            entity_type=EntityType(row.entity_type),
            native_key=row.native_key,
            tenant_id=row.tenant_id,
            secondary_keys=dict(row.secondary_keys),
        ),
        confirmed=row.confirmed,
        created_at=row.created_at,
        updated_at=row.updated_at,
        confirmed_at=row.confirmed_at,
    )


def _with_proposal(
    review: IdentityReview, proposal: MatchProposal, now: datetime
) -> IdentityReview:
    replaced = tuple(
        proposal if existing.proposal_id == proposal.proposal_id else existing
        for existing in review.proposals
    )
    return IdentityReview(version=REVIEW_FILE_VERSION, generated_at=now, proposals=replaced)


def _merge(previous: MatchProposal | None, fresh: MatchProposal) -> tuple[MatchProposal, bool]:
    """Fold a fresh bootstrap result onto what the file already held.

    Returns ``(merged, superseded)``. Human-owned fields (``decision``,
    ``chosen_native_key``, ``note``) and the applied-binding record survive; machine-owned
    fields are taken from the fresh result. A decision made about a *different* candidate
    set is never carried over — it is reset to pending and preserved verbatim in
    ``superseded_decision`` so nothing a person wrote is lost and nothing stale is acted on.
    """
    if previous is None:
        return fresh, False

    carried = replace(
        fresh,
        first_proposed_at=previous.first_proposed_at,
        note=previous.note,
        applied_at=previous.applied_at or fresh.applied_at,
        applied_neutral_id=previous.applied_neutral_id or fresh.applied_neutral_id,
    )

    if previous.applied_at is not None:
        return (
            replace(
                carried,
                decision=previous.decision,
                chosen_native_key=previous.chosen_native_key,
                decided_at=previous.decided_at,
                superseded_decision=previous.superseded_decision,
            ),
            False,
        )

    if previous.candidates_fingerprint == fresh.candidates_fingerprint:
        return (
            replace(
                carried,
                decision=previous.decision,
                chosen_native_key=previous.chosen_native_key,
                decided_at=previous.decided_at,
                superseded_decision=previous.superseded_decision,
            ),
            False,
        )

    if previous.decision is ReviewDecision.PENDING:
        return replace(carried, superseded_decision=previous.superseded_decision), False

    decided = "unknown time" if previous.decided_at is None else _dt_to_json(previous.decided_at)
    chosen = previous.chosen_native_key or "-"
    return (
        replace(
            carried,
            decision=ReviewDecision.PENDING,
            chosen_native_key=None,
            decided_at=None,
            superseded_decision=(
                f"{previous.decision.value} (chosen {chosen}) recorded at {decided} against "
                f"candidates {previous.candidates_fingerprint}, which no longer match what the "
                "catalogs report; re-review before applying"
            ),
        ),
        True,
    )
