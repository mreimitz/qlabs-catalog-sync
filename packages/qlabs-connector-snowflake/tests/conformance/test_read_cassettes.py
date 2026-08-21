"""Hand-authored ``vcrpy`` cassette tests -- the recorded-response half of T6.6's brief,
alongside the respx unit mocks everywhere else in this directory.

**These cassettes are hand-authored from documentation, not captured from a live
Snowflake account.** There is no live tenant for this build (``read.py``'s own module
docstring flags every response shape it assumes as TENANT-UNVERIFIED for the same
reason), so every request and response body was produced from the field shapes RS-05
documents -- see each cassette YAML's own header comment for the sections. The cassette
*files* are well-formed ``vcrpy`` YAML because that structure was written by ``vcrpy``'s
own serializer rather than hand-rolled; the *content* is authored from RS-05.

What these prove that the respx-mocked tests elsewhere in this directory do not: the read
path plays back correctly against a *recorded fixture* format -- the one a real recording,
made once an account is available, could drop in without any test code changing.
``harness.py`` (SDK) draws exactly this distinction: "respx is for unit tests against
synthetic responses ... vcrpy cassettes are for a recorded fixture of what a real endpoint
actually returned, replayed without live credentials in CI."

**Matching, and why it is not the harness default.** ``StatementClient.execute`` stamps a
fresh ``requestId`` UUID on every statement (RS-05 section 3.8), so ``vcr_config``'s
default ``match_on``, which includes ``query``, could never match a replayed run. The
``snowflake_vcr`` fixture matches on method + path instead (``conftest.CASSETTE_MATCH_ON``),
which makes every ``POST /api/v2/statements`` interaction equivalent and lets vcrpy serve
them in recorded order. That ordering is a real property worth pinning: the assertions
below only hold if the connector issues its statements in the order ``read.py`` documents,
because a swapped pair would be answered with the wrong result set and fail loudly.

Three cassettes, three call shapes:

* ``snowflake_connector_setup_and_read_dataset.yaml`` -- the public entry points
  (``Connector.setup()``, ``healthcheck()``, ``Connector.read()``) on a ``DATASET`` ref.
* ``snowflake_read_schema_direct.yaml`` -- ``read.read_schema()`` called directly, so the
  test can assert on the whole ``DataProductRead`` (the data product *and* its member
  datasets) that ``Connector.read()`` alone returns only half of.
* ``snowflake_read_listing_direct.yaml`` -- ``read.read_listing()``, including the
  ``DESCRIBE SHARE`` enrichment beneath the listing.
"""

from __future__ import annotations

import vcr

from qlabs_catalog_sync_sdk.contract import HealthState
from qlabs_catalog_sync_sdk.models import AssetType, DataProductStatus, TextFormat
from qlabs_connector_snowflake import read

from .conftest import (
    LISTING_GLOBAL_NAME,
    SCHEMA_FQN,
    TABLE,
    TABLE_FQN,
    dataset_ref,
    listing_ref,
    schema_ref,
    setup_connector,
    statement_client,
)


async def test_connector_healthcheck_and_read_dataset_end_to_end(snowflake_vcr: vcr.VCR) -> None:
    """``setup()`` -> ``healthcheck()`` -> ``read()`` on a ``DATASET`` ref, played back
    from a hand-authored cassette -- the connector's public entry points, exercised
    exactly as the engine calls them.

    ``setup()`` itself contributes no interaction: Snowflake's key-pair JWT is signed
    locally (``auth.py``), so unlike the Databricks connector's OAuth fetch there is no
    token round trip for a cassette to carry.
    """
    with snowflake_vcr.use_cassette("snowflake_connector_setup_and_read_dataset.yaml") as tape:
        async with setup_connector() as connector:
            status = await connector.healthcheck()
            dataset = await connector.read(dataset_ref())

    assert tape.all_played, "every recorded interaction must be consumed"
    assert tape.play_count == 4, "the probe plus read.py's three dataset statements"
    assert status.state is HealthState.HEALTHY
    assert dataset.name == TABLE
    assert dataset.asset_type is AssetType.TABLE
    assert dataset.physical_ref == TABLE_FQN
    assert dataset.description is not None
    assert dataset.description.text == "Order header rows, one per checkout."
    assert [party.display_name for party in dataset.owners] == ["SALES_ENGINEER"]
    assert [(tag.key, tag.value) for tag in dataset.tags] == [
        ("GOVERNANCE.TAGS.COST_CENTER", "commerce")
    ]
    assert dataset.classifications == ["PRIVACY_CATEGORY=IDENTIFIER"]
    assert dataset.custom_attributes["TABLE_TYPE"] == "BASE TABLE"
    for field_name in ("name", "description", "owners", "tags", "classifications"):
        assert field_name in dataset.field_envelopes


async def test_read_schema_direct_delivers_the_schema_and_its_datasets(
    snowflake_vcr: vcr.VCR,
) -> None:
    """The member half of a schema read, which ``Connector.read()`` discards."""
    async with statement_client() as client:
        with snowflake_vcr.use_cassette("snowflake_read_schema_direct.yaml") as tape:
            schema_read = await read.read_schema(client, schema_ref())

    assert tape.all_played
    assert tape.play_count == 4, "SCHEMATA, schema tags, member TABLES, member tag index"
    data_product = schema_read.data_product
    assert data_product.name == "PUBLIC"
    assert data_product.identities[0].native_key == SCHEMA_FQN
    assert data_product.description is not None
    assert data_product.description.text == "Conformed sales dimensions and facts."
    assert [party.display_name for party in data_product.owners] == ["SYSADMIN"]
    assert [(tag.key, tag.value) for tag in data_product.tags] == [
        ("GOVERNANCE.TAGS.DOMAIN", "sales")
    ]

    assert schema_read.member_object_names == [TABLE_FQN]
    assert [dataset.name for dataset in schema_read.datasets] == [TABLE]
    member = schema_read.datasets[0]
    assert member.asset_type is AssetType.TABLE
    # The member's tags come from the account-wide index (one statement for the whole
    # schema), not from a per-table call -- read.py's own reason for that statement.
    assert [tag.key for tag in member.tags] == ["GOVERNANCE.TAGS.COST_CENTER"]


async def test_read_listing_direct_delivers_the_listing_and_its_share(
    snowflake_vcr: vcr.VCR,
) -> None:
    """``SHOW LISTINGS`` + ``DESCRIBE LISTING`` merged, with the share composition folded
    into ``custom_attributes`` -- the only sense in which this connector reads shares."""
    async with statement_client() as client:
        with snowflake_vcr.use_cassette("snowflake_read_listing_direct.yaml") as tape:
            listing_read = await read.read_listing(client, listing_ref())

    assert tape.all_played
    assert tape.play_count == 3, "SHOW LISTINGS, DESCRIBE LISTING, DESCRIBE SHARE"
    data_product = listing_read.data_product
    assert data_product.identities[0].native_key == LISTING_GLOBAL_NAME
    assert data_product.name == "Daily sales"
    assert data_product.description is not None
    assert data_product.description.text == "Daily sales by region, refreshed nightly"
    assert data_product.documentation is not None
    assert data_product.documentation.format is TextFormat.MARKDOWN
    assert data_product.status is DataProductStatus.ACTIVE

    assert listing_read.datasets == []
    assert listing_read.member_object_names == [TABLE_FQN]
    composition = data_product.custom_attributes[read.SHARE_COMPOSITION_KEY]
    assert composition == [
        {
            "created_on": "1700000000.000000000",
            "kind": "TABLE",
            "name": TABLE_FQN,
            "shared_on": "1700000000.000000000",
        }
    ]
