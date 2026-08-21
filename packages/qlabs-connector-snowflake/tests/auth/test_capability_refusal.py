"""What T6.1 owns of the contract's shape: the connector instantiates, its manifest is
callable without setup() (unlike Databricks — see manifest.py's docstring on
config-independence), list_changed() is an honest NotImplementedError placeholder for
T6.3, and the write path refuses with CapabilityError without this connector writing any
write code at all.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.contract import Watermark
from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType, FieldDiff, IdentityRef
from qlabs_connector_snowflake import Connector


def _ref(entity_type: EntityType = EntityType.DATA_PRODUCT) -> IdentityRef:
    return IdentityRef(
        endpoint="snowflake",
        entity_type=entity_type,
        native_key="SALES_DB.SALES_SCHEMA",
        tenant_id="acme",
    )


def test_capabilities_does_not_need_setup_first() -> None:
    """Unlike Databricks (whose manifest varies with a resolved SQL-warehouse config),
    build_manifest() takes no arguments, so this must work before setup()."""
    manifest = Connector().capabilities()

    assert manifest.supports(EntityType.DATA_PRODUCT)
    assert manifest.supports(EntityType.DATASET)


async def test_list_changed_raises_not_implemented_with_a_t6_3_pointer() -> None:
    connector = Connector()

    with pytest.raises(NotImplementedError, match="T6.3"):
        await connector.list_changed(
            EntityType.DATA_PRODUCT,
            Watermark.initial("snowflake", EntityType.DATA_PRODUCT),
        )



async def test_create_refuses_with_capability_error() -> None:
    connector = Connector()
    entity = DataProduct(name="sales")

    with pytest.raises(CapabilityError):
        await connector.create(entity)


async def test_update_refuses_with_capability_error() -> None:
    connector = Connector()
    diff = FieldDiff(entity_type=EntityType.DATA_PRODUCT, changes=[])

    with pytest.raises(CapabilityError):
        await connector.update(_ref(), diff)


async def test_delete_refuses_with_capability_error() -> None:
    connector = Connector()

    with pytest.raises(CapabilityError):
        await connector.delete(_ref())
