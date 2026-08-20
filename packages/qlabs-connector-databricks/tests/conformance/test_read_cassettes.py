"""Hand-authored ``vcrpy`` cassette tests -- the recorded-response half of this task's
brief, alongside the respx unit mocks everywhere else in this directory.

**These cassettes are hand-authored from documentation, not captured from a live
Databricks tenant.** RM-01 is explicitly built without live tenants (decision-databricks-
to-qlik-mvp.md: "all tests run against respx mocks, an SDK-provided fake connector, and
hand-authored cassettes"). Every recorded interaction's request/response body was typed
by hand from the field shapes RS-01 documents (``databricks-catalog-api-reference.md``
sections 1.2, 1.3, 3.2, 3.6) -- see each cassette YAML file's own header comment. The
cassette *files* are still well-formed ``vcrpy`` YAML (correct top-level ``version``/
``interactions`` shape, ``request``/``response`` sub-shape) because that structure was
produced by dumping this same hand-typed content through ``vcrpy``'s own serializer
rather than risking a hand-rolled YAML syntax error; the *content* — every field, value,
and URL — is authored from RS-01, never observed from a real workspace.

What these prove that the respx-mocked tests elsewhere in this directory do not: that
the connector's read path plays back correctly against a *recorded fixture* format (the
one a real recording, made once a live workspace was available, could drop in without
any test code changing) rather than only against synthetic in-test mocks. ``harness.py``
(SDK) draws exactly this distinction in its own module docstring: "respx is for unit
tests against synthetic responses ... vcrpy cassettes are for a recorded fixture of what
a real endpoint actually returned, replayed without live credentials in CI."

Three cassettes, three call shapes:

* ``databricks_connector_setup_and_read_schema.yaml`` -- the full public entry points
  (``Connector.setup()`` then ``Connector.read()``), OAuth token fetch included.
* ``databricks_read_schema_direct.yaml`` -- ``read.read_schema()`` called directly
  against a bare ``HttpEndpoint``, so the test can assert on the full ``SchemaRead``
  (the data product *and* its member datasets) that ``Connector.read()`` alone only
  returns half of (see ``read.py``'s own docstring on why ``read_entity`` discards the
  datasets half for a ``DATA_PRODUCT`` ref).
* ``databricks_read_dataset_direct.yaml`` -- ``read.read_dataset()`` for a single table,
  no listing, no OAuth.
"""

from __future__ import annotations

import vcr

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import AssetType, EntityType, IdentityRef, Party
from qlabs_connector_databricks import read

from .conftest import ENDPOINT, HOST, METASTORE_ID, setup_connector


def _schema_ref() -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key="b4b7c3d2-4e9a-4b1b-9a3f-2e6d9c8a7f10",
        tenant_id=METASTORE_ID,
        secondary_keys={"full_name": "prod.sales"},
    )


def _orders_ref() -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key="c5d8e4f3-5f0b-4c2c-8b4a-3f7e0d9b8a21",
        tenant_id=METASTORE_ID,
        secondary_keys={"full_name": "prod.sales.orders"},
    )


async def test_connector_setup_and_read_data_product_end_to_end(databricks_vcr: vcr.VCR) -> None:
    """``Connector.setup()`` (OAuth M2M) then ``Connector.read()`` on a ``DATA_PRODUCT``
    ref, played back from a hand-authored cassette -- the connector's own two public
    entry points, exercised exactly as the engine would call them."""
    with databricks_vcr.use_cassette("databricks_connector_setup_and_read_schema.yaml"):
        async with setup_connector(sql_warehouse_id=None, mock_oauth=False) as connector:
            data_product = await connector.read(_schema_ref())

    assert data_product.name == "sales"
    assert data_product.description is not None
    assert data_product.description.text == "Sales domain schema, owned by data engineering."
    assert data_product.owners == [Party(email="data-eng@acme.com", role="owner")]
    assert data_product.custom_attributes["properties"] == {"team": "sales"}
    for field in ("name", "description", "owners"):
        assert field in data_product.field_envelopes


async def test_read_schema_direct_delivers_the_schema_and_its_datasets(
    databricks_vcr: vcr.VCR,
) -> None:
    async with HttpEndpoint(HOST) as http:
        with databricks_vcr.use_cassette("databricks_read_schema_direct.yaml"):
            schema_read = await read.read_schema(
                http, _schema_ref(), catalog_schema_patterns=["prod.*"]
            )

    assert schema_read.data_product.name == "sales"
    assert {dataset.name for dataset in schema_read.datasets} == {"orders", "customers"}

    orders = next(d for d in schema_read.datasets if d.name == "orders")
    assert orders.asset_type is AssetType.TABLE
    assert orders.physical_ref == "prod.sales.orders"
    assert orders.description is not None
    assert orders.description.text == "One row per customer order."
    assert orders.custom_attributes["properties"] == {"delta.minReaderVersion": "1"}

    customers = next(d for d in schema_read.datasets if d.name == "customers")
    assert customers.asset_type is AssetType.VIEW
    assert customers.physical_ref == "prod.sales.customers"


async def test_read_dataset_direct_delivers_one_table(databricks_vcr: vcr.VCR) -> None:
    async with HttpEndpoint(HOST) as http:
        with databricks_vcr.use_cassette("databricks_read_dataset_direct.yaml"):
            dataset = await read.read_dataset(http, _orders_ref())

    assert dataset.name == "orders"
    assert dataset.asset_type is AssetType.TABLE
    assert dataset.physical_ref == "prod.sales.orders"
    assert dataset.owners == [Party(email="data-eng@acme.com", role="owner")]
    assert dataset.custom_attributes["table_type"] == "MANAGED"
    assert dataset.custom_attributes["data_source_format"] == "DELTA"
