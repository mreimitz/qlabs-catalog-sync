"""What T4.1 owns of the contract's shape: the connector instantiates and refuses
what a read-only source connector must refuse. capabilities()/list_changed()/read()
are later tasks' scope (T4.2/T4.3/T4.4) and raise NotImplementedError here — an honest
placeholder, never something a later task could mistake for real behavior. The write
path (create/update/delete) is never overridden, so it inherits the ABC's
CapabilityError refusal without this connector writing any write code at all.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.contract import Watermark
from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType, FieldDiff, IdentityRef
from qlabs_connector_databricks import Connector


def _ref(entity_type: EntityType = EntityType.DATA_PRODUCT) -> IdentityRef:
    return IdentityRef(
        endpoint="databricks",
        entity_type=entity_type,
        native_key="sales.orders",
        tenant_id="acme",
    )


def test_capabilities_needs_setup_first():
    """The manifest varies with the resolved config (D6 gates tags on a SQL warehouse),
    so asking before setup() is a lifecycle error, not an empty manifest."""
    with pytest.raises(RuntimeError, match="setup"):
        Connector().capabilities()


async def test_list_changed_needs_setup_first():
    """The read path talks over an HttpEndpoint that setup() builds, so asking before
    setup is a lifecycle error rather than a silent empty result."""
    connector = Connector()
    with pytest.raises(RuntimeError, match="setup"):
        await connector.list_changed(
            EntityType.DATA_PRODUCT,
            Watermark.initial("databricks", EntityType.DATA_PRODUCT),
        )


async def test_read_needs_setup_first():
    """Same lifecycle rule as list_changed."""
    connector = Connector()
    with pytest.raises(RuntimeError, match="setup"):
        await connector.read(_ref(EntityType.DATA_PRODUCT))


async def test_create_refuses_with_capability_error():
    connector = Connector()
    entity = DataProduct(name="sales")

    with pytest.raises(CapabilityError):
        await connector.create(entity)


async def test_update_refuses_with_capability_error():
    connector = Connector()
    diff = FieldDiff(entity_type=EntityType.DATA_PRODUCT, changes=[])

    with pytest.raises(CapabilityError):
        await connector.update(_ref(), diff)


async def test_delete_refuses_with_capability_error():
    connector = Connector()

    with pytest.raises(CapabilityError):
        await connector.delete(_ref())
