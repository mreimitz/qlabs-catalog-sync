"""The manifest wired into a real Connector subclass, via the contract's own guards.

``ensure_supported``/``ensure_writable`` live on ``contract.Connector`` (T1.2) and call
straight into whatever ``capabilities()`` returns. These tests prove the concrete
``CapabilityManifest`` (T1.3) makes those guards behave correctly end to end — nothing
here is mocked: ``FakeQlikConnector`` (conftest.py) is a real ``Connector`` subclass and
``qlik_manifest``/its fields are the real pydantic model.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.contract import CapabilityError, FieldDiff, IdentityRef
from qlabs_catalog_sync_sdk.models import EntityType, FieldChange

from .conftest import FakeQlikConnector


def test_ensure_writable_raises_for_a_ro_field(qlik_connector: FakeQlikConnector) -> None:
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="placement", value="shared-space")],
    )

    with pytest.raises(CapabilityError) as exc_info:
        qlik_connector.ensure_writable(diff)

    assert exc_info.value.field == "placement"
    assert exc_info.value.entity_type == EntityType.DATA_PRODUCT
    assert exc_info.value.retryable is False


def test_ensure_writable_raises_for_a_na_field(qlik_connector: FakeQlikConnector) -> None:
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="glossary_term_refs", value=[])],
    )

    with pytest.raises(CapabilityError) as exc_info:
        qlik_connector.ensure_writable(diff)

    assert exc_info.value.field == "glossary_term_refs"


def test_ensure_writable_raises_for_an_undeclared_field(qlik_connector: FakeQlikConnector) -> None:
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="no_such_field", value="x")],
    )

    with pytest.raises(CapabilityError) as exc_info:
        qlik_connector.ensure_writable(diff)

    assert exc_info.value.field == "no_such_field"


def test_ensure_writable_passes_for_an_rw_field(qlik_connector: FakeQlikConnector) -> None:
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="name", value="New Name")],
    )

    qlik_connector.ensure_writable(diff)  # must not raise


def test_ensure_writable_raises_for_an_unsupported_entity(
    qlik_connector: FakeQlikConnector,
) -> None:
    diff = FieldDiff(
        entity_type=EntityType.GLOSSARY_TERM,
        changes=[FieldChange(field="name", value="EBIT")],
    )

    with pytest.raises(CapabilityError):
        qlik_connector.ensure_writable(diff)


def test_ensure_supported_raises_for_glossary_per_decision_d5(
    qlik_connector: FakeQlikConnector,
) -> None:
    with pytest.raises(CapabilityError):
        qlik_connector.ensure_supported(EntityType.GLOSSARY_TERM)


def test_ensure_supported_passes_for_data_product(qlik_connector: FakeQlikConnector) -> None:
    qlik_connector.ensure_supported(EntityType.DATA_PRODUCT)  # must not raise


async def test_update_end_to_end_refuses_a_ro_field_before_any_api_call(
    qlik_connector: FakeQlikConnector, qlik_data_product_ref: IdentityRef
) -> None:
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="placement", value="shared-space")],
    )

    with pytest.raises(CapabilityError):
        await qlik_connector.update(qlik_data_product_ref, diff)


async def test_update_end_to_end_succeeds_for_a_writable_field(
    qlik_connector: FakeQlikConnector, qlik_data_product_ref: IdentityRef
) -> None:
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="name", value="New Name")],
    )

    result = await qlik_connector.update(qlik_data_product_ref, diff)

    assert result.written_fields == ["name"]
