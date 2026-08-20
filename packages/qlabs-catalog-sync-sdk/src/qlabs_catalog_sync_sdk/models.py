"""Neutral metadata model types (pydantic v2).

WP1 / T1.1 (foundational). Defines the vendor-neutral entities and value types every
connector maps to and from, exactly as tabled in the RS-03 neutral metadata model
specification (sections 3 and 5): :class:`DataProduct`, :class:`Dataset`,
:class:`GlossaryTerm`, :class:`Category`, :class:`Party`, :class:`Tag`,
:class:`TextField`, :class:`IdentityRef`, :class:`FieldEnvelope` and
:class:`FieldDiff`.

Conventions that bind every downstream package:

* Python attributes are ``snake_case``; every model also accepts and can emit the
  ``camelCase`` spelling used by the RS-03 tables (``model_dump(by_alias=True)``).
* Enums are :class:`enum.StrEnum` so JSON stays stable and human-readable.
* Every model round-trips: ``M.model_validate(m.model_dump(mode="json")) == m``.
* Provenance lives beside the value. Entity fields carry plain neutral values (so
  mapping code stays readable) and each entity carries a ``field_envelopes`` sidecar
  of JSON-normalized :class:`FieldEnvelope` records keyed by neutral field name.
* Checksum *computation* is not defined here — ``envelope.py`` (T1.6) owns canonical
  normalization and hashing. This module only declares the envelope's shape.

Scope note: v1 is upstream-only with no access-control sync, so no ``Principal`` or
``AccessBinding`` entity exists by design.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Generic, TypeVar
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator
from pydantic.alias_generators import to_camel

__all__ = [
    "AssetLink",
    "AssetType",
    "Category",
    "DataProduct",
    "DataProductStatus",
    "Dataset",
    "EntityType",
    "FieldChange",
    "FieldDiff",
    "FieldEnvelope",
    "FieldUpdateMode",
    "GlossaryTerm",
    "GlossaryTermStatus",
    "IdentityRef",
    "NeutralEntity",
    "NeutralModel",
    "Party",
    "PartyRole",
    "Tag",
    "TermRelation",
    "TextField",
    "TextFormat",
]


# --------------------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------------------


class NeutralModel(BaseModel):
    """Base for every neutral model type.

    Accepts both the Python (``snake_case``) and the RS-03 wire (``camelCase``)
    spelling of every field, rejects unknown keys, and round-trips through
    ``model_dump(mode="json")`` in either spelling.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class EntityType(StrEnum):
    """The neutral entity types the engine synchronizes.

    v1 is upstream-only with no access-control sync: there is deliberately no
    ``PRINCIPAL`` or ``ACCESS_BINDING`` member.
    """

    DATA_PRODUCT = "data_product"
    DATASET = "dataset"
    GLOSSARY_TERM = "glossary_term"
    CATEGORY = "category"


class TextFormat(StrEnum):
    """How the body of a :class:`TextField` should be interpreted."""

    PLAIN = "plain"
    MARKDOWN = "markdown"


class DataProductStatus(StrEnum):
    """Neutral data-product lifecycle status (RS-03 section 3.1)."""

    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class GlossaryTermStatus(StrEnum):
    """Neutral glossary-term status (RS-03 section 3.3)."""

    DRAFT = "draft"
    VERIFIED = "verified"
    DEPRECATED = "deprecated"


class AssetType(StrEnum):
    """Neutral dataset/asset kind (RS-03 section 3.2).

    ``VOLUME`` covers the spec's combined "file/volume" case (Databricks volumes,
    file-backed assets); anything else a source reports maps to ``OTHER``.
    """

    TABLE = "table"
    VIEW = "view"
    VOLUME = "volume"
    DATASET = "dataset"
    OTHER = "other"


class PartyRole(StrEnum):
    """Why a :class:`Party` is attached to an entity.

    Owners are best-effort descriptive metadata in v1, never an identity system.
    """

    OWNER = "owner"
    STEWARD = "steward"
    CONTACT = "contact"
    OTHER = "other"


class FieldUpdateMode(StrEnum):
    """Write intent for a single field in a :class:`FieldDiff`.

    ``PATCH`` means the endpoint can mutate the field in place. ``REPLACE`` means the
    writer must send the complete new value (read-modify-write), which is what
    endpoints requiring full-array replacement need — notably Qlik data-product arrays
    and Databricks metric views (RS-03 section 7).
    """

    PATCH = "patch"
    REPLACE = "replace"


# --------------------------------------------------------------------------------------
# Shared value types (RS-03 section 3)
# --------------------------------------------------------------------------------------


class TextField(NeutralModel):
    """A text value that knows whether it is plain text or markdown."""

    text: str = ""
    format: TextFormat = TextFormat.PLAIN

    @classmethod
    def plain(cls, text: str) -> TextField:
        """Build a plain-text field."""
        return cls(text=text, format=TextFormat.PLAIN)

    @classmethod
    def markdown(cls, text: str) -> TextField:
        """Build a markdown field."""
        return cls(text=text, format=TextFormat.MARKDOWN)


class Tag(NeutralModel):
    """A key/value tag. ``value`` is ``None`` for key-only (label) tags."""

    key: str = Field(min_length=1)
    value: str | None = None


class Party(NeutralModel):
    """A person or team attached to an entity, with the role they play.

    At least one of ``party_id``, ``email`` or ``display_name`` must be present; a
    party with no identifying attribute at all carries no information.
    """

    party_id: str | None = None
    display_name: str | None = None
    email: str | None = None
    role: PartyRole

    @model_validator(mode="after")
    def _require_an_identifier(self) -> Party:
        if not (self.party_id or self.email or self.display_name):
            raise ValueError("Party needs at least one of party_id, email or display_name")
        return self


class IdentityRef(NeutralModel):
    """A native key for one entity at one endpoint.

    ``tenant_id`` is always required: native keys are only unique within a tenant or
    account (RS-03 section 4). ``secondary_keys`` carries the additional native keys an
    endpoint exposes for the same object — for example Qlik's ``id`` and ``resourceId``
    alongside the primary ``secureQri``.
    """

    endpoint: str = Field(min_length=1)
    entity_type: EntityType
    native_key: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    secondary_keys: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Field envelope (RS-03 section 5)
# --------------------------------------------------------------------------------------


ValueT = TypeVar("ValueT")


class FieldEnvelope(NeutralModel, Generic[ValueT]):
    """Provenance wrapper around a single field value.

    ``checksum`` is a hash of the *normalized* value; computing it is
    ``envelope.py``'s job (T1.6), not this module's, so it stays optional here and is
    filled in by the envelope utilities. ``source_revision`` and ``last_modified_at``
    are optional because not every endpoint exposes field-level provenance (Databricks
    is snapshot + checksum only).
    """

    value: ValueT
    source_endpoint: str = Field(min_length=1)
    source_revision: str | None = None
    last_modified_at: AwareDatetime | None = None
    last_synced_at: AwareDatetime | None = None
    checksum: str | None = None


# --------------------------------------------------------------------------------------
# Field diff (RS-03 sections 7 and 10; consumed by the connector contract, T1.2)
# --------------------------------------------------------------------------------------


class FieldChange(NeutralModel):
    """One field's desired change, carrying the writer's replace-vs-patch intent.

    ``value`` and ``previous`` are JSON-normalized neutral values (what
    ``model_dump(mode="json")`` produces for a typed neutral value such as
    :class:`TextField` or ``list[Tag]``). Keeping the diff in JSON form is what makes
    it checksummable, loggable and directly usable by a JSON Patch writer.
    """

    field: str = Field(min_length=1)
    mode: FieldUpdateMode = FieldUpdateMode.PATCH
    value: JsonValue = None
    previous: JsonValue = None
    envelope: FieldEnvelope[JsonValue] | None = None

    @property
    def is_full_replace(self) -> bool:
        """True when the endpoint must be sent the complete new value."""
        return self.mode is FieldUpdateMode.REPLACE


class FieldDiff(NeutralModel):
    """The minimal set of field changes to apply to one entity at one endpoint.

    ``expected_revision`` carries the ETag / revision counter the change was computed
    against, for endpoints that declare optimistic concurrency.
    """

    entity_type: EntityType
    changes: list[FieldChange] = Field(default_factory=list)
    expected_revision: str | None = None

    @model_validator(mode="after")
    def _reject_duplicate_fields(self) -> FieldDiff:
        seen: set[str] = set()
        for change in self.changes:
            if change.field in seen:
                raise ValueError(f"duplicate field in diff: {change.field!r}")
            seen.add(change.field)
        return self

    @property
    def field_names(self) -> list[str]:
        """The neutral field names this diff touches, in order."""
        return [change.field for change in self.changes]

    def change_for(self, field: str) -> FieldChange | None:
        """The change for ``field``, or ``None`` when the diff does not touch it."""
        for change in self.changes:
            if change.field == field:
                return change
        return None

    def requires_full_replace(self, field: str) -> bool:
        """True when ``field`` is in this diff and must be written as a full replace."""
        change = self.change_for(field)
        return change is not None and change.is_full_replace


# --------------------------------------------------------------------------------------
# Entities (RS-03 section 3)
# --------------------------------------------------------------------------------------


class NeutralEntity(NeutralModel):
    """Fields common to every neutral entity.

    ``neutral_id`` is engine-assigned and stable; a connector that reads an entity gets
    a fresh id which the engine replaces with the one bound in the IdentityMap.

    ``custom_attributes`` preserves endpoint-specific extras round-trip: whatever JSON
    goes in comes out unchanged, nested structures included.

    ``field_envelopes`` is the provenance sidecar — neutral field name to the
    :class:`FieldEnvelope` the value was read in. It is empty for entities built by
    hand and populated by connectors on ``read``.
    """

    ENTITY_TYPE: ClassVar[EntityType]

    neutral_id: UUID = Field(default_factory=uuid4)
    identities: list[IdentityRef] = Field(default_factory=list)
    custom_attributes: dict[str, JsonValue] = Field(default_factory=dict)
    field_envelopes: dict[str, FieldEnvelope[JsonValue]] = Field(default_factory=dict)

    def identity_for(self, endpoint: str) -> IdentityRef | None:
        """This entity's native identity at ``endpoint``, if it has one."""
        for ref in self.identities:
            if ref.endpoint == endpoint:
                return ref
        return None

    def envelope_for(self, field: str) -> FieldEnvelope[JsonValue] | None:
        """The provenance envelope recorded for ``field``, if any."""
        return self.field_envelopes.get(field)


class DataProduct(NeutralEntity):
    """A curated, first-class data product (RS-03 section 3.1).

    Distribution constructs (Delta shares, Marketplace/Snowflake listings) are
    projections of this entity, never the entity itself.
    """

    ENTITY_TYPE: ClassVar[EntityType] = EntityType.DATA_PRODUCT

    name: str = Field(min_length=1)
    description: TextField | None = None
    documentation: TextField | None = None
    status: DataProductStatus | None = None
    owners: list[Party] = Field(default_factory=list)
    tags: list[Tag] = Field(default_factory=list)
    dataset_refs: list[UUID] = Field(default_factory=list)
    glossary_term_refs: list[UUID] = Field(default_factory=list)
    placement: str | None = None


class Dataset(NeutralEntity):
    """A dataset / asset composing a data product (RS-03 section 3.2)."""

    ENTITY_TYPE: ClassVar[EntityType] = EntityType.DATASET

    name: str = Field(min_length=1)
    description: TextField | None = None
    owners: list[Party] = Field(default_factory=list)
    tags: list[Tag] = Field(default_factory=list)
    classifications: list[str] = Field(default_factory=list)
    glossary_term_refs: list[UUID] = Field(default_factory=list)
    physical_ref: str | None = None
    asset_type: AssetType = AssetType.OTHER


class TermRelation(NeutralModel):
    """A typed link from one glossary term to another (Qlik ``relatesTo`` and friends).

    ``type`` stays an open string: the relation vocabulary differs per endpoint (Qlik
    ships 14 types, Collibra has its own), and the engine carries it verbatim.
    """

    type: str = Field(min_length=1)
    target_term_ref: UUID


class AssetLink(NeutralModel):
    """A typed link from a glossary term to a dataset or data product."""

    type: str = Field(min_length=1)
    target_ref: UUID


class GlossaryTerm(NeutralEntity):
    """A business glossary term (RS-03 section 3.3)."""

    ENTITY_TYPE: ClassVar[EntityType] = EntityType.GLOSSARY_TERM

    name: str = Field(min_length=1)
    definition: TextField | None = None
    abbreviation: str | None = None
    category_ref: UUID | None = None
    status: GlossaryTermStatus | None = None
    tags: list[Tag] = Field(default_factory=list)
    stewards: list[Party] = Field(default_factory=list)
    term_relations: list[TermRelation] = Field(default_factory=list)
    asset_links: list[AssetLink] = Field(default_factory=list)


class Category(NeutralEntity):
    """A glossary category / domain (RS-03 section 3.4)."""

    ENTITY_TYPE: ClassVar[EntityType] = EntityType.CATEGORY

    name: str = Field(min_length=1)
    description: TextField | None = None
    parent_category_ref: UUID | None = None
