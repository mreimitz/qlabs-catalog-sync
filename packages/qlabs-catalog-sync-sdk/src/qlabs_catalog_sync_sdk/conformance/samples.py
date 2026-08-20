"""Synthetic neutral entities and field values for exercising a connector generically.

WP1 / T1.8. The conformance suite (:mod:`.suite`) knows nothing about any particular
connector's domain, so it cannot rely on connector-specific fixtures to build a
``DataProduct`` or a ``GlossaryTerm`` worth writing. This module is the one place that
maps every neutral field on every :class:`~qlabs_catalog_sync_sdk.models.NeutralEntity`
subclass to a small, deterministic value generator, keyed by field name (and, where the
same name means different things per entity — only ``status`` — by entity type too).

``variant`` distinguishes "the first value a test writes" (``0``) from "a second, always
genuinely different value" (any other integer) — the mechanism the round-trip and
idempotency checks use to prove an update actually changed something versus prove a
re-apply of the same value is a no-op.

Exported publicly (not just for :mod:`.suite`) because a connector author's own
conformance test file (T3.8, T4.6) may want a valid, throwaway entity for a specific
entity type without hand-rolling one field at a time.
"""

from __future__ import annotations

from typing import Final
from uuid import uuid4

from ..models import (
    AssetLink,
    AssetType,
    Category,
    DataProduct,
    DataProductStatus,
    Dataset,
    EntityType,
    GlossaryTerm,
    GlossaryTermStatus,
    NeutralEntity,
    Party,
    PartyRole,
    Tag,
    TermRelation,
    TextField,
)

__all__ = [
    "ENTITY_CLASSES",
    "entity_field_names",
    "sample_entity",
    "sample_value",
]

#: Every concrete neutral entity type the SDK defines, keyed by its ``EntityType``. Kept
#: here (rather than imported from :mod:`qlabs_catalog_sync_sdk.testing.fake_connector`,
#: which does not export its equivalent private mapping) so the conformance kit has no
#: dependency on the testing subpackage's internals — only on the public model surface.
ENTITY_CLASSES: Final[dict[EntityType, type[NeutralEntity]]] = {
    EntityType.DATA_PRODUCT: DataProduct,
    EntityType.DATASET: Dataset,
    EntityType.GLOSSARY_TERM: GlossaryTerm,
    EntityType.CATEGORY: Category,
}

#: ``NeutralEntity``'s own bookkeeping fields, excluded from every neutral field name
#: enumeration below — a capability manifest's per-entity ``fields`` dict is never keyed
#: by these.
_BASE_NEUTRAL_FIELDS: Final[frozenset[str]] = frozenset(NeutralEntity.model_fields)


def entity_field_names(entity_type: EntityType) -> tuple[str, ...]:
    """The neutral field names of ``entity_type``'s model, excluding the base bookkeeping
    fields every entity carries (``neutral_id``, ``identities``, ``custom_attributes``,
    ``field_envelopes``) — exactly what a capability manifest's ``fields`` dict for that
    entity type is keyed by.
    """
    entity_cls = ENTITY_CLASSES[entity_type]
    return tuple(name for name in entity_cls.model_fields if name not in _BASE_NEUTRAL_FIELDS)


def _status_value(entity_type: EntityType, variant: int) -> DataProductStatus | GlossaryTermStatus:
    if entity_type is EntityType.DATA_PRODUCT:
        return DataProductStatus.ACTIVE if variant == 0 else DataProductStatus.DEPRECATED
    return GlossaryTermStatus.DRAFT if variant == 0 else GlossaryTermStatus.VERIFIED


def sample_value(entity_type: EntityType, field: str, *, variant: int = 0) -> object:
    """A deterministic, validly-typed sample value for ``field`` of ``entity_type``.

    ``variant=0`` and ``variant=1`` always differ for a given ``(entity_type, field)``
    pair — that difference is what makes an update test meaningful. Values referencing a
    UUID (``dataset_refs``, ``category_ref``, ...) are always fresh regardless of
    ``variant``: a random UUID is already "a different value" from whatever came before.

    Raises :class:`KeyError` for a field name this module does not know — deliberately,
    so the conformance kit fails loudly on an entity type it has not been taught about
    rather than silently skipping a field.
    """
    generators: dict[str, object] = {
        "name": f"Conformance {entity_type.value} {variant}",
        "description": TextField.plain(f"Conformance description v{variant}."),
        "documentation": TextField.plain(f"Conformance documentation v{variant}."),
        "definition": TextField.plain(f"Conformance definition v{variant}."),
        "status": _status_value(entity_type, variant),
        "owners": [Party(display_name=f"Conformance Owner {variant}", role=PartyRole.OWNER)],
        "stewards": [Party(display_name=f"Conformance Steward {variant}", role=PartyRole.STEWARD)],
        "tags": [Tag(key="conformance", value=f"v{variant}")],
        "dataset_refs": [uuid4()],
        "glossary_term_refs": [uuid4()],
        "placement": f"spaces/conformance-{variant}",
        "classifications": [f"classification-{variant}"],
        "physical_ref": f"native/conformance/{variant}",
        "asset_type": AssetType.TABLE if variant == 0 else AssetType.VIEW,
        "abbreviation": f"CF{variant}",
        "category_ref": uuid4(),
        "parent_category_ref": uuid4(),
        "term_relations": [TermRelation(type="related_to", target_term_ref=uuid4())],
        "asset_links": [AssetLink(type="documents", target_ref=uuid4())],
    }
    if field not in generators:
        raise KeyError(
            f"conformance.samples has no value generator for field {field!r} of "
            f"{entity_type.value!r}; teach sample_value() about it"
        )
    return generators[field]


def sample_entity(entity_type: EntityType, *, variant: int = 0) -> NeutralEntity:
    """A fully-populated, validly-typed sample entity of ``entity_type``.

    Every neutral field the entity's model declares (per :func:`entity_field_names`) is
    set from :func:`sample_value`, so the entity is realistic enough for a connector's
    ``create`` to map every field, not just the one or two a narrower fixture might
    bother with.
    """
    entity_cls = ENTITY_CLASSES[entity_type]
    values = {
        field: sample_value(entity_type, field, variant=variant)
        for field in entity_field_names(entity_type)
    }
    return entity_cls.model_validate(values)
