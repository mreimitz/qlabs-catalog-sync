"""The synthetic sample-entity/value factory: every neutral field of every entity type
produces a validly-typed value, and ``variant=0``/``variant=1`` always genuinely differ —
the property the round-trip and idempotency checks in ``suite.py`` are built on.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.conformance.samples import (
    ENTITY_CLASSES,
    entity_field_names,
    sample_entity,
    sample_value,
)
from qlabs_catalog_sync_sdk.envelope import to_json_value
from qlabs_catalog_sync_sdk.models import EntityType


@pytest.mark.parametrize("entity_type", list(EntityType))
def test_sample_entity_validates_for_every_entity_type(entity_type: EntityType) -> None:
    entity = sample_entity(entity_type)
    assert isinstance(entity, ENTITY_CLASSES[entity_type])
    assert entity.name  # every neutral entity requires a non-empty name


@pytest.mark.parametrize("entity_type", list(EntityType))
def test_every_declared_field_has_a_sample_value(entity_type: EntityType) -> None:
    for field in entity_field_names(entity_type):
        value = sample_value(entity_type, field)
        assert value is not None


@pytest.mark.parametrize("entity_type", list(EntityType))
def test_variant_0_and_variant_1_always_differ(entity_type: EntityType) -> None:
    for field in entity_field_names(entity_type):
        first = to_json_value(sample_value(entity_type, field, variant=0))
        second = to_json_value(sample_value(entity_type, field, variant=1))
        assert first != second, f"{entity_type.value}.{field} did not vary between variants"


def test_an_unknown_field_raises_instead_of_silently_skipping() -> None:
    with pytest.raises(KeyError):
        sample_value(EntityType.DATA_PRODUCT, "not_a_real_field")
