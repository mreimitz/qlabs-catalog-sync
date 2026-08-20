"""`CapabilityManifestBase` is the anchor T1.3's concrete manifest must satisfy."""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.contract import CapabilityManifestBase, EntityType


def test_the_anchor_itself_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        CapabilityManifestBase()  # type: ignore[abstract]


def test_a_manifest_missing_a_required_question_cannot_be_instantiated() -> None:
    class Partial(CapabilityManifestBase):
        def supports(self, entity_type: EntityType) -> bool:
            return True

        def is_writable(self, entity_type: EntityType, field: str) -> bool:
            return True

    with pytest.raises(TypeError, match="requires_full_replace"):
        Partial()  # type: ignore[abstract]


def test_a_concrete_manifest_answers_all_three_questions(
    source_connector, target_connector
) -> None:
    source = source_connector.capabilities()
    target = target_connector.capabilities()

    assert isinstance(source, CapabilityManifestBase)

    assert source.supports(EntityType.DATA_PRODUCT)
    assert not source.supports(EntityType.GLOSSARY_TERM)
    assert not source.is_writable(EntityType.DATA_PRODUCT, "description")

    assert target.is_writable(EntityType.DATA_PRODUCT, "description")
    assert not target.is_writable(EntityType.DATA_PRODUCT, "owners")
    assert target.requires_full_replace(EntityType.DATA_PRODUCT, "tags")
    assert not target.requires_full_replace(EntityType.DATA_PRODUCT, "description")


def test_a_manifest_is_a_serializable_neutral_model(target_connector) -> None:
    dumped = target_connector.capabilities().model_dump(mode="json")

    assert dumped["supported"] == ["data_product"]
