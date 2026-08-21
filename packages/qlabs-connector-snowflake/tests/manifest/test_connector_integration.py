"""The manifest wired into a real ``Connector`` subclass, and into the actual Snowflake
``Connector`` from ``__init__.py`` (the "integration test that Connector().capabilities()
returns it" the task DoD calls for).

``ensure_supported``/``ensure_writable`` live on ``contract.Connector`` (T1.2 in the SDK)
and call straight into whatever ``capabilities()`` returns; the write-path defaults
(``create``/``update``/``delete``) call ``self._capability_error(...)`` unconditionally.
These tests prove that combination end to end for the real, built manifest — nothing is
mocked, no API is touched, and ``_ManifestOnlyConnector`` (``conftest.py``) never
overrides a write method, exactly like the real Snowflake ``Connector``.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.contract import CapabilityError, FieldDiff, IdentityRef
from qlabs_catalog_sync_sdk.manifest import CapabilityManifest
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType, FieldChange
from qlabs_connector_snowflake import Connector
from qlabs_connector_snowflake.manifest import build_manifest

from .conftest import _ManifestOnlyConnector


def test_real_connector_capabilities_returns_the_built_manifest() -> None:
    """Connector().capabilities() (no setup() call needed — see test_declaration.py's
    config-independence note) returns exactly what build_manifest() builds."""
    connector = Connector()

    assert connector.capabilities() == build_manifest()


def test_real_connector_capabilities_is_a_capability_manifest_instance() -> None:
    manifest = Connector().capabilities()

    assert isinstance(manifest, CapabilityManifest)


def test_ensure_supported_passes_for_data_product_and_dataset(
    connector: _ManifestOnlyConnector,
) -> None:
    connector.ensure_supported(EntityType.DATA_PRODUCT)  # must not raise
    connector.ensure_supported(EntityType.DATASET)  # must not raise


def test_ensure_supported_raises_for_glossary(connector: _ManifestOnlyConnector) -> None:
    with pytest.raises(CapabilityError):
        connector.ensure_supported(EntityType.GLOSSARY_TERM)


def test_ensure_supported_raises_for_category(connector: _ManifestOnlyConnector) -> None:
    with pytest.raises(CapabilityError):
        connector.ensure_supported(EntityType.CATEGORY)


def test_ensure_writable_raises_for_every_declared_data_product_field(
    connector: _ManifestOnlyConnector,
) -> None:
    for field in ("name", "description", "owners", "tags", "dataset_refs", "status"):
        diff = FieldDiff(
            entity_type=EntityType.DATA_PRODUCT,
            changes=[FieldChange(field=field, value="x")],
        )
        with pytest.raises(CapabilityError) as exc_info:
            connector.ensure_writable(diff)
        assert exc_info.value.field == field


def test_ensure_writable_raises_for_an_undeclared_field(connector: _ManifestOnlyConnector) -> None:
    diff = FieldDiff(
        entity_type=EntityType.DATASET,
        changes=[FieldChange(field="no_such_field", value="x")],
    )

    with pytest.raises(CapabilityError):
        connector.ensure_writable(diff)


async def test_create_refuses_with_capability_error_without_any_api_call(
    connector: _ManifestOnlyConnector,
) -> None:
    with pytest.raises(CapabilityError):
        await connector.create(DataProduct(name="sales"))


async def test_update_refuses_with_capability_error_for_every_declared_field(
    connector: _ManifestOnlyConnector, data_product_ref: IdentityRef
) -> None:
    for field in ("name", "description", "owners", "tags", "dataset_refs", "status"):
        diff = FieldDiff(
            entity_type=EntityType.DATA_PRODUCT,
            changes=[FieldChange(field=field, value="x")],
        )
        with pytest.raises(CapabilityError):
            await connector.update(data_product_ref, diff)


async def test_update_refuses_even_for_a_field_the_manifest_marks_ro(
    connector: _ManifestOnlyConnector, dataset_ref: IdentityRef
) -> None:
    """`physical_ref` is `ro`, never `na` — the closest thing this manifest has to a
    "looks readable" field — and it must still refuse a write."""
    diff = FieldDiff(
        entity_type=EntityType.DATASET,
        changes=[FieldChange(field="physical_ref", value="SALES_DB.SALES_SCHEMA.ORDERS")],
    )

    with pytest.raises(CapabilityError):
        await connector.update(dataset_ref, diff)


async def test_delete_refuses_with_capability_error(
    connector: _ManifestOnlyConnector, data_product_ref: IdentityRef
) -> None:
    with pytest.raises(CapabilityError):
        await connector.delete(data_product_ref)
