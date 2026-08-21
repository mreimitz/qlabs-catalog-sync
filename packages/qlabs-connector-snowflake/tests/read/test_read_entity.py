"""``read_entity`` -- the function ``Connector.read(ref)`` delegates straight to -- and the
shape detection that decides whether a ``DATA_PRODUCT`` ref names a schema or a listing."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    Dataset,
    EntityType,
    IdentityRef,
)
from qlabs_connector_snowflake.read import (
    DataProductShape,
    StatementClient,
    data_product_shape,
    read_entity,
)

from .conftest import (
    COLUMNS_COLUMNS,
    DESCRIBE_LISTING_COLUMNS,
    ENDPOINT,
    LISTING_COLUMNS,
    SCHEMATA_COLUMNS,
    SHARE_COLUMNS,
    TABLES_COLUMNS,
    TAG_REFERENCE_COLUMNS,
    TENANT_ID,
    StatementRouter,
    column_row,
    dataset_ref,
    describe_listing_row,
    listing_ref,
    listing_row,
    schema_ref,
    schema_row,
    share_row,
    statements_url,
    table_row,
)

# ----------------------------------------------------------------------------------
# Shape detection
# ----------------------------------------------------------------------------------


def test_a_two_part_native_key_is_a_schema() -> None:
    assert data_product_shape(schema_ref("SALES_DB.PUBLIC")) is DataProductShape.SCHEMA


def test_an_opaque_single_token_native_key_is_a_listing() -> None:
    """RS-05 2.4 describes a global name as a single, structureless token."""
    assert data_product_shape(schema_ref("GZTSZAS2KH9")) is DataProductShape.LISTING


def test_an_explicit_listing_secondary_key_wins_over_the_name_shape() -> None:
    ref = schema_ref("SALES_DB.PUBLIC", listing_name="SALES_DAILY")

    assert data_product_shape(ref) is DataProductShape.LISTING


def test_a_listing_global_name_secondary_key_also_marks_a_listing() -> None:
    ref = schema_ref("GZTSZAS2KH9", listing_global_name="GZTSZAS2KH9")

    assert data_product_shape(ref) is DataProductShape.LISTING


def test_a_three_part_name_is_refused_rather_than_read_as_the_wrong_entity() -> None:
    with pytest.raises(ValueError, match="three-part"):
        data_product_shape(schema_ref("SALES_DB.PUBLIC.ORDERS"))


# ----------------------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------------------


async def test_a_dataset_ref_reads_a_dataset(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row()])
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [column_row()])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    entity = await read_entity(make_client(http), dataset_ref())

    assert isinstance(entity, Dataset)
    assert entity.name == "ORDERS"


async def test_a_schema_shaped_data_product_ref_reads_the_schema(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.rows("INFORMATION_SCHEMA.SCHEMATA", SCHEMATA_COLUMNS, [schema_row()])
    router.rows("TAG_REFERENCES(", TAG_REFERENCE_COLUMNS, [])
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    entity = await read_entity(make_client(http), schema_ref())

    assert isinstance(entity, DataProduct)
    assert entity.name == "PUBLIC"
    # Only the data product is returned; the member half of the read is not.
    assert router.statements_matching("SHOW LISTINGS") == []


async def test_a_listing_shaped_data_product_ref_reads_the_listing(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.rows("SHOW LISTINGS", LISTING_COLUMNS, [listing_row()])
    router.rows("DESCRIBE LISTING", DESCRIBE_LISTING_COLUMNS, [describe_listing_row()])
    router.rows("DESCRIBE SHARE", SHARE_COLUMNS, [share_row()])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    entity = await read_entity(make_client(http), listing_ref())

    assert isinstance(entity, DataProduct)
    assert entity.name == "Daily sales"
    assert router.statements_matching("INFORMATION_SCHEMA.SCHEMATA") == []


@pytest.mark.parametrize("entity_type", [EntityType.GLOSSARY_TERM, EntityType.CATEGORY])
async def test_an_entity_type_the_manifest_declares_unsupported_raises_capability_error(
    http: HttpEndpoint, make_client: Callable[..., StatementClient], entity_type: EntityType
) -> None:
    ref = IdentityRef(
        endpoint=ENDPOINT,
        entity_type=entity_type,
        native_key="SOMETHING",
        tenant_id=TENANT_ID,
    )

    with pytest.raises(CapabilityError) as caught:
        await read_entity(make_client(http), ref)

    assert caught.value.operation == "read"
    assert caught.value.retryable is False
