"""The RS-03 field tables are present in full, and the models validate what they promise."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qlabs_catalog_sync_sdk import models
from qlabs_catalog_sync_sdk.models import (
    AssetType,
    Category,
    DataProduct,
    DataProductStatus,
    Dataset,
    EntityType,
    GlossaryTerm,
    GlossaryTermStatus,
    IdentityRef,
    NeutralEntity,
    Party,
    PartyRole,
    Tag,
    TextField,
    TextFormat,
)

COMMON = {"neutral_id", "identities", "custom_attributes", "field_envelopes"}


def _fields(model: type[NeutralEntity]) -> set[str]:
    return set(model.model_fields)


def test_data_product_has_every_rs03_field() -> None:
    assert _fields(DataProduct) == COMMON | {
        "name",
        "description",
        "documentation",
        "status",
        "owners",
        "tags",
        "dataset_refs",
        "glossary_term_refs",
        "placement",
    }


def test_dataset_has_every_rs03_field() -> None:
    assert _fields(Dataset) == COMMON | {
        "name",
        "description",
        "owners",
        "tags",
        "classifications",
        "glossary_term_refs",
        "physical_ref",
        "asset_type",
    }


def test_glossary_term_has_every_rs03_field() -> None:
    assert _fields(GlossaryTerm) == COMMON | {
        "name",
        "definition",
        "abbreviation",
        "category_ref",
        "status",
        "tags",
        "stewards",
        "term_relations",
        "asset_links",
    }


def test_category_has_every_rs03_field() -> None:
    assert _fields(Category) == COMMON | {"name", "description", "parent_category_ref"}


def test_identity_ref_has_every_rs03_field() -> None:
    assert set(IdentityRef.model_fields) == {
        "endpoint",
        "entity_type",
        "native_key",
        "tenant_id",
        "secondary_keys",
    }


def test_party_and_tag_have_every_rs03_field() -> None:
    assert set(Party.model_fields) == {"party_id", "display_name", "email", "role"}
    assert set(Tag.model_fields) == {"key", "value"}


def test_neutral_status_enums_match_rs03() -> None:
    assert [s.value for s in DataProductStatus] == ["draft", "active", "deprecated", "archived"]
    assert [s.value for s in GlossaryTermStatus] == ["draft", "verified", "deprecated"]


def test_enums_serialize_as_readable_strings() -> None:
    assert DataProductStatus.ACTIVE == "active"
    assert EntityType.GLOSSARY_TERM == "glossary_term"
    assert AssetType.TABLE == "table"
    assert PartyRole.STEWARD == "steward"
    assert TextFormat.MARKDOWN == "markdown"
    product = DataProduct(name="p", status=DataProductStatus.DEPRECATED)
    assert product.model_dump(mode="json")["status"] == "deprecated"


def test_access_control_entities_do_not_exist() -> None:
    """v1 is upstream-only with no access-control sync (RM-01 scope guardrail)."""
    assert not hasattr(models, "Principal")
    assert not hasattr(models, "AccessBinding")
    assert "principal" not in {e.value for e in EntityType}
    assert "access_binding" not in {e.value for e in EntityType}


def test_every_entity_declares_its_entity_type() -> None:
    assert DataProduct.ENTITY_TYPE is EntityType.DATA_PRODUCT
    assert Dataset.ENTITY_TYPE is EntityType.DATASET
    assert GlossaryTerm.ENTITY_TYPE is EntityType.GLOSSARY_TERM
    assert Category.ENTITY_TYPE is EntityType.CATEGORY


def test_text_field_distinguishes_plain_from_markdown() -> None:
    assert TextField.plain("a").format is TextFormat.PLAIN
    assert TextField.markdown("a").format is TextFormat.MARKDOWN
    assert TextField(text="a").format is TextFormat.PLAIN
    assert TextField.plain("a") != TextField.markdown("a")


def test_identity_ref_requires_a_tenant() -> None:
    with pytest.raises(ValidationError):
        IdentityRef.model_validate(
            {"endpoint": "qlik", "entityType": "dataset", "nativeKey": "k"}
        )
    with pytest.raises(ValidationError):
        IdentityRef.model_validate(
            {"endpoint": "qlik", "entityType": "dataset", "nativeKey": "k", "tenantId": ""}
        )


def test_party_needs_at_least_one_identifier() -> None:
    with pytest.raises(ValidationError, match="at least one of"):
        Party(role=PartyRole.OWNER)
    assert Party(display_name="Team Finance", role=PartyRole.CONTACT).email is None


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Dataset.model_validate({"name": "orders", "lineage": ["upstream"]})


def test_entity_names_must_not_be_empty() -> None:
    for model in (DataProduct, Dataset, GlossaryTerm, Category):
        with pytest.raises(ValidationError):
            model.model_validate({"name": ""})


def test_both_spellings_of_a_field_are_accepted() -> None:
    by_alias = Dataset.model_validate({"name": "orders", "physicalRef": "main.retail.orders"})
    by_name = Dataset.model_validate({"name": "orders", "physical_ref": "main.retail.orders"})
    assert by_alias.physical_ref == by_name.physical_ref == "main.retail.orders"


def test_identity_lookup_by_endpoint(data_product: DataProduct) -> None:
    qlik = data_product.identity_for("qlik")
    assert qlik is not None
    assert qlik.native_key == "dp-9"
    assert data_product.identity_for("snowflake") is None


def test_neutral_ids_are_assigned_and_unique() -> None:
    first = Dataset(name="a")
    second = Dataset(name="a")
    assert first.neutral_id != second.neutral_id
