"""``read_schema``: a Snowflake schema as the schema-shaped ``DataProduct``, read together
with the tables and views it contains."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import NotFound, TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import AssetType, EntityType
from qlabs_connector_snowflake.read import StatementClient, read_schema

from .conftest import (
    ENDPOINT,
    SCHEMATA_COLUMNS,
    TABLES_COLUMNS,
    TAG_REFERENCE_COLUMNS,
    TENANT_ID,
    StatementRouter,
    dataset_ref,
    schema_ref,
    schema_row,
    statements_url,
    table_row,
    tag_reference_row,
)


def _route_schema(router: StatementRouter, *, members: list[list[object]] | None = None) -> None:
    router.rows("INFORMATION_SCHEMA.SCHEMATA", SCHEMATA_COLUMNS, [schema_row()])
    router.rows(
        "TAG_REFERENCES(",
        TAG_REFERENCE_COLUMNS,
        [tag_reference_row("DOMAIN", "sales", object_name="PUBLIC")],
    )
    router.rows(
        "INFORMATION_SCHEMA.TABLES",
        TABLES_COLUMNS,
        members
        if members is not None
        else [
            table_row("ORDERS"),
            table_row("ORDERS_EU", table_type="VIEW", comment=None),
        ],
    )
    router.rows(
        "ACCOUNT_USAGE.TAG_REFERENCES",
        TAG_REFERENCE_COLUMNS,
        [tag_reference_row("COST_CENTER", "commerce", object_name="ORDERS")],
    )


async def test_a_schema_reads_into_a_data_product_with_its_members(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_schema(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_schema(make_client(http), schema_ref())

    product = read.data_product
    assert product.name == "PUBLIC"
    assert product.description is not None
    assert product.description.text == "Conformed sales dimensions and facts."
    assert [party.display_name for party in product.owners] == ["SYSADMIN"]
    assert [tag.key for tag in product.tags] == ["GOVERNANCE.TAGS.DOMAIN"]
    assert [dataset.name for dataset in read.datasets] == ["ORDERS", "ORDERS_EU"]
    assert read.member_object_names == [
        "SALES_DB.PUBLIC.ORDERS",
        "SALES_DB.PUBLIC.ORDERS_EU",
    ]


async def test_the_identity_is_the_two_part_qualified_name(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_schema(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_schema(make_client(http), schema_ref())

    (ref,) = read.data_product.identities
    assert ref.endpoint == ENDPOINT
    assert ref.entity_type is EntityType.DATA_PRODUCT
    assert ref.native_key == "SALES_DB.PUBLIC"
    assert ref.tenant_id == TENANT_ID


async def test_dataset_refs_stay_empty_so_the_checksum_is_stable_across_reads(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """A fresh random ``neutral_id`` per read would make ``dataset_refs`` change every
    cycle; membership is IdentityMap work for the sync loop instead."""
    _route_schema(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_schema(make_client(http), schema_ref())

    assert read.data_product.dataset_refs == []
    assert "dataset_refs" not in read.data_product.field_envelopes
    # The membership is still reported, just not through that field.
    assert len(read.member_object_names) == len(read.datasets) == 2


async def test_a_schema_has_no_documentation_status_or_placement(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_schema(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    product = (await read_schema(make_client(http), schema_ref())).data_product

    assert product.documentation is None
    assert product.status is None
    assert product.placement is None


async def test_member_datasets_get_their_tags_from_the_account_wide_index(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """One statement covers every member, whatever the table count."""
    _route_schema(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_schema(make_client(http), schema_ref())

    tagged = {dataset.name: [tag.key for tag in dataset.tags] for dataset in read.datasets}
    assert tagged["ORDERS"] == ["GOVERNANCE.TAGS.COST_CENTER"]
    # A member the index has no row for is genuinely untagged, not "unread".
    assert tagged["ORDERS_EU"] == []
    assert len(router.statements_matching("ACCOUNT_USAGE.TAG_REFERENCES")) == 1


async def test_the_member_tag_index_is_scoped_by_bind_values(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_schema(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    await read_schema(make_client(http), schema_ref())

    body = router.body_for("ACCOUNT_USAGE.TAG_REFERENCES")
    assert body["bindings"] == {
        "1": {"type": "TEXT", "value": "SALES_DB"},
        "2": {"type": "TEXT", "value": "PUBLIC"},
    }


async def test_an_ungranted_account_usage_share_leaves_member_tags_absent(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """The ``ACCOUNT_USAGE`` share is separately granted. Its absence must degrade to
    "tags not read" -- absent, so the engine leaves the target alone -- not to a false
    empty list, and not to a failed read of the whole data product."""
    router.rows("INFORMATION_SCHEMA.SCHEMATA", SCHEMATA_COLUMNS, [schema_row()])
    router.rows("TAG_REFERENCES(", TAG_REFERENCE_COLUMNS, [])
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row("ORDERS")])
    router.on(
        "ACCOUNT_USAGE.TAG_REFERENCES",
        httpx.Response(403, json={"message": "Insufficient privileges on SNOWFLAKE database"}),
    )
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_schema(make_client(http), schema_ref())

    assert read.data_product.name == "PUBLIC"
    (member,) = read.datasets
    assert member.tags == []
    assert "tags" not in member.field_envelopes
    assert "classifications" not in member.field_envelopes


async def test_a_transient_member_tag_failure_still_propagates(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """Absorbing a blip would turn it into silently missing metadata on every cycle."""
    router.rows("INFORMATION_SCHEMA.SCHEMATA", SCHEMATA_COLUMNS, [schema_row()])
    router.rows("TAG_REFERENCES(", TAG_REFERENCE_COLUMNS, [])
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row("ORDERS")])
    router.on(
        "ACCOUNT_USAGE.TAG_REFERENCES",
        httpx.Response(503, json={"message": "unavailable"}),
    )
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    with pytest.raises(TransientError):
        await read_schema(make_client(http), schema_ref())


async def test_an_empty_schema_skips_the_member_tag_statement_entirely(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_schema(router, members=[])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_schema(make_client(http), schema_ref())

    assert read.datasets == []
    assert read.member_object_names == []
    assert router.statements_matching("ACCOUNT_USAGE.TAG_REFERENCES") == []


async def test_the_schemas_own_tags_use_the_fresh_per_object_surface(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_schema(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    await read_schema(make_client(http), schema_ref())

    statement = router.body_for("TAG_REFERENCES(")["statement"]
    assert "INFORMATION_SCHEMA.TAG_REFERENCES" in statement
    assert "'SALES_DB.PUBLIC'" in statement
    assert "'SCHEMA'" in statement


async def test_members_are_read_in_a_single_statement_ordered_by_name(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_schema(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    await read_schema(make_client(http), schema_ref())

    assert len(router.statements_matching("INFORMATION_SCHEMA.TABLES")) == 1
    assert "ORDER BY TABLE_NAME" in router.body_for("INFORMATION_SCHEMA.TABLES")["statement"]
    # Four statements total regardless of how many tables the schema holds.
    assert len(router.statements) == 4


async def test_members_carry_their_own_asset_type(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_schema(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_schema(make_client(http), schema_ref())

    kinds = {dataset.name: dataset.asset_type for dataset in read.datasets}
    assert kinds == {"ORDERS": AssetType.TABLE, "ORDERS_EU": AssetType.VIEW}


async def test_a_missing_schema_raises_not_found(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.rows("INFORMATION_SCHEMA.SCHEMATA", SCHEMATA_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    with pytest.raises(NotFound) as caught:
        await read_schema(make_client(http), schema_ref("SALES_DB.GONE"))

    assert caught.value.native_key == "SALES_DB.GONE"


async def test_a_dataset_ref_is_rejected(
    http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    with pytest.raises(ValueError, match="DATA_PRODUCT"):
        await read_schema(make_client(http), dataset_ref())


async def test_a_malformed_schema_name_is_rejected(
    http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    with pytest.raises(ValueError, match="DATABASE.SCHEMA"):
        await read_schema(make_client(http), schema_ref("PUBLIC"))
