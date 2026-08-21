"""``read_listing``: a Snowflake listing as the listing-shaped ``DataProduct``, identified
by its ``global_name``, with the share beneath it read only as enrichment (RS-05 sections
2.1, 3.5, 3.6)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import NotFound
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import DataProductStatus, EntityType, TextFormat
from qlabs_connector_snowflake.read import (
    SHARE_COMPOSITION_KEY,
    StatementClient,
    read_listing,
    read_share_composition,
)

from .conftest import (
    DESCRIBE_LISTING_COLUMNS,
    ENDPOINT,
    LISTING_COLUMNS,
    SHARE_COLUMNS,
    TENANT_ID,
    StatementRouter,
    dataset_ref,
    describe_listing_row,
    listing_ref,
    listing_row,
    share_row,
    statements_url,
)


def _route_listing(router: StatementRouter, **overrides: object) -> None:
    router.rows("SHOW LISTINGS", LISTING_COLUMNS, [listing_row(**overrides)])
    router.rows("DESCRIBE LISTING", DESCRIBE_LISTING_COLUMNS, [describe_listing_row()])
    router.rows("DESCRIBE SHARE", SHARE_COLUMNS, [share_row()])


async def test_a_listing_reads_into_a_data_product(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_listing(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    product = (await read_listing(make_client(http), listing_ref())).data_product

    assert product.name == "Daily sales"
    assert product.status is DataProductStatus.ACTIVE
    assert [party.display_name for party in product.owners] == ["SALES_PROVIDER"]


async def test_the_identity_is_the_global_name_with_the_local_name_alongside(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """RS-05 2.4 calls the global name "decisively" the right cross-account matching key;
    the local name still travels because ``DESCRIBE LISTING`` addresses a listing by it."""
    _route_listing(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    product = (await read_listing(make_client(http), listing_ref())).data_product

    (ref,) = product.identities
    assert ref.endpoint == ENDPOINT
    assert ref.entity_type is EntityType.DATA_PRODUCT
    assert ref.native_key == "GZTSZAS2KH9"
    assert ref.tenant_id == TENANT_ID
    assert ref.secondary_keys == {"listing_name": "SALES_DAILY"}


async def test_the_subtitle_is_the_short_description_and_the_description_the_long_form(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_listing(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    product = (await read_listing(make_client(http), listing_ref())).data_product

    assert product.description is not None
    assert product.description.text == "Daily sales by region, refreshed nightly"
    assert product.description.format is TextFormat.PLAIN
    assert product.documentation is not None
    assert product.documentation.text.startswith("# Daily sales")
    assert product.documentation.format is TextFormat.MARKDOWN


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("DRAFT", DataProductStatus.DRAFT),
        ("PUBLISHED", DataProductStatus.ACTIVE),
        ("UNPUBLISHED", DataProductStatus.ARCHIVED),
    ],
)
async def test_the_publish_state_becomes_the_neutral_status(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
    state: str,
    expected: DataProductStatus,
) -> None:
    """RS-05 4.4: a sync that manages listings must model draft vs published state."""
    router.rows("SHOW LISTINGS", LISTING_COLUMNS, [listing_row(state=state)])
    router.rows("DESCRIBE LISTING", DESCRIBE_LISTING_COLUMNS, [describe_listing_row(state=state)])
    router.rows("DESCRIBE SHARE", SHARE_COLUMNS, [share_row()])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    product = (await read_listing(make_client(http), listing_ref())).data_product

    assert product.status is expected


async def test_rs05_section_2_2_metadata_round_trips_through_custom_attributes(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """Categories, business needs, data attributes and compliance badges have no neutral
    field; losing them would be worse than carrying them opaquely."""
    _route_listing(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    product = (await read_listing(make_client(http), listing_ref())).data_product

    attributes = product.custom_attributes
    assert attributes["categories"] == json.dumps(["BUSINESS"])
    assert attributes["business_needs"] == json.dumps([{"name": "Revenue reporting"}])
    assert attributes["data_attributes"] == json.dumps({"refresh_rate": "DAILY"})
    assert attributes["compliance_badges"] == json.dumps(["GDPR"])
    assert attributes["review_state"] == "APPROVED"
    # The listing's own comment is a separate field from subtitle/description and survives.
    assert attributes["comment"] == "Managed by the commercial analytics team."
    # The promoted columns are not duplicated.
    for consumed in ("title", "subtitle", "description", "global_name", "name"):
        assert consumed not in attributes


async def test_a_v1_listing_is_distinguishable_from_a_v2_one_by_its_targeting(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """RS-05 2.3: V1 targets individual accounts through ``targets``; V2 uses
    ``external_targets``/``locations`` and supports pricing plans. Both round-trip."""
    v2_columns = (*DESCRIBE_LISTING_COLUMNS, "external_targets", "locations", "pricing_plans")
    v2_row = [
        *describe_listing_row(),
        json.dumps({"all_organizations": True}),
        json.dumps({"access_regions": ["PUBLIC"]}),
        json.dumps([{"name": "standard"}]),
    ]
    router.rows("SHOW LISTINGS", LISTING_COLUMNS, [listing_row()])
    router.rows("DESCRIBE LISTING", v2_columns, [v2_row])
    router.rows("DESCRIBE SHARE", SHARE_COLUMNS, [share_row()])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    product = (await read_listing(make_client(http), listing_ref())).data_product

    assert product.custom_attributes["targets"] == json.dumps({"accounts": ["Org1.Account1"]})
    assert product.custom_attributes["external_targets"] == json.dumps({"all_organizations": True})
    assert product.custom_attributes["locations"] == json.dumps({"access_regions": ["PUBLIC"]})
    assert product.custom_attributes["pricing_plans"] == json.dumps([{"name": "standard"}])


async def test_the_data_dictionary_names_the_member_objects(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    _route_listing(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_listing(make_client(http), listing_ref())

    assert read.member_object_names == ["SALES_DB.PUBLIC.ORDERS"]
    # A listing's objects can live in another account entirely, so they are named, not read.
    assert read.datasets == []
    assert read.data_product.dataset_refs == []


async def test_a_flat_data_dictionary_list_is_understood_too(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.rows("SHOW LISTINGS", LISTING_COLUMNS, [listing_row(share=None)])
    router.rows(
        "DESCRIBE LISTING",
        DESCRIBE_LISTING_COLUMNS,
        [
            describe_listing_row(
                data_dictionary=[
                    {"database": "SALES_DB", "schema": "PUBLIC", "name": "ORDERS"},
                    {"database": "SALES_DB", "schema": "PUBLIC", "name": "CUSTOMERS"},
                ]
            )
        ],
    )
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_listing(make_client(http), listing_ref())

    assert read.member_object_names == [
        "SALES_DB.PUBLIC.ORDERS",
        "SALES_DB.PUBLIC.CUSTOMERS",
    ]


async def test_an_unparseable_data_dictionary_costs_the_hint_not_the_read(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.rows("SHOW LISTINGS", LISTING_COLUMNS, [listing_row(share=None)])
    router.rows(
        "DESCRIBE LISTING",
        DESCRIBE_LISTING_COLUMNS,
        [describe_listing_row(data_dictionary="featured:\n  database: SALES_DB")],
    )
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_listing(make_client(http), listing_ref())

    assert read.member_object_names == []
    # The raw value still round-trips verbatim, so nothing is lost.
    assert read.data_product.custom_attributes["data_dictionary"].startswith("featured:")


async def test_the_share_composition_is_folded_in_as_enrichment(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """A share is the substrate beneath a listing, never an entity of its own -- it shows
    up only as extra detail on the listing's data product."""
    _route_listing(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_listing(make_client(http), listing_ref())

    composition = read.data_product.custom_attributes[SHARE_COMPOSITION_KEY]
    assert composition == [
        {
            "created_on": "1700000000.000000000",
            "kind": "TABLE",
            "name": "SALES_DB.PUBLIC.ORDERS",
            "shared_on": "1700000000.000000000",
        }
    ]
    assert 'DESCRIBE SHARE "SALES_S"' in router.body_for("DESCRIBE SHARE")["statement"]


async def test_a_listing_with_no_share_never_describes_one(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.rows("SHOW LISTINGS", LISTING_COLUMNS, [listing_row(share=None)])
    router.rows("DESCRIBE LISTING", DESCRIBE_LISTING_COLUMNS, [describe_listing_row()])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_listing(make_client(http), listing_ref())

    assert router.statements_matching("DESCRIBE SHARE") == []
    assert SHARE_COMPOSITION_KEY not in read.data_product.custom_attributes


async def test_an_unreadable_share_degrades_instead_of_failing_the_listing_read(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """``DESCRIBE SHARE`` is an ACCOUNTADMIN-shaped privilege a read-only sync role may
    not hold. Enrichment absent is not the listing being unreadable."""
    router.rows("SHOW LISTINGS", LISTING_COLUMNS, [listing_row()])
    router.rows("DESCRIBE LISTING", DESCRIBE_LISTING_COLUMNS, [describe_listing_row()])
    router.on("DESCRIBE SHARE", httpx.Response(403, json={"message": "insufficient privileges"}))
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    read = await read_listing(make_client(http), listing_ref())

    assert read.data_product.name == "Daily sales"
    assert SHARE_COMPOSITION_KEY not in read.data_product.custom_attributes


async def test_read_share_composition_returns_none_rather_than_raising_on_refusal(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.on("DESCRIBE SHARE", httpx.Response(404, json={"message": "does not exist"}))
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    assert await read_share_composition(make_client(http), "GONE_S") is None


async def test_the_listing_is_selected_by_identity_not_by_being_the_only_row(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """``SHOW LISTINGS LIKE``'s ``_`` and ``%`` are wildcards, so the pattern can match
    more than intended and the row must still be picked by global name."""
    router.rows(
        "SHOW LISTINGS",
        LISTING_COLUMNS,
        [
            listing_row(name="SALESXDAILY", global_name="OTHER", title="Decoy"),
            listing_row(),
        ],
    )
    router.rows("DESCRIBE LISTING", DESCRIBE_LISTING_COLUMNS, [describe_listing_row()])
    router.rows("DESCRIBE SHARE", SHARE_COLUMNS, [share_row()])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    product = (await read_listing(make_client(http), listing_ref())).data_product

    assert product.name == "Daily sales"
    assert product.identities[0].native_key == "GZTSZAS2KH9"


async def test_a_ref_with_only_a_global_name_lists_every_listing_and_filters(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """``SHOW LISTINGS`` narrows on the local name, which a global-name-only ref lacks."""
    _route_listing(router)
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    product = (await read_listing(make_client(http), listing_ref(listing_name=None))).data_product

    assert router.body_for("SHOW LISTINGS")["statement"] == "SHOW LISTINGS"
    assert product.identities[0].native_key == "GZTSZAS2KH9"


async def test_a_listing_nobody_matches_raises_not_found(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.rows("SHOW LISTINGS", LISTING_COLUMNS, [listing_row(global_name="SOMETHING_ELSE")])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    with pytest.raises(NotFound) as caught:
        await read_listing(make_client(http), listing_ref())

    assert caught.value.native_key == "GZTSZAS2KH9"


async def test_describe_listing_detail_wins_over_the_show_row(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """``DESCRIBE`` is the more detailed of the two surfaces, so its values are merged on
    top."""
    router.rows("SHOW LISTINGS", LISTING_COLUMNS, [listing_row(title="Stale title")])
    router.rows(
        "DESCRIBE LISTING", DESCRIBE_LISTING_COLUMNS, [describe_listing_row(title="Fresh title")]
    )
    router.rows("DESCRIBE SHARE", SHARE_COLUMNS, [share_row()])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    product = (await read_listing(make_client(http), listing_ref())).data_product

    assert product.name == "Fresh title"


async def test_a_listing_with_no_title_falls_back_to_its_local_name(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    router.rows("SHOW LISTINGS", LISTING_COLUMNS, [listing_row(title="", share=None)])
    router.rows("DESCRIBE LISTING", DESCRIBE_LISTING_COLUMNS, [describe_listing_row(title="")])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]

    product = (await read_listing(make_client(http), listing_ref())).data_product

    assert product.name == "SALES_DAILY"


async def test_a_dataset_ref_is_rejected(
    http: HttpEndpoint, make_client: Callable[..., StatementClient]
) -> None:
    with pytest.raises(ValueError, match="DATA_PRODUCT"):
        await read_listing(make_client(http), dataset_ref())
