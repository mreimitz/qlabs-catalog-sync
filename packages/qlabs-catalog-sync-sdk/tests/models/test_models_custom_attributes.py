"""customAttributes must round-trip losslessly, nested structures included."""

from __future__ import annotations

from typing import Any

import pytest

from qlabs_catalog_sync_sdk.models import (
    Category,
    DataProduct,
    Dataset,
    GlossaryTerm,
    NeutralEntity,
)

ENTITY_FIXTURES = ("data_product", "dataset", "glossary_term", "category")
ENTITY_TYPES: dict[str, type[NeutralEntity]] = {
    "data_product": DataProduct,
    "dataset": Dataset,
    "glossary_term": GlossaryTerm,
    "category": Category,
}


@pytest.mark.parametrize("fixture_name", ENTITY_FIXTURES)
def test_custom_attributes_survive_json_roundtrip(
    fixture_name: str, request: Any, nested_custom_attributes: dict[str, Any]
) -> None:
    entity: NeutralEntity = request.getfixturevalue(fixture_name)
    revived = ENTITY_TYPES[fixture_name].model_validate(entity.model_dump(mode="json"))
    assert revived.custom_attributes == nested_custom_attributes


def test_scalar_types_are_not_coerced() -> None:
    attributes: dict[str, Any] = {
        "flag": True,
        "count": 3,
        "ratio": 0.5,
        "text": "3",
        "nothing": None,
        "nested": {"list": [1, "1", True, None, {"deep": [[]]}]},
    }
    product = DataProduct(name="p", custom_attributes=attributes)
    revived = DataProduct.model_validate(product.model_dump(mode="json"))

    assert revived.custom_attributes == attributes
    assert isinstance(revived.custom_attributes["flag"], bool)
    assert isinstance(revived.custom_attributes["count"], int)
    assert not isinstance(revived.custom_attributes["count"], bool)
    assert isinstance(revived.custom_attributes["ratio"], float)
    assert isinstance(revived.custom_attributes["text"], str)
    assert revived.custom_attributes["nothing"] is None


def test_custom_attributes_default_to_empty_and_are_not_shared() -> None:
    first = Dataset(name="a")
    second = Dataset(name="b")
    first.custom_attributes["only-mine"] = 1
    assert second.custom_attributes == {}
