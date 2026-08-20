"""read_entity: the dispatcher matching Connector.read(ref)'s signature exactly."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import DataProduct, Dataset, EntityType, IdentityRef
from qlabs_connector_databricks.read import SCHEMAS_PATH, TABLES_PATH, read_entity

from .conftest import BASE_URL, data_product_ref, dataset_ref, make_raw_schema, make_raw_table


async def test_data_product_ref_dispatches_to_read_schema(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.get(f"{BASE_URL}{SCHEMAS_PATH}").mock(
        return_value=httpx.Response(200, json={"schemas": [make_raw_schema()]})
    )
    respx_mock.get(f"{BASE_URL}{TABLES_PATH}").mock(
        return_value=httpx.Response(200, json={"tables": [make_raw_table()]})
    )
    http = make_http()

    entity = await read_entity(http, data_product_ref(), catalog_schema_patterns=["prod.sales"])

    assert isinstance(entity, DataProduct)
    assert entity.name == "sales"


async def test_dataset_ref_dispatches_to_read_dataset(
    respx_mock: object, make_http: Callable[..., HttpEndpoint]
) -> None:
    respx_mock.get(f"{BASE_URL}{TABLES_PATH}/prod.sales.orders").mock(
        return_value=httpx.Response(200, json=make_raw_table())
    )
    http = make_http()

    entity = await read_entity(http, dataset_ref(), catalog_schema_patterns=["prod.sales"])

    assert isinstance(entity, Dataset)
    assert entity.name == "orders"


async def test_unsupported_entity_type_raises_capability_error(
    make_http: Callable[..., HttpEndpoint],
) -> None:
    ref = IdentityRef(
        endpoint="databricks",
        entity_type=EntityType.GLOSSARY_TERM,
        native_key="anything",
        tenant_id="metastore-11111111",
    )
    http = make_http()

    with pytest.raises(CapabilityError):
        await read_entity(http, ref, catalog_schema_patterns=["prod.sales"])
