"""``read.read`` — the top-level dispatcher matching ``Connector.read(ref)`` exactly.

Covers: routing a DATA_PRODUCT ref to ``read_data_product`` and a DATASET ref to
``read_dataset`` (returning the correctly-typed neutral subclass), and refusing
glossary/category refs with ``CapabilityError``.
"""

from __future__ import annotations

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, IdentityRef
from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import DataProduct, Dataset
from qlabs_connector_qlik import read

from .conftest import ENDPOINT, TENANT_BASE_URL, TENANT_ID


async def test_data_product_ref_dispatches_to_read_data_product(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.get(f"{TENANT_BASE_URL}/api/data-governance/data-products/dp1").mock(
        return_value=httpx.Response(200, json={"id": "dp1", "name": "Product"})
    )
    ref = IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key="dp1",
        tenant_id=TENANT_ID,
    )

    entity = await read.read(http, ref, endpoint=ENDPOINT)

    assert isinstance(entity, DataProduct)
    assert entity.name == "Product"


async def test_dataset_ref_dispatches_to_read_dataset(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.get(f"{TENANT_BASE_URL}/api/v1/items/item-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "item-1",
                "resourceId": "ds1",
                "name": "orders",
                "resourceAttributes": {"secureQri": "qdf-secure:x"},
            },
        )
    )
    ref = IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key="qdf-secure:x",
        tenant_id=TENANT_ID,
        secondary_keys={"id": "item-1"},
    )

    entity = await read.read(http, ref, endpoint=ENDPOINT)

    assert isinstance(entity, Dataset)
    assert entity.name == "orders"


@pytest.mark.parametrize("entity_type", [EntityType.GLOSSARY_TERM, EntityType.CATEGORY])
async def test_unsupported_entity_types_raise_capability_error(
    http: HttpEndpoint, entity_type: EntityType
) -> None:
    ref = IdentityRef(
        endpoint=ENDPOINT, entity_type=entity_type, native_key="x1", tenant_id=TENANT_ID
    )

    with pytest.raises(CapabilityError):
        await read.read(http, ref, endpoint=ENDPOINT)
