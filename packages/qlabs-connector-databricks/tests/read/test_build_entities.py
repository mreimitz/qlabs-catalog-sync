"""build_data_product / build_dataset: identity, field envelopes, lossless
custom_attributes round-trip, and deterministic checksums (the property the engine's
idempotency rests on)."""

from __future__ import annotations

from qlabs_catalog_sync_sdk.models import AssetType, EntityType
from qlabs_connector_databricks.read import build_data_product, build_dataset

from .conftest import ENDPOINT, TENANT_ID, make_raw_schema, make_raw_table


def test_data_product_identity_uses_schema_id_as_native_key() -> None:
    raw = make_raw_schema()

    product = build_data_product(raw, endpoint=ENDPOINT)

    assert product.name == "sales"
    assert len(product.identities) == 1
    ref = product.identities[0]
    assert ref.endpoint == ENDPOINT
    assert ref.entity_type is EntityType.DATA_PRODUCT
    # The stable id, not the renameable full_name, is the join key.
    assert ref.native_key == "schema-uuid-sales"
    assert ref.tenant_id == TENANT_ID
    assert ref.secondary_keys == {"full_name": "prod.sales"}


def test_data_product_field_envelopes_cover_every_populated_field() -> None:
    product = build_data_product(make_raw_schema(), endpoint=ENDPOINT)

    assert set(product.field_envelopes) == {"name", "custom_attributes"}
    for envelope in product.field_envelopes.values():
        assert envelope.source_endpoint == ENDPOINT
        assert envelope.checksum is not None
        assert envelope.checksum.startswith("sha256:")


def test_data_product_leaves_t4_5_and_t4_7_fields_at_their_defaults() -> None:
    product = build_data_product(make_raw_schema(), endpoint=ENDPOINT)

    assert product.description is None
    assert product.documentation is None
    assert product.status is None
    assert product.owners == []
    assert product.tags == []
    assert product.dataset_refs == []
    assert product.placement is None


def test_data_product_custom_attributes_preserve_unmapped_fields_byte_identical() -> None:
    raw = make_raw_schema(some_future_field={"nested": [1, 2, 3]})

    product = build_data_product(raw, endpoint=ENDPOINT)

    assert product.custom_attributes["comment"] == "Sales domain schema"
    assert product.custom_attributes["owner"] == "data-eng-sp"
    assert product.custom_attributes["properties"] == {"team": "sales"}
    assert product.custom_attributes["some_future_field"] == {"nested": [1, 2, 3]}
    # Structural fields consumed into identity/name are not duplicated here.
    for consumed in ("name", "full_name", "catalog_name", "schema_id", "metastore_id"):
        assert consumed not in product.custom_attributes


def test_two_reads_of_identical_data_product_data_produce_identical_checksums() -> None:
    raw = make_raw_schema()

    first = build_data_product(raw, endpoint=ENDPOINT)
    second = build_data_product(dict(raw), endpoint=ENDPOINT)

    assert first.field_envelopes["name"].checksum == second.field_envelopes["name"].checksum
    assert (
        first.field_envelopes["custom_attributes"].checksum
        == second.field_envelopes["custom_attributes"].checksum
    )


def test_dataset_identity_uses_table_id_as_native_key() -> None:
    raw = make_raw_table()

    dataset = build_dataset(raw, endpoint=ENDPOINT)

    assert dataset.name == "orders"
    assert dataset.asset_type is AssetType.TABLE
    ref = dataset.identities[0]
    assert ref.entity_type is EntityType.DATASET
    assert ref.native_key == "table-uuid-orders"
    assert ref.tenant_id == TENANT_ID
    assert ref.secondary_keys == {"full_name": "prod.sales.orders"}


def test_dataset_view_asset_type() -> None:
    raw = make_raw_table(table_type="VIEW", data_source_format=None)

    dataset = build_dataset(raw, endpoint=ENDPOINT)

    assert dataset.asset_type is AssetType.VIEW


def test_dataset_field_envelopes_cover_every_populated_field() -> None:
    dataset = build_dataset(make_raw_table(), endpoint=ENDPOINT)

    assert set(dataset.field_envelopes) == {"name", "asset_type", "custom_attributes"}


def test_dataset_custom_attributes_preserve_columns_byte_identical() -> None:
    raw = make_raw_table()

    dataset = build_dataset(raw, endpoint=ENDPOINT)

    assert dataset.custom_attributes["columns"] == raw["columns"]
    assert dataset.custom_attributes["table_type"] == "MANAGED"  # preserved even though consumed
    assert dataset.custom_attributes["data_source_format"] == "DELTA"


def test_dataset_leaves_t4_5_and_t4_7_fields_at_their_defaults() -> None:
    dataset = build_dataset(make_raw_table(), endpoint=ENDPOINT)

    assert dataset.description is None
    assert dataset.owners == []
    assert dataset.tags == []
    assert dataset.classifications == []
    assert dataset.physical_ref is None


def test_two_reads_of_identical_dataset_data_produce_identical_checksums() -> None:
    raw = make_raw_table()

    first = build_dataset(raw, endpoint=ENDPOINT)
    second = build_dataset(dict(raw), endpoint=ENDPOINT)

    for field in ("name", "asset_type", "custom_attributes"):
        assert (
            first.field_envelopes[field].checksum == second.field_envelopes[field].checksum
        ), field
