"""End-to-end: a realistic UC schema/table payload maps to the right neutral DataProduct/
Dataset fields when assembled the way ``read.py`` is expected to once T4.5 is wired in (see
this task's report for the exact ``read.py`` change).

This mirrors ``tests/read/test_build_entities.py``'s shape without importing from it or from
``read.py``'s private helpers -- ``read.py`` remains out of this task's owned paths, so these
tests build the neutral entities directly from this module's public functions plus the SDK, the
same way ``read.py`` will once wired.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.envelope import build_field_envelopes
from qlabs_catalog_sync_sdk.models import (
    AssetType,
    DataProduct,
    Dataset,
    EntityType,
    IdentityRef,
    TextFormat,
)
from qlabs_connector_databricks.mapping import (
    map_custom_attributes,
    map_data_product_fields,
    map_dataset_fields,
)

from .conftest import ENDPOINT, make_raw_schema, make_raw_table

_SCHEMA_STRUCTURAL_FIELDS = frozenset(
    {"name", "full_name", "catalog_name", "schema_id", "metastore_id"}
)
_TABLE_STRUCTURAL_FIELDS = frozenset(
    {"name", "full_name", "catalog_name", "schema_name", "table_id", "metastore_id"}
)


def _schema_ref(raw: dict[str, object]) -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=str(raw["schema_id"]),
        tenant_id=str(raw["metastore_id"]),
        secondary_keys={"full_name": str(raw["full_name"])},
    )


def _table_ref(raw: dict[str, object]) -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key=str(raw["table_id"]),
        tenant_id=str(raw["metastore_id"]),
        secondary_keys={"full_name": str(raw["full_name"])},
    )


def test_realistic_schema_payload_maps_to_the_right_data_product_fields() -> None:
    raw = make_raw_schema(
        comment="Sales domain schema.",
        owner="sales-analytics@acme.com",
    )
    custom_attributes = map_custom_attributes(raw, exclude=_SCHEMA_STRUCTURAL_FIELDS)
    content = map_data_product_fields(raw)
    values: dict[str, object] = {
        "name": raw["name"],
        "custom_attributes": custom_attributes,
        **content,
    }

    product = DataProduct(
        identities=[_schema_ref(raw)],
        name=str(raw["name"]),
        custom_attributes=custom_attributes,
        field_envelopes=build_field_envelopes(values, source_endpoint=ENDPOINT),
        **content,
    )

    assert product.description is not None
    assert product.description.text == "Sales domain schema."
    assert product.description.format is TextFormat.PLAIN
    assert len(product.owners) == 1
    assert product.owners[0].email == "sales-analytics@acme.com"
    # T4.7's seam: never set here.
    assert product.tags == []
    # Manifest `na` fields: never invented.
    assert product.documentation is None
    assert product.status is None
    assert product.placement is None
    # properties still round-trips, nested exactly once.
    assert product.custom_attributes["properties"] == raw["properties"]
    assert "comment" not in product.custom_attributes
    assert "owner" not in product.custom_attributes


def test_realistic_table_payload_maps_to_the_right_dataset_fields() -> None:
    raw = make_raw_table(
        comment="Order header rows.",
        owner="e3b0c442-98fc-4e1c-8b1a-3f1b2c4d5e6f",
    )
    custom_attributes = map_custom_attributes(raw, exclude=_TABLE_STRUCTURAL_FIELDS)
    content = map_dataset_fields(raw)
    values: dict[str, object] = {
        "name": raw["name"],
        "asset_type": AssetType.TABLE,
        "custom_attributes": custom_attributes,
        **content,
    }

    dataset = Dataset(
        identities=[_table_ref(raw)],
        name=str(raw["name"]),
        asset_type=AssetType.TABLE,
        custom_attributes=custom_attributes,
        field_envelopes=build_field_envelopes(values, source_endpoint=ENDPOINT),
        **content,
    )

    assert dataset.description is not None
    assert dataset.description.text == "Order header rows."
    assert len(dataset.owners) == 1
    assert dataset.owners[0].party_id == "e3b0c442-98fc-4e1c-8b1a-3f1b2c4d5e6f"
    assert dataset.owners[0].email is None
    assert dataset.physical_ref == "prod.sales.orders"
    # T4.7's seam: never set here.
    assert dataset.tags == []
    assert dataset.classifications == []
    # properties still round-trips, and columns (not otherwise mapped) survive too.
    assert dataset.custom_attributes["properties"] == raw["properties"]
    assert dataset.custom_attributes["columns"] == raw["columns"]
    assert "comment" not in dataset.custom_attributes
    assert "owner" not in dataset.custom_attributes


def test_content_fields_never_touch_na_or_t4_7_fields() -> None:
    raw_schema = make_raw_schema()
    raw_table = make_raw_table()

    assert set(map_data_product_fields(raw_schema)) <= {"description", "owners"}
    assert set(map_dataset_fields(raw_table)) <= {"description", "owners", "physical_ref"}
