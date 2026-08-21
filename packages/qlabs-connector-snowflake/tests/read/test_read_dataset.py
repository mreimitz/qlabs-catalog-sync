"""``read_dataset``: one table or view, its columns and its tags, into a neutral
``Dataset`` -- read from ``INFORMATION_SCHEMA`` for freshness (RS-05 section 1.4)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import NotFound
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import AssetType, EntityType, TextFormat
from qlabs_connector_snowflake.read import StatementClient, read_dataset

from .conftest import (
    COLUMNS_COLUMNS,
    ENDPOINT,
    TABLES_COLUMNS,
    TAG_REFERENCE_COLUMNS,
    TENANT_ID,
    VIEWS_COLUMNS,
    StatementRouter,
    column_row,
    dataset_ref,
    result_response,
    schema_ref,
    statements_url,
    table_row,
    tag_reference_row,
    view_row,
)


def _route_table(router: StatementRouter, **overrides: object) -> StatementRouter:
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row(**overrides)])
    router.rows(
        "INFORMATION_SCHEMA.COLUMNS",
        COLUMNS_COLUMNS,
        [
            column_row("ID", position=1, data_type="NUMBER", comment="Surrogate key"),
            column_row("EMAIL", position=2, data_type="TEXT", nullable="YES", comment=None),
        ],
    )
    router.rows(
        "TAG_REFERENCES",
        TAG_REFERENCE_COLUMNS,
        [
            tag_reference_row("COST_CENTER", "commerce"),
            tag_reference_row(
                "PRIVACY_CATEGORY",
                "IDENTIFIER",
                tag_database="SNOWFLAKE",
                tag_schema="CORE",
                column_name="EMAIL",
            ),
        ],
    )
    return router


async def test_a_table_reads_into_a_dataset_with_every_neutral_field(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_table(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    dataset = await read_dataset(make_client(http), dataset_ref())

    assert dataset.name == "ORDERS"
    assert dataset.asset_type is AssetType.TABLE
    assert dataset.physical_ref == "SALES_DB.PUBLIC.ORDERS"
    assert dataset.description is not None
    assert dataset.description.text == "Order header rows, one per checkout."
    assert dataset.description.format is TextFormat.PLAIN
    assert [party.display_name for party in dataset.owners] == ["SALES_ENGINEER"]
    assert dataset.owners[0].email is None
    assert [tag.key for tag in dataset.tags] == ["GOVERNANCE.TAGS.COST_CENTER"]
    assert dataset.classifications == ["PRIVACY_CATEGORY=IDENTIFIER"]


async def test_the_identity_is_the_fully_qualified_name(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """Unlike Databricks (an immutable object id), Snowflake's ``INFORMATION_SCHEMA``
    exposes no id column at all, so the FQN is the native key."""
    _route_table(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    dataset = await read_dataset(make_client(http), dataset_ref())

    (ref,) = dataset.identities
    assert ref.endpoint == ENDPOINT
    assert ref.entity_type is EntityType.DATASET
    assert ref.native_key == "SALES_DB.PUBLIC.ORDERS"
    assert ref.tenant_id == TENANT_ID
    assert ref.secondary_keys == {}


async def test_a_caller_supplied_object_id_is_carried_as_a_secondary_key(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """``ACCOUNT_USAGE`` is the only surface with a numeric id, so it can only arrive on
    the ref -- never from the read itself."""
    _route_table(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    dataset = await read_dataset(make_client(http), dataset_ref(object_id="1234567"))

    assert dataset.identities[0].secondary_keys == {"object_id": "1234567"}


async def test_the_column_list_is_read_and_preserved(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_table(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    dataset = await read_dataset(make_client(http), dataset_ref())

    columns = dataset.custom_attributes["COLUMNS"]
    assert [column["COLUMN_NAME"] for column in columns] == ["ID", "EMAIL"]
    assert columns[0]["DATA_TYPE"] == "NUMBER"
    assert columns[1]["COMMENT"] is None
    # Ordered by ordinal position, as the projection asks for.
    assert "ORDER BY ORDINAL_POSITION" in router.body_for("INFORMATION_SCHEMA.COLUMNS")["statement"]


async def test_the_native_kind_survives_in_custom_attributes(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """``asset_type`` collapses RS-05 1.2's six table kinds onto ``TABLE``, so the kind
    itself has to be preserved somewhere."""
    _route_table(router, table_type="ICEBERG TABLE")
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    dataset = await read_dataset(make_client(http), dataset_ref())

    assert dataset.asset_type is AssetType.TABLE
    assert dataset.custom_attributes["TABLE_TYPE"] == "ICEBERG TABLE"
    assert dataset.custom_attributes["IS_TRANSIENT"] == "NO"
    # Identity and promoted content columns are not duplicated here.
    for consumed in ("TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "COMMENT"):
        assert consumed not in dataset.custom_attributes


async def test_a_view_falls_back_to_the_views_projection(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """``TABLES`` is tried first; a name it does not know is looked for in ``VIEWS``."""
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [])
    router.rows("INFORMATION_SCHEMA.VIEWS", VIEWS_COLUMNS, [view_row()])
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [column_row()])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    dataset = await read_dataset(make_client(http), dataset_ref("SALES_DB.PUBLIC.ORDERS_EU"))

    assert dataset.name == "ORDERS_EU"
    assert dataset.asset_type is AssetType.VIEW
    assert dataset.custom_attributes["IS_SECURE"] == "YES"
    # The tag read names the right object domain for a view.
    assert "'VIEW'" in router.body_for("TAG_REFERENCES")["statement"]


async def test_a_table_never_triggers_the_views_fallback(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_table(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    await read_dataset(make_client(http), dataset_ref())

    assert router.statements_matching("INFORMATION_SCHEMA.VIEWS") == []
    assert "'TABLE'" in router.body_for("TAG_REFERENCES")["statement"]


async def test_an_object_in_neither_view_raises_not_found(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [])
    router.rows("INFORMATION_SCHEMA.VIEWS", VIEWS_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    with pytest.raises(NotFound) as caught:
        await read_dataset(make_client(http), dataset_ref("SALES_DB.PUBLIC.GONE"))

    assert caught.value.native_key == "SALES_DB.PUBLIC.GONE"


async def test_null_optional_columns_never_become_invented_values(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """A ``NULL`` comment and owner mean "the source says there is nothing here" --
    an explicit empty, not a fabricated default and not silence."""
    router.rows(
        "INFORMATION_SCHEMA.TABLES",
        TABLES_COLUMNS,
        [table_row(comment=None, owner=None)],
    )
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    dataset = await read_dataset(make_client(http), dataset_ref())

    assert dataset.description is None
    assert dataset.owners == []
    assert dataset.custom_attributes["COLUMNS"] == []


async def test_an_untagged_object_reports_empty_tags_not_absent_ones(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """The tag read ran and found nothing -- a real answer worth syncing, distinct from
    "tags were never read"."""
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row()])
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    dataset = await read_dataset(make_client(http), dataset_ref())

    assert dataset.tags == []
    assert dataset.classifications == []
    assert "tags" in dataset.field_envelopes
    assert "classifications" in dataset.field_envelopes


async def test_reading_uses_exactly_three_statements(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_table(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    await read_dataset(make_client(http), dataset_ref())

    assert len(router.statements) == 3


async def test_a_data_product_ref_is_rejected(
    http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    with pytest.raises(ValueError, match="DATASET"):
        await read_dataset(make_client(http), schema_ref())


@pytest.mark.parametrize(
    "native_key", ["SALES_DB.PUBLIC", "ORDERS", "SALES_DB.PUBLIC.ORDERS.ID", "A..C"]
)
async def test_a_malformed_object_name_is_rejected(
    http: HttpEndpoint, make_client: Callable[..., StatementClient], native_key: str
) -> None:
    with pytest.raises(ValueError, match="DATABASE.SCHEMA.OBJECT"):
        await read_dataset(make_client(http), dataset_ref(native_key))


async def test_a_paged_column_list_is_fully_collected(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """A wide table's column list spans partitions; every one has to be collected or the
    dataset silently loses columns."""
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row()])
    router.on(
        "INFORMATION_SCHEMA.COLUMNS",
        httpx.Response(
            200,
            json=result_response(COLUMNS_COLUMNS, [column_row("ID", position=1)], partitions=2),
        ),
    )
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]
    respx_mock.get(  # type: ignore[attr-defined]
        url__regex=r".*/api/v2/statements/.*"
    ).mock(return_value=httpx.Response(200, json={"data": [column_row("EMAIL", position=2)]}))

    dataset = await read_dataset(make_client(http), dataset_ref())

    assert [column["COLUMN_NAME"] for column in dataset.custom_attributes["COLUMNS"]] == [
        "ID",
        "EMAIL",
    ]
