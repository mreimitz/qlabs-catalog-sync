"""End to end: a realistic Snowflake row maps to the right neutral entity fields when
assembled the way ``read.py`` assembles them.

Built here from ``mapping.py``'s public functions plus the SDK rather than by calling
``read.py``, so this suite proves the mapping contract on its own -- mirroring how the
Databricks connector's ``tests/mapping/test_content_fields.py`` stays independent of its
read path.
"""

from __future__ import annotations

from typing import Any

from qlabs_catalog_sync_sdk.envelope import build_field_envelopes
from qlabs_catalog_sync_sdk.models import (
    AssetType,
    DataProduct,
    DataProductStatus,
    Dataset,
    EntityType,
    IdentityRef,
    TextFormat,
)
from qlabs_connector_snowflake.mapping import (
    MAPPED_LISTING_FIELDS,
    MAPPED_SOURCE_FIELDS,
    asset_type_for_object,
    map_custom_attributes,
    map_data_product_fields,
    map_dataset_fields,
    map_listing_fields,
)

from .conftest import (
    ENDPOINT,
    TENANT_ID,
    make_raw_listing,
    make_raw_schema,
    make_raw_table,
    system_tag,
    user_tag,
)

_OBJECT_STRUCTURAL_FIELDS = (
    frozenset({"TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME"}) | MAPPED_SOURCE_FIELDS
)
_SCHEMA_STRUCTURAL_FIELDS = frozenset({"CATALOG_NAME", "SCHEMA_NAME"}) | MAPPED_SOURCE_FIELDS
_LISTING_STRUCTURAL_FIELDS = frozenset({"global_name", "name"}) | MAPPED_LISTING_FIELDS


def _ref(entity_type: EntityType, native_key: str, **secondary: str) -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=entity_type,
        native_key=native_key,
        tenant_id=TENANT_ID,
        secondary_keys=secondary,
    )


def test_a_realistic_table_row_maps_to_the_right_dataset_fields() -> None:
    raw = make_raw_table()
    references = [user_tag("COST_CENTER", "commerce"), system_tag("PRIVACY_CATEGORY", "IDENTIFIER")]
    custom_attributes = map_custom_attributes(raw, exclude=_OBJECT_STRUCTURAL_FIELDS)
    content = map_dataset_fields(raw, tag_references=references)
    asset_type = asset_type_for_object(raw)
    values: dict[str, Any] = {
        "name": raw["TABLE_NAME"],
        "asset_type": asset_type,
        "custom_attributes": custom_attributes,
        **content,
    }

    dataset = Dataset(
        identities=[_ref(EntityType.DATASET, "SALES_DB.PUBLIC.ORDERS")],
        name=str(raw["TABLE_NAME"]),
        asset_type=asset_type,
        custom_attributes=custom_attributes,
        field_envelopes=build_field_envelopes(values, source_endpoint=ENDPOINT),
        **content,
    )

    assert dataset.description is not None
    assert dataset.description.text == "Order header rows, one per checkout."
    assert dataset.description.format is TextFormat.PLAIN
    assert [party.display_name for party in dataset.owners] == ["SALES_ENGINEER"]
    assert dataset.owners[0].email is None
    assert dataset.physical_ref == "SALES_DB.PUBLIC.ORDERS"
    assert dataset.asset_type is AssetType.TABLE
    assert [tag.key for tag in dataset.tags] == ["GOVERNANCE.TAGS.COST_CENTER"]
    assert dataset.classifications == ["PRIVACY_CATEGORY=IDENTIFIER"]
    # Manifest `na` for this entity -- never invented.
    assert dataset.glossary_term_refs == []
    # The native kind still round-trips even though asset_type collapsed it.
    assert dataset.custom_attributes["TABLE_TYPE"] == "BASE TABLE"
    assert "COMMENT" not in dataset.custom_attributes


def test_a_realistic_schema_row_maps_to_the_right_data_product_fields() -> None:
    raw = make_raw_schema()
    custom_attributes = map_custom_attributes(raw, exclude=_SCHEMA_STRUCTURAL_FIELDS)
    content = map_data_product_fields(raw, tag_references=[user_tag("DOMAIN", "sales")])
    values: dict[str, Any] = {
        "name": raw["SCHEMA_NAME"],
        "custom_attributes": custom_attributes,
        **content,
    }

    product = DataProduct(
        identities=[_ref(EntityType.DATA_PRODUCT, "SALES_DB.PUBLIC")],
        name=str(raw["SCHEMA_NAME"]),
        custom_attributes=custom_attributes,
        field_envelopes=build_field_envelopes(values, source_endpoint=ENDPOINT),
        **content,
    )

    assert product.description is not None
    assert product.description.text == "Conformed sales dimensions and facts."
    assert [party.display_name for party in product.owners] == ["SYSADMIN"]
    assert [tag.key for tag in product.tags] == ["GOVERNANCE.TAGS.DOMAIN"]
    # A Snowflake schema has no long-form doc, no lifecycle and no placement.
    assert product.documentation is None
    assert product.status is None
    assert product.placement is None
    # Membership is resolved by the sync loop through the IdentityMap, not here.
    assert product.dataset_refs == []


def test_a_realistic_listing_row_maps_to_the_right_data_product_fields() -> None:
    raw = make_raw_listing()
    custom_attributes = map_custom_attributes(raw, exclude=_LISTING_STRUCTURAL_FIELDS)
    content = map_listing_fields(raw)
    values: dict[str, Any] = {
        "name": raw["title"],
        "custom_attributes": custom_attributes,
        **content,
    }

    product = DataProduct(
        identities=[
            _ref(EntityType.DATA_PRODUCT, "GZTSZAS2KH9", listing_name="SALES_DAILY"),
        ],
        name=str(raw["title"]),
        custom_attributes=custom_attributes,
        field_envelopes=build_field_envelopes(values, source_endpoint=ENDPOINT),
        **content,
    )

    assert product.name == "Daily sales"
    assert product.description is not None
    assert product.description.text == "Daily sales by region, refreshed nightly"
    assert product.description.format is TextFormat.PLAIN
    assert product.documentation is not None
    assert product.documentation.format is TextFormat.MARKDOWN
    assert product.documentation.text.startswith("# Daily sales")
    assert product.status is DataProductStatus.ACTIVE
    assert [party.display_name for party in product.owners] == ["SALES_PROVIDER"]
    # RS-05 2.2 metadata with no neutral home, carried whole.
    assert product.custom_attributes["categories"] == ["BUSINESS"]
    assert product.custom_attributes["compliance_badges"] == ["GDPR"]
    assert product.custom_attributes["data_dictionary"] == raw["data_dictionary"]
    assert product.custom_attributes["comment"] == "Managed by the commercial analytics team."


def test_the_fragments_only_ever_carry_neutral_model_field_names() -> None:
    """Every fragment key must be unpackable straight into the neutral constructor."""
    dataset_keys = set(map_dataset_fields(make_raw_table(), tag_references=[]))
    product_keys = set(map_data_product_fields(make_raw_schema(), tag_references=[]))
    listing_keys = set(map_listing_fields(make_raw_listing(), tag_references=[]))

    assert dataset_keys <= set(Dataset.model_fields)
    assert product_keys <= set(DataProduct.model_fields)
    assert listing_keys <= set(DataProduct.model_fields)
    assert dataset_keys == {
        "description",
        "owners",
        "physical_ref",
        "tags",
        "classifications",
    }
    assert product_keys == {"description", "owners", "tags"}
    assert listing_keys == {"description", "documentation", "status", "owners", "tags"}
