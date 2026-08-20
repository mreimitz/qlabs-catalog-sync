"""The manifest wired into a real ``Connector`` subclass.

``ensure_supported``/``ensure_writable`` live on ``contract.Connector`` (T1.2) and call
straight into whatever ``capabilities()`` returns; the write-path defaults
(``create``/``update``/``delete``) call ``self._capability_error(...)`` unconditionally.
These tests prove that combination end to end for the real, built manifest — nothing is
mocked, no API is touched, and ``_ManifestOnlyConnector`` (``conftest.py``) never
overrides a write method, exactly like the real Databricks ``Connector``.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.contract import CapabilityError, FieldDiff, IdentityRef
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType, FieldChange

from .conftest import _ManifestOnlyConnector


def test_ensure_supported_passes_for_data_product_and_dataset(
    connector_with_warehouse: _ManifestOnlyConnector,
) -> None:
    connector_with_warehouse.ensure_supported(EntityType.DATA_PRODUCT)  # must not raise
    connector_with_warehouse.ensure_supported(EntityType.DATASET)  # must not raise


def test_ensure_supported_raises_for_glossary(
    connector_with_warehouse: _ManifestOnlyConnector,
) -> None:
    with pytest.raises(CapabilityError):
        connector_with_warehouse.ensure_supported(EntityType.GLOSSARY_TERM)


def test_ensure_writable_raises_for_every_declared_field(
    connector_with_warehouse: _ManifestOnlyConnector,
) -> None:
    for field in ("name", "description", "owners", "tags", "dataset_refs", "placement"):
        diff = FieldDiff(
            entity_type=EntityType.DATA_PRODUCT,
            changes=[FieldChange(field=field, value="x")],
        )
        with pytest.raises(CapabilityError) as exc_info:
            connector_with_warehouse.ensure_writable(diff)
        assert exc_info.value.field == field


def test_ensure_writable_raises_for_an_undeclared_field(
    connector_with_warehouse: _ManifestOnlyConnector,
) -> None:
    diff = FieldDiff(
        entity_type=EntityType.DATASET,
        changes=[FieldChange(field="no_such_field", value="x")],
    )

    with pytest.raises(CapabilityError):
        connector_with_warehouse.ensure_writable(diff)


async def test_create_refuses_with_capability_error_without_any_api_call(
    connector_with_warehouse: _ManifestOnlyConnector,
) -> None:
    with pytest.raises(CapabilityError):
        await connector_with_warehouse.create(DataProduct(name="sales"))


async def test_update_refuses_with_capability_error_for_every_declared_field(
    connector_with_warehouse: _ManifestOnlyConnector, schema_ref: IdentityRef
) -> None:
    for field in ("name", "description", "owners", "tags", "dataset_refs", "placement"):
        diff = FieldDiff(
            entity_type=EntityType.DATA_PRODUCT,
            changes=[FieldChange(field=field, value="x")],
        )
        with pytest.raises(CapabilityError):
            await connector_with_warehouse.update(schema_ref, diff)


async def test_update_refuses_even_for_a_field_the_manifest_marks_ro(
    connector_with_warehouse: _ManifestOnlyConnector, table_ref: IdentityRef
) -> None:
    """`physical_ref` is `ro`, never `na` — the closest thing this manifest has to a
    "looks readable" field — and it must still refuse a write."""
    diff = FieldDiff(
        entity_type=EntityType.DATASET,
        changes=[FieldChange(field="physical_ref", value="main.sales.orders")],
    )

    with pytest.raises(CapabilityError):
        await connector_with_warehouse.update(table_ref, diff)


async def test_delete_refuses_with_capability_error(
    connector_with_warehouse: _ManifestOnlyConnector, schema_ref: IdentityRef
) -> None:
    with pytest.raises(CapabilityError):
        await connector_with_warehouse.delete(schema_ref)


async def test_write_path_refuses_identically_without_a_warehouse_too(
    connector_without_warehouse: _ManifestOnlyConnector, schema_ref: IdentityRef
) -> None:
    """The write refusal is unconditional — it does not depend on D6's tags/warehouse
    branch at all, since nothing is ever writable either way."""
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="name", value="x")],
    )

    with pytest.raises(CapabilityError):
        await connector_without_warehouse.update(schema_ref, diff)
