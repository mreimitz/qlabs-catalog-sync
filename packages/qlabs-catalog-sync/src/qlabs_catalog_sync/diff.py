"""Field diff engine — the minimal mutation set for one entity at one target.

WP7 / T7.2. Answers one question: *given what the source reports now and what we last
knew of the target, what is the smallest honest set of field writes?* This is where
"the sync writes only what actually differs" becomes true or false, so every rule below
is deliberate.

The engine builds on the SDK's envelope layer (T1.6) and shapes its result with the
target's capability manifest (T1.3). It emits the SDK's own :class:`FieldDiff` /
:class:`FieldChange` types — there is no parallel diff representation — wrapped in a
:class:`DiffPlan` that also carries what could **not** be written and why.

Rules, and why each one is what it is
-------------------------------------

**Minimality.** A field whose checksum matches the last-known target checksum produces
no change at all. The comparison is :func:`~qlabs_catalog_sync_sdk.envelope.changed_fields`
and nothing else, so this module inherits the canonicalization contract wholesale rather
than re-deriving it: an order-insensitive array reordered at the source hashes the same
and is therefore invisible here (the phantom diff that would otherwise rewrite every
Qlik data product on every cycle), and a genuinely different array is not.

**An empty diff is a result, not an absence.** Identical source and target produce a
:class:`DiffPlan` whose :attr:`DiffPlan.is_empty` is ``True`` and whose ``diff.changes``
is ``[]``. The sync loop turns that into a skipped write, which is the whole idempotency
claim, so it must never be confused with "no diff was computed". This module makes the
two structurally different: you either hold a plan (a diff was computed) or an exception
was raised. Nothing returns ``None``.

**Absent is not null.** ``null`` is the source asserting the field is empty — clear the
target. Absent is the source not reporting the field — leave the target's copy alone.
``changed_fields`` already applies that rule (only fields present in the source can be
reported), and this module never widens the field set beyond it. In an upstream-only v1
that is also the only safe reading: a source that stops reporting ``tags`` has not asked
for the target's tags to be deleted.

**The value that goes out is the source's, verbatim.** Every emitted value is the
faithful projection (:func:`~qlabs_catalog_sync_sdk.envelope.to_json_value`) of the
source envelope's stored value. :func:`~qlabs_catalog_sync_sdk.envelope.canonicalize`
appears nowhere in this module and must not: sorting ``datasetIds`` before hashing is
what kills the phantom diff, sorting them before writing would be a gratuitous mutation
of the customer's data. For the same reason a widened (full-replace) value is taken from
the source, never assembled by patching the stored target copy — the stored copy is a
cache of what we last saw, and rebuilding a desired value out of it would carry stale
target state into the write.

**Widening.** When the manifest declares ``partial_update=False`` for a field, the
change is emitted as :attr:`~qlabs_catalog_sync_sdk.models.FieldUpdateMode.REPLACE`
carrying the complete desired value. Qlik data-product arrays are the motivating case:
one added tag means sending the whole ``tags`` array (RS-02 readiness note section 2,
RS-03 section 7). Because a field envelope always holds a field's *whole* value, the
complete desired value is simply the source value — widening changes the declared write
intent, never the payload.

**``ro``/``na`` are never emitted — and never silently dropped.** Writability is decided
by :meth:`~qlabs_catalog_sync_sdk.contract.CapabilityManifestBase.is_writable`, which
fails closed: a field the manifest does not mention is treated exactly like ``na``. A
field that *differs* but cannot be written is recorded in :attr:`DiffPlan.dropped` with
the reason that forbade it, so a run report can say "these fields could not be carried
across" instead of the sync quietly losing them. A field that is ``ro`` but happens to
match needs no report — nothing was lost. The connector's
:meth:`~qlabs_catalog_sync_sdk.contract.Connector.ensure_writable` guard therefore never
fires on a diff from this module; that guard is the connector's belt and braces, not a
substitute for planning honestly.

**``allowed_update_paths`` is only checkable where the mapping is known.** The manifest
records the endpoint's *native wire* paths (``/datasetIds``); a diff is in *neutral*
field names (``dataset_refs``), and the connector — not the engine — owns that
translation. Refusing a field on a mapping this module does not own would be worse than
not refusing: it would drop a legitimate write on a guess. So the check is opt-in via
``native_update_paths``. Supply it and the rule is enforced honestly and fails closed (a
field with no mapping, or one mapped outside the enum, is dropped and reported); omit it
and the enum is left to the connector, which is the only component that can apply it
correctly. Either way the connector's own translation remains the authoritative gate.

**Batching is the connector's job, not the diff engine's.** ``max_update_operations``
(Qlik: 8 operations per data-product PATCH) is a *transport* limit on one request, while
a diff is a complete statement about one entity. Splitting it here would be wrong twice
over: one neutral field can map to zero, one, or several native operations, so counting
neutral changes against a native cap counts the wrong things; and a :class:`FieldDiff`
carries exactly one ``expected_revision``, which the first request invalidates — every
batch after the first would ship a revision that is stale by construction. This module
therefore never truncates and never splits. It records the declared cap, and exposes
:attr:`DiffPlan.exceeds_operation_cap` and :attr:`DiffPlan.request_count` so the writer
and the dry-run plan can see the split coming.

**``expected_revision``.** Taken from the ``source_revision`` of the last-known target
envelopes of *the fields this diff actually changes* — those are the reads the write is
guarded against. One distinct revision is used; none leaves it ``None``. Several distinct
revisions leave it ``None`` and set :attr:`DiffPlan.revision_ambiguous`, because picking
one of them would be a fabrication: envelopes for one entity can legitimately come from
more than one target resource (a Qlik data product and its item metadata are different
GETs), and only the connector knows which resource an ``if-match`` would guard.

**An unsupported entity type raises.** RS-08 section 8 is explicit that unsupported
entity types are simply not scheduled, so being asked for one is a planning bug, not a
data condition. Returning an empty diff would report "in sync" for something that can
never sync — a green line in the run report for work that did not happen. It raises
:class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError` instead: the SDK's exact
type for "the engine asked for something the manifest forbids", never retryable.

Synchronous on purpose: this module performs no I/O. It is pure functions over two
mappings and a manifest, which is what makes it cheap enough to run for every field of
every entity on every cycle, and trivial to test. The engine's async convention applies
to I/O, and there is none here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import structlog
from pydantic import JsonValue

from qlabs_catalog_sync_sdk.contract import CapabilityManifestBase
from qlabs_catalog_sync_sdk.envelope import changed_fields, to_json_value
from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.manifest import (
    CapabilityManifest,
    ConcurrencyMode,
    FieldCapability,
    FieldCapabilityMode,
)
from qlabs_catalog_sync_sdk.models import (
    EntityType,
    FieldChange,
    FieldDiff,
    FieldEnvelope,
    FieldUpdateMode,
)

__all__ = [
    "DiffPlan",
    "DropReason",
    "DroppedField",
    "compute_field_diff",
]

_LOG = structlog.get_logger("qlabs.catalog_sync.diff")


# --------------------------------------------------------------------------------------
# What could not be written
# --------------------------------------------------------------------------------------


class DropReason(StrEnum):
    """Why a field that genuinely differs was not emitted as a change.

    Every member describes a *manifest* decision, never a failure: a dropped field is
    the sync working as declared. Keeping ``ro`` and ``na`` apart matters for the run
    report — one says "the endpoint owns this value", the other says "the endpoint has
    no such thing" — and is the distinction
    :class:`~qlabs_catalog_sync_sdk.manifest.FieldCapabilityMode` exists to preserve.
    """

    READ_ONLY = "read_only"
    """The target declares the field ``ro``: it can express the field but only returns
    it (Qlik ``secureQri``, a status the endpoint owns). The source's value is real and
    is deliberately not carried across."""

    NOT_APPLICABLE = "not_applicable"
    """The target declares the field ``na``: no native equivalent, or unavailable at
    this endpoint instance."""

    UNDECLARED = "undeclared"
    """The target's manifest does not mention the field at all. Treated exactly like
    ``na`` — silence is not permission — so a connector that grew a neutral field
    without updating its manifest loses the field visibly instead of gaining a write
    path it cannot serve."""

    NO_UPDATE_PATH = "no_update_path"
    """The field maps to no native path inside the endpoint's closed update-path enum.
    Only ever produced when the caller supplied ``native_update_paths``; see the module
    docstring for why the engine will not guess the mapping."""


@dataclass(frozen=True, slots=True)
class DroppedField:
    """One field the source changed that the target will not be asked to write."""

    field: str
    """The neutral field name."""

    reason: DropReason

    value: JsonValue = None
    """The source value that could not be carried across, in its faithful JSON form.
    Present so a run report can show what was lost; never logged by this module."""

    capability_mode: FieldCapabilityMode | None = None
    """The manifest mode that forbade the write, when the manifest declares the field.
    ``None`` for an undeclared field — there was no mode to read."""

    native_path: str | None = None
    """For :attr:`DropReason.NO_UPDATE_PATH`, the native path the caller's mapping gave
    for this field, or ``None`` when the mapping had no entry for it at all."""


# --------------------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiffPlan:
    """A computed diff plus everything the caller needs to act on it honestly.

    Holding a plan means a diff *was* computed. An empty :attr:`diff` therefore means
    "nothing differs that this target can write", which is exactly the signal the sync
    loop turns into a skipped write.
    """

    diff: FieldDiff
    """The minimal mutation set, in the SDK's own type. Possibly empty."""

    dropped: tuple[DroppedField, ...] = ()
    """Fields that differ but were not emitted, in the same sorted order as the diff."""

    max_update_operations: int | None = None
    """The target's declared per-request operation cap, carried through for the writer.
    ``None`` when the endpoint declares no cap."""

    revision_ambiguous: bool = False
    """True when the changed fields' last-known target envelopes disagreed about
    ``source_revision``, so no single ``expected_revision`` could be claimed."""

    @property
    def entity_type(self) -> EntityType:
        """The entity type this plan was computed for."""
        return self.diff.entity_type

    @property
    def changes(self) -> tuple[FieldChange, ...]:
        """The emitted changes."""
        return tuple(self.diff.changes)

    @property
    def expected_revision(self) -> str | None:
        """The revision the diff was computed against, for ``if-match`` on the write."""
        return self.diff.expected_revision

    @property
    def is_empty(self) -> bool:
        """True when nothing needs writing. The idempotent-skip signal."""
        return not self.diff.changes

    @property
    def changed_field_names(self) -> tuple[str, ...]:
        """Neutral names of the fields this plan writes, sorted."""
        return tuple(self.diff.field_names)

    @property
    def dropped_field_names(self) -> tuple[str, ...]:
        """Neutral names of the fields that differ but will not be written, sorted."""
        return tuple(dropped.field for dropped in self.dropped)

    @property
    def operation_count(self) -> int:
        """Number of neutral field changes.

        A lower bound on native operations, not a translation of them: one neutral field
        may become several native operations, or none. The connector counts what it
        actually sends.
        """
        return len(self.diff.changes)

    @property
    def exceeds_operation_cap(self) -> bool:
        """True when this plan cannot be one request under the declared cap.

        Advisory. Nothing was truncated — dropping a real change to fit a request would
        be a silent data loss — so the writer must batch (see the module docstring).
        """
        cap = self.max_update_operations
        return cap is not None and self.operation_count > cap

    @property
    def request_count(self) -> int:
        """Minimum write requests this plan needs at the declared cap. ``0`` when empty."""
        count = self.operation_count
        if count == 0:
            return 0
        cap = self.max_update_operations
        if cap is None:
            return 1
        return (count + cap - 1) // cap

    def change_for(self, field: str) -> FieldChange | None:
        """The emitted change for ``field``, or ``None``."""
        return self.diff.change_for(field)

    def dropped_for(self, field: str) -> DroppedField | None:
        """The drop record for ``field``, or ``None`` when it was not dropped."""
        for dropped in self.dropped:
            if dropped.field == field:
                return dropped
        return None


# --------------------------------------------------------------------------------------
# Manifest lookups
# --------------------------------------------------------------------------------------


_DROP_REASONS: Final[dict[FieldCapabilityMode, DropReason]] = {
    FieldCapabilityMode.RO: DropReason.READ_ONLY,
    FieldCapabilityMode.NA: DropReason.NOT_APPLICABLE,
}


@dataclass(frozen=True, slots=True)
class _TargetConstraints:
    """The entity-level detail the base manifest contract does not expose.

    ``CapabilityManifestBase`` answers only supports / is_writable / requires_full_replace.
    ``allowed_update_paths``, ``max_update_operations``, the per-field ``ro``-vs-``na``
    distinction and the connector's concurrency mode all live on the concrete
    :class:`~qlabs_catalog_sync_sdk.manifest.CapabilityManifest`. This narrows to it once,
    so the rest of the module has no ``isinstance`` branching: with any other conformant
    manifest the extras are simply absent and the diff still computes correctly from the
    three questions the base contract does answer.
    """

    fields: Mapping[str, FieldCapability]
    allowed_update_paths: frozenset[str] | None
    max_update_operations: int | None
    concurrency: ConcurrencyMode | None


def _constraints(manifest: CapabilityManifestBase, entity_type: EntityType) -> _TargetConstraints:
    if not isinstance(manifest, CapabilityManifest):
        return _TargetConstraints({}, None, None, None)
    capability = manifest.entity_capability(entity_type)
    if capability is None:  # pragma: no cover - supports() already excluded this
        return _TargetConstraints({}, None, None, manifest.concurrency)
    paths = capability.allowed_update_paths
    return _TargetConstraints(
        fields=capability.fields,
        allowed_update_paths=None if paths is None else frozenset(paths),
        max_update_operations=capability.max_update_operations,
        concurrency=manifest.concurrency,
    )


# --------------------------------------------------------------------------------------
# The diff
# --------------------------------------------------------------------------------------


def compute_field_diff(
    *,
    entity_type: EntityType,
    source_envelopes: Mapping[str, FieldEnvelope[JsonValue]],
    target_envelopes: Mapping[str, FieldEnvelope[JsonValue]],
    manifest: CapabilityManifestBase,
    native_update_paths: Mapping[str, str] | None = None,
    endpoint: str | None = None,
) -> DiffPlan:
    """Plan the minimal set of field writes for one entity at one target.

    :param entity_type: The neutral entity type being synced.
    :param source_envelopes: Freshly read source envelopes, keyed by neutral field name.
        Only fields present here can produce a change; a field the source did not report
        is absent, not ``null``, and the target's copy is left alone. Each envelope must
        carry a checksum — build them with
        :func:`~qlabs_catalog_sync_sdk.envelope.build_field_envelopes` — or
        :class:`~qlabs_catalog_sync_sdk.envelope.CanonicalizationError` is raised, since
        an unchecksummed fresh envelope is a connector bug and guessing would defeat the
        idempotency the checksum exists to provide.
    :param target_envelopes: The last known state of the same entity at the target,
        keyed by neutral field name. Empty for an entity the target has never seen, in
        which case every writable field the source reports is planned — which is also
        how a dry run previews a first write.
    :param manifest: The **target** connector's capability manifest. Every shaping
        decision comes from here; nothing is special-cased per endpoint.
    :param native_update_paths: Optional neutral-field-name to native-wire-path mapping
        (``{"dataset_refs": "/datasetIds"}``). Supply it only when it is complete for
        this entity type: given it, the endpoint's ``allowed_update_paths`` enum is
        enforced and fails closed. Omitted, the enum is left to the connector that owns
        the translation.
    :param endpoint: The target endpoint key, for log context and error attribution.
    :returns: A :class:`DiffPlan`, possibly with an empty diff.
    :raises CapabilityError: when the manifest does not support ``entity_type``.
    """
    if not manifest.supports(entity_type):
        raise CapabilityError(
            f"target manifest does not support {entity_type.value!r}, so there is no "
            "diff to compute; an unsupported entity type is never scheduled for sync",
            endpoint=endpoint,
            entity_type=entity_type,
            operation="update",
        )

    constraints = _constraints(manifest, entity_type)
    changes: list[FieldChange] = []
    dropped: list[DroppedField] = []

    for name in changed_fields(source_envelopes, target_envelopes):
        source = source_envelopes[name]
        # The faithful projection, deliberately never `canonicalize`: this is the value
        # that reaches the endpoint, so its whitespace and array order must survive.
        desired = to_json_value(source.value)

        if not manifest.is_writable(entity_type, name):
            capability = constraints.fields.get(name)
            dropped.append(
                DroppedField(
                    field=name,
                    reason=(
                        DropReason.UNDECLARED
                        if capability is None
                        else _DROP_REASONS.get(capability.mode, DropReason.UNDECLARED)
                    ),
                    value=desired,
                    capability_mode=None if capability is None else capability.mode,
                )
            )
            continue

        if constraints.allowed_update_paths is not None and native_update_paths is not None:
            native_path = native_update_paths.get(name)
            if native_path is None or native_path not in constraints.allowed_update_paths:
                dropped.append(
                    DroppedField(
                        field=name,
                        reason=DropReason.NO_UPDATE_PATH,
                        value=desired,
                        capability_mode=FieldCapabilityMode.RW,
                        native_path=native_path,
                    )
                )
                continue

        previous = target_envelopes.get(name)
        changes.append(
            FieldChange(
                field=name,
                mode=(
                    FieldUpdateMode.REPLACE
                    if manifest.requires_full_replace(entity_type, name)
                    else FieldUpdateMode.PATCH
                ),
                value=desired,
                previous=None if previous is None else to_json_value(previous.value),
                # The source envelope: the provenance of the value being written, which
                # is what the state store persists once the write succeeds.
                envelope=source,
            )
        )

    revision, ambiguous = _expected_revision(changes, target_envelopes)
    plan = DiffPlan(
        diff=FieldDiff(entity_type=entity_type, changes=changes, expected_revision=revision),
        dropped=tuple(dropped),
        max_update_operations=constraints.max_update_operations,
        revision_ambiguous=ambiguous,
    )

    log = _LOG.bind(endpoint=endpoint, entity_type=entity_type.value)
    # Field names and counts only — a field *value* is customer metadata and has no
    # business in a log line; `DiffPlan.dropped` is the reporting channel for it.
    log.debug(
        "diff.computed",
        changed=list(plan.changed_field_names),
        dropped=list(plan.dropped_field_names),
        operations=plan.operation_count,
        max_operations=plan.max_update_operations,
        expected_revision=plan.expected_revision,
    )
    if ambiguous and constraints.concurrency in (ConcurrencyMode.ETAG, ConcurrencyMode.REVISION):
        log.warning(
            "diff.revision_ambiguous",
            changed=list(plan.changed_field_names),
            concurrency=constraints.concurrency,
        )
    return plan


def _expected_revision(
    changes: list[FieldChange],
    target_envelopes: Mapping[str, FieldEnvelope[JsonValue]],
) -> tuple[str | None, bool]:
    """The revision the diff is guarded against, and whether it was ambiguous.

    Only the changed fields' target envelopes are consulted: those are the reads the
    write would overwrite. Fields left alone can have been read at any revision without
    making this write unsafe.
    """
    revisions = {
        envelope.source_revision
        for envelope in (target_envelopes.get(change.field) for change in changes)
        if envelope is not None and envelope.source_revision is not None
    }
    if len(revisions) == 1:
        return revisions.pop(), False
    return None, len(revisions) > 1
