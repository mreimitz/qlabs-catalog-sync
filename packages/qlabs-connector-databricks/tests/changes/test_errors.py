"""Terminal HTTP failures map onto the SDK's typed exceptions; a retryable failure is
actually retried by the shared ``HttpEndpoint`` before this module ever sees it; and an
entity type Databricks has no native answer for (decision D5: no glossary) refuses
cleanly rather than returning an empty/fake result."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, Watermark
from qlabs_catalog_sync_sdk.exceptions import AuthError, CapabilityError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.changes import list_changed

from .conftest import BASE_URL, CATALOGS_PATH, ENDPOINT, catalog, mock_single_page


async def test_401_raises_auth_error(respx_mock, http: HttpEndpoint) -> None:
    respx_mock.get(f"{BASE_URL}{CATALOGS_PATH}").mock(
        return_value=httpx.Response(401, json={"message": "invalid token"})
    )

    with pytest.raises(AuthError):
        await list_changed(
            http,
            EntityType.DATA_PRODUCT,
            Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
            endpoint=ENDPOINT,
        )


async def test_403_also_raises_auth_error(respx_mock, http: HttpEndpoint) -> None:
    respx_mock.get(f"{BASE_URL}{CATALOGS_PATH}").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )

    with pytest.raises(AuthError):
        await list_changed(
            http,
            EntityType.DATA_PRODUCT,
            Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
            endpoint=ENDPOINT,
        )


async def test_429_is_retried_then_succeeds(
    respx_mock, make_http: Callable[..., HttpEndpoint]
) -> None:
    endpoint = make_http()
    route = respx_mock.get(f"{BASE_URL}{CATALOGS_PATH}").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"}),
            httpx.Response(200, json={"catalogs": [catalog("main")]}),
        ]
    )
    mock_single_page(
        respx_mock,
        "/api/2.1/unity-catalog/schemas",
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[],
    )

    result = await list_changed(
        endpoint,
        EntityType.DATA_PRODUCT,
        Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
    )

    assert route.call_count == 2
    assert result.is_empty


async def test_unsupported_glossary_term_entity_type_raises_capability_error(
    http: HttpEndpoint,
) -> None:
    with pytest.raises(CapabilityError):
        await list_changed(
            http,
            EntityType.GLOSSARY_TERM,
            Watermark.initial(ENDPOINT, EntityType.GLOSSARY_TERM),
            endpoint=ENDPOINT,
        )


async def test_unsupported_category_entity_type_raises_capability_error(
    http: HttpEndpoint,
) -> None:
    with pytest.raises(CapabilityError):
        await list_changed(
            http,
            EntityType.CATEGORY,
            Watermark.initial(ENDPOINT, EntityType.CATEGORY),
            endpoint=ENDPOINT,
        )
