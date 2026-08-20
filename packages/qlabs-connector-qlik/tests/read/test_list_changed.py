"""``read.list_changed`` — cursor pagination via the SDK's ``paginate_cursor`` helper.

Covers: a multi-page data-product listing follows ``links.next`` and stops (asserting
the exact request count catches a runaway loop); the same for datasets over the Items
API; ``has_more`` is always ``False`` (every call is a full scan — module docstring
point 6); the returned watermark is a stable opaque cursor; and glossary/category
entity types are refused with ``CapabilityError``.
"""

from __future__ import annotations

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, Watermark, WatermarkKind
from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_qlik import read

from .conftest import ENDPOINT, SPACE_ID, TENANT_BASE_URL, TENANT_ID

DATA_PRODUCTS_URL = f"{TENANT_BASE_URL}/api/data-governance/data-products"
ITEMS_URL = f"{TENANT_BASE_URL}/api/v1/items"

DATA_PRODUCT_PAGE_1 = {
    "data": [{"id": "dp1", "qri": "qri:data-product://dp1", "name": "Product One"}],
    "links": {"next": {"href": f"{DATA_PRODUCTS_URL}?cursor=page2"}},
}
DATA_PRODUCT_PAGE_2 = {
    "data": [{"id": "dp2", "qri": "qri:data-product://dp2", "name": "Product Two"}],
    "links": {},
}

ITEM_PAGE_1 = {
    "data": [{"id": "item-1", "resourceId": "ds1", "name": "orders"}],
    "links": {"next": {"href": f"{ITEMS_URL}?cursor=page2"}},
}
ITEM_PAGE_2 = {
    "data": [{"id": "item-2", "resourceId": "ds2", "name": "customers"}],
    "links": {},
}


def _initial(entity_type: EntityType) -> Watermark:
    return Watermark.initial(ENDPOINT, entity_type)


async def test_data_product_listing_follows_links_next_and_stops(
    respx_mock: object, http: HttpEndpoint
) -> None:
    route = respx_mock.get(DATA_PRODUCTS_URL).mock(
        side_effect=[
            httpx.Response(200, json=DATA_PRODUCT_PAGE_1),
            httpx.Response(200, json=DATA_PRODUCT_PAGE_2),
        ]
    )

    result = await read.list_changed(
        http,
        EntityType.DATA_PRODUCT,
        _initial(EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        space_id=SPACE_ID,
    )

    assert route.call_count == 2
    assert [change.ref.native_key for change in result.changes] == ["dp1", "dp2"]
    assert result.has_more is False
    assert result.is_exhausted


async def test_dataset_listing_follows_links_next_and_stops(
    respx_mock: object, http: HttpEndpoint
) -> None:
    route = respx_mock.get(ITEMS_URL).mock(
        side_effect=[
            httpx.Response(200, json=ITEM_PAGE_1),
            httpx.Response(200, json=ITEM_PAGE_2),
        ]
    )

    result = await read.list_changed(
        http,
        EntityType.DATASET,
        _initial(EntityType.DATASET),
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        space_id=SPACE_ID,
    )

    assert route.call_count == 2
    assert [change.ref.native_key for change in result.changes] == ["item-1", "item-2"]
    # Dataset changes carry the item id as native_key (secureQri unresolved until
    # read()); secondary_keys already carry id/resourceId for the follow-up read.
    assert result.changes[0].ref.secondary_keys == {"id": "item-1", "resourceId": "ds1"}


async def test_next_watermark_is_a_stable_opaque_cursor(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.get(DATA_PRODUCTS_URL).mock(
        return_value=httpx.Response(200, json={"data": [], "links": {}})
    )

    result = await read.list_changed(
        http,
        EntityType.DATA_PRODUCT,
        _initial(EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        space_id=SPACE_ID,
    )

    assert result.next_watermark.kind is WatermarkKind.CURSOR
    assert result.next_watermark.cursor == "full-scan"
    assert result.next_watermark.endpoint == ENDPOINT
    assert result.next_watermark.entity_type is EntityType.DATA_PRODUCT
    assert result.is_empty


async def test_empty_first_page_makes_exactly_one_request(
    respx_mock: object, http: HttpEndpoint
) -> None:
    route = respx_mock.get(ITEMS_URL).mock(
        return_value=httpx.Response(200, json={"data": [], "links": {}})
    )

    result = await read.list_changed(
        http,
        EntityType.DATASET,
        _initial(EntityType.DATASET),
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        space_id=SPACE_ID,
    )

    assert route.call_count == 1
    assert result.is_empty


@pytest.mark.parametrize("entity_type", [EntityType.GLOSSARY_TERM, EntityType.CATEGORY])
async def test_unsupported_entity_types_raise_capability_error(
    http: HttpEndpoint, entity_type: EntityType
) -> None:
    with pytest.raises(CapabilityError):
        await read.list_changed(
            http,
            entity_type,
            _initial(entity_type),
            endpoint=ENDPOINT,
            tenant_id=TENANT_ID,
            space_id=SPACE_ID,
        )
