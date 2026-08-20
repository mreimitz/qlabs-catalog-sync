"""Capability manifest types — what a connector honestly declares it can do.

WP1 / T1.3. Defines the concrete shape T1.2's ``contract.CapabilityManifestBase``
anchors: :class:`FieldCapability` (a field's ``rw``/``ro``/``na`` mode, how it is
written, and whether it needs a full-value replace), :class:`EntityCapability` (whether
one entity type is supported at all, its identity keys, its per-field capabilities, the
closed update-path enum some endpoints impose, and whether the endpoint emits change
events), and :class:`CapabilityManifest` itself (the per-entity map plus the
connector-level concurrency strategy).

Import direction: this module imports from ``contract`` (the abstract anchor) and
``models`` (``EntityType``, ``NeutralModel``); it is never imported by either. The
engine plans field-level sync strictly from what is declared here — see RS-08 section 8
("capability negotiation") — so every default here is chosen to fail closed: an
undeclared field, entity, or manifest question always answers "not writable" rather than
silently permitting a write the connector cannot actually perform.

Where the pieces map to the research inputs:

* RS-08 section 4.2 is the shape this module implements almost verbatim, with
  ``Literal["rw","ro","na"]`` / ``Literal["etag","revision","none"]`` promoted to
  :class:`enum.StrEnum` to match every other enum in this SDK (``WatermarkKind``,
  ``ChangeKind``, ``HealthState``, ``models.FieldUpdateMode``, ...).
* RS-03 section 7 ("write semantics per endpoint") and section 8 (the per-field rw/ro/na
  tables) are what a real connector's ``fields`` dict encodes.
* The Qlik data-product PATCH path enum and its 8-operation cap
  (RS-02 ``qlik-two-way-sync-readiness.md`` section 2) are what
  :attr:`EntityCapability.allowed_update_paths` and
  :attr:`EntityCapability.max_update_operations` exist to express.

v1 guardrails this must express without special-casing (RM-01 decision D5, D6; agent
guide "v1 scope guardrails"): a read-only source connector marks every field ``ro``/
``na`` and never ``rw``; Databricks marks ``tags`` ``ro`` only when a SQL warehouse is
configured for that endpoint, ``na`` otherwise (D6) — a runtime choice made when
*building* a :class:`FieldCapability`, not a structural feature of the type; Qlik
declares ``GLOSSARY_TERM`` unsupported for the MVP (D5) by simply omitting it from
``entities`` or setting ``supported=False`` — again no special-casing needed. There is
no ``Principal``/``AccessBinding`` entity type to unsupport: ``models.EntityType``
(T1.1) never defines one, so the no-access-control-sync guardrail is enforced one layer
down and this module has nothing further to encode for it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from .contract import CapabilityManifestBase
from .models import EntityType, NeutralModel

__all__ = [
    "CapabilityManifest",
    "ConcurrencyMode",
    "EntityCapability",
    "FieldCapability",
    "FieldCapabilityMode",
]


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class FieldCapabilityMode(StrEnum):
    """A field's write posture at one endpoint, per RS-08 section 4.2.

    Named ``FieldCapabilityMode`` rather than the shorter ``FieldMode`` to keep it
    unambiguous next to ``models.FieldUpdateMode`` (``FieldChange``'s patch-vs-replace
    write *intent*) — a different question about a different object, both importable
    from the same package.

    ``NA`` and ``RO`` are deliberately distinct even though the engine treats both as
    "never write this": ``RO`` means the endpoint can express the field but only ever
    returns it (Qlik ``secureQri``, Databricks ``physical_ref``); ``NA`` means either the
    endpoint has no native equivalent at all (Databricks has no glossary), or — for a
    field that could exist but this endpoint *instance* cannot currently even read
    (Databricks ``tags`` with no SQL warehouse configured, decision D6) — is unavailable
    full stop. Keeping them distinct lets the run report and connector documentation
    explain *why* a field never changes, rather than collapsing both into one
    uninformative "not writable".
    """

    RW = "rw"
    RO = "ro"
    NA = "na"


class ConcurrencyMode(StrEnum):
    """How a connector's write path guards against a lost update, per RS-08 section 4.2.

    Connector-level, not per-field: an endpoint's optimistic-concurrency story is a
    property of its write API as a whole (Qlik sends the ETag it last read back as
    ``if-match``), not of any one field.
    """

    ETAG = "etag"
    REVISION = "revision"
    NONE = "none"


# --------------------------------------------------------------------------------------
# FieldCapability
# --------------------------------------------------------------------------------------


class FieldCapability(NeutralModel):
    """What one endpoint can do with one neutral field of one entity type.

    ``mode`` has no default: every field an endpoint declares must be a deliberate
    choice, never a silently-assumed one — that is what makes the manifest safe for the
    engine to plan writes from. Prefer the :meth:`rw`/:meth:`ro`/:meth:`na` builders over
    the raw constructor; they read as the decision they encode.

    ``normalized_by_target`` says the endpoint stores a *lossy projection* of the neutral
    value, so a read-back is not byte-identical to what was written even when the write
    fully succeeded. Qlik's ``readMe`` has no plain-vs-markdown concept and its ``tags``
    are flat strings where the neutral ``Tag`` is a key/value pair — both are facts about
    Qlik, not connector defects. Declaring it lets the conformance kit hold such a field
    to "the substance survived" instead of "the bytes matched", and lets the engine
    explain a field that never quite settles. It is deliberately **not** a licence to skip
    a check: a field that could round-trip exactly and does not is still a defect.
    """

    mode: FieldCapabilityMode
    normalized_by_target: bool = False

    writable_via: str | None = None
    """How a write reaches the endpoint, e.g. ``"rest-patch"``, ``"sql-ddl"``,
    ``"lifecycle-action"``. Informational — the connector, not the engine, uses this to
    route the write — so it is only meaningful when ``mode`` is
    :attr:`FieldCapabilityMode.RW`, and validated accordingly below."""

    partial_update: bool = True
    """``True``: the endpoint can patch this field in place. ``False``: the writer must
    send the complete new value (read-modify-write full replacement) — Qlik's
    data-product arrays (``datasetIds``, ``glossaryIds``, ``tags``, ``keyContacts``) are
    the motivating case (RS-03 section 7)."""

    @model_validator(mode="after")
    def _writable_via_only_when_writable(self) -> FieldCapability:
        if self.mode is not FieldCapabilityMode.RW and self.writable_via is not None:
            raise ValueError(
                f"writable_via is meaningless for a {self.mode.value!r} field "
                f"(got {self.writable_via!r}); only an rw field can declare how it is written"
            )
        return self

    @classmethod
    def rw(
        cls,
        *,
        writable_via: str | None = None,
        partial_update: bool = True,
        normalized_by_target: bool = False,
    ) -> FieldCapability:
        """A field this endpoint can write."""
        return cls(
            mode=FieldCapabilityMode.RW,
            writable_via=writable_via,
            partial_update=partial_update,
            normalized_by_target=normalized_by_target,
        )

    @classmethod
    def ro(cls, *, normalized_by_target: bool = False) -> FieldCapability:
        """A field this endpoint can only ever read."""
        return cls(mode=FieldCapabilityMode.RO, normalized_by_target=normalized_by_target)

    @classmethod
    def na(cls) -> FieldCapability:
        """A field this endpoint has no native equivalent for (or cannot even read)."""
        return cls(mode=FieldCapabilityMode.NA)

    @property
    def is_writable(self) -> bool:
        """True when this field is declared ``rw``."""
        return self.mode is FieldCapabilityMode.RW

    @property
    def requires_full_replace(self) -> bool:
        """True when writing this field needs the complete new value.

        Gated on :attr:`is_writable` — a non-writable field never "requires" a replace
        strategy because it is never written at all — mirroring how
        :meth:`CapabilityManifest.requires_full_replace` gates on the same thing.
        """
        return self.is_writable and not self.partial_update


# --------------------------------------------------------------------------------------
# EntityCapability
# --------------------------------------------------------------------------------------


class EntityCapability(NeutralModel):
    """What one endpoint can do with one neutral entity type as a whole.

    ``supported=False`` is itself meaningful, not just "absent from the manifest" — it
    lets a connector document *that it considered* an entity type and deliberately
    excluded it (Qlik's ``GLOSSARY_TERM`` for the v1 MVP, decision D5) rather than
    leaving a reader to guess whether the omission was an oversight.
    """

    supported: bool = True

    identity_keys: list[str] = Field(default_factory=list)
    """The native key(s) that identify one object of this entity type at this endpoint,
    e.g. ``["secure_qri"]`` for a Qlik dataset, ``["full_name", "object_id"]`` for a
    Databricks table. Required (non-empty) for a supported entity — the engine cannot
    resolve identity for one it can't."""

    fields: dict[str, FieldCapability] = Field(default_factory=dict)
    """Neutral field name to its capability. A field this entity type has in the neutral
    model but which is absent from this dict is treated exactly like ``na`` by
    :meth:`CapabilityManifest.is_writable` — see that method's docstring for why."""

    supports_events: bool = False
    """True when the endpoint pushes change notifications for this entity type instead
    of requiring a poll. Always ``False`` for every v1 endpoint — Qlik publishes no
    webhooks for items, datasets, data products or glossaries — so the engine always
    polls via ``list_changed`` in the MVP; this field exists so a future event-capable
    endpoint needs no manifest shape change."""

    allowed_update_paths: list[str] | None = None
    """The closed set of native update paths (JSON Pointers) this entity type's write
    endpoint accepts, when the endpoint imposes one. Qlik's data-product PATCH is the
    motivating case: JSON Patch ``op: "replace"`` only, against exactly ``/name``,
    ``/description``, ``/datasetIds``, ``/glossaryIds``, ``/readMe``, ``/keyContacts``,
    ``/tags``, ``/apiConsumableDatasetIds`` (RS-02 ``qlik-two-way-sync-readiness.md``
    section 2). ``None`` means the endpoint imposes no such enum — an open PATCH
    surface, or a read-only/unsupported entity type.

    These are the endpoint's *native* wire paths, not neutral field names (Qlik's
    ``/datasetIds`` is the neutral ``dataset_refs``); the connector, not the engine or
    this manifest, owns that translation. This lives on the entity rather than on
    :class:`FieldCapability` because the enum constrains one *request* against the whole
    entity, not any single field in isolation.
    """

    max_update_operations: int | None = None
    """The maximum number of update operations one write request to this entity type may
    carry, when the endpoint caps it (Qlik data-product PATCH: 8, matching its 8-path
    enum above). Lives beside :attr:`allowed_update_paths` rather than on
    :class:`CapabilityManifest` (the cap is per entity type, not connector-wide — two
    entity types on the same connector can sit behind different endpoints with different
    caps) or on :class:`FieldCapability` (the cap bounds the whole request, not any one
    field in it).
    """

    @model_validator(mode="after")
    def _supported_entity_needs_identity_keys(self) -> EntityCapability:
        if self.supported and not self.identity_keys:
            raise ValueError("a supported entity capability must declare identity_keys")
        if len(set(self.identity_keys)) != len(self.identity_keys):
            raise ValueError(f"identity_keys must not repeat: {self.identity_keys!r}")
        return self

    @model_validator(mode="after")
    def _allowed_update_paths_are_a_real_closed_enum(self) -> EntityCapability:
        paths = self.allowed_update_paths
        if paths is not None:
            if not paths:
                raise ValueError(
                    "allowed_update_paths must be omitted (None), not an empty list, when "
                    "an endpoint imposes no closed path enum"
                )
            if len(set(paths)) != len(paths):
                raise ValueError(f"allowed_update_paths must not repeat: {paths!r}")
            not_pointers = [path for path in paths if not path.startswith("/")]
            if not_pointers:
                raise ValueError(f"allowed_update_paths must be JSON Pointers: {not_pointers!r}")
        if self.max_update_operations is not None:
            if self.max_update_operations < 1:
                raise ValueError("max_update_operations must be at least 1")
            if paths is not None and self.max_update_operations > len(paths):
                raise ValueError(
                    f"max_update_operations ({self.max_update_operations}) cannot exceed "
                    f"the number of allowed_update_paths ({len(paths)})"
                )
        return self


# --------------------------------------------------------------------------------------
# CapabilityManifest
# --------------------------------------------------------------------------------------


class CapabilityManifest(CapabilityManifestBase):
    """The concrete manifest every connector's ``capabilities()`` returns.

    Answers the three questions ``contract.CapabilityManifestBase`` requires
    (:meth:`supports`, :meth:`is_writable`, :meth:`requires_full_replace`) by looking
    them up in :attr:`entities`, and carries the connector-level :attr:`concurrency`
    strategy the engine uses to decide whether to send ``if-match``.

    Every lookup here fails closed: an entity type or field this manifest simply does
    not mention answers exactly like one explicitly marked ``na`` — never raises, never
    guesses ``rw``. See :meth:`is_writable` for the deliberate reasoning behind the
    undeclared-field case specifically.
    """

    entities: dict[EntityType, EntityCapability] = Field(default_factory=dict)
    concurrency: ConcurrencyMode = ConcurrencyMode.NONE

    def supports(self, entity_type: EntityType) -> bool:
        capability = self.entities.get(entity_type)
        return capability is not None and capability.supported

    def is_writable(self, entity_type: EntityType, field: str) -> bool:
        """True only for a field explicitly declared ``rw`` on a supported entity.

        False for a field declared ``ro``/``na``, for any field of an unsupported (or
        never-mentioned) entity type, and — deliberately — for a field the entity's
        capability does not mention at all. An undeclared field is not "unknown
        capability, assume writable"; it is treated exactly like ``na``, because the
        alternative (silence as permission) is precisely the dishonest-manifest failure
        mode this type exists to prevent: a connector that grew a new neutral field
        without updating its manifest would otherwise silently gain a write path the
        engine believes exists and the connector cannot actually serve.
        """
        capability = self.entities.get(entity_type)
        if capability is None or not capability.supported:
            return False
        field_capability = capability.fields.get(field)
        return field_capability is not None and field_capability.is_writable

    def requires_full_replace(self, entity_type: EntityType, field: str) -> bool:
        capability = self.entities.get(entity_type)
        if capability is None or not capability.supported:
            return False
        field_capability = capability.fields.get(field)
        if field_capability is None:
            return False
        return field_capability.requires_full_replace

    def entity_capability(self, entity_type: EntityType) -> EntityCapability | None:
        """The declared capability for ``entity_type``, or ``None`` if never mentioned."""
        return self.entities.get(entity_type)

    def field_capability(self, entity_type: EntityType, field: str) -> FieldCapability | None:
        """The declared capability for one field, or ``None`` if never mentioned."""
        capability = self.entities.get(entity_type)
        return None if capability is None else capability.fields.get(field)

    @property
    def supported_entity_types(self) -> frozenset[EntityType]:
        """Every entity type this manifest declares ``supported``."""
        return frozenset(
            entity_type
            for entity_type, capability in self.entities.items()
            if capability.supported
        )
