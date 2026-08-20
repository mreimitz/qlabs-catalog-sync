"""Hand-authored ``vcrpy`` cassette tests — the recorded-response half of this task's
brief, alongside the respx-mocked tests everywhere else in this directory.

**These cassettes are hand-authored from documentation, not captured from a live Qlik
Cloud tenant.** RM-01 is explicitly built without live tenants
(``decision-databricks-to-qlik-mvp.md`` D8 / the agent guide's "no live tenants" rule —
the same convention T4.6's Databricks cassettes state plainly, see
``packages/qlabs-connector-databricks/tests/conformance/test_read_cassettes.py``). Every
field, value and URL in ``tests/cassettes/qlik_*.yaml`` was typed by hand from the shapes
RS-02 documents — ``qlik-two-way-sync-readiness.md`` section 2 (the data-product create
body and its identity/audit fields) and ``qlik-catalog-api-reference.md`` section 1.1 (the
Items API envelope, ``resourceAttributes.secureQri``) — never observed from a real
tenant. The cassette *files* are still well-formed ``vcrpy`` YAML (produced by dumping
this same hand-typed content through ``vcrpy``'s own serializer, exactly like the
Databricks cassettes' own header comments describe) — the *content* is authored from
research, not recorded.

What these prove that the respx-mocked tests elsewhere in this directory do not: that the
connector's read path plays back correctly against a *recorded fixture* format (the one a
real recording, made once a live tenant is available, could drop in without any test code
changing), not only against synthetic in-test mocks built by this same suite.
``qlabs_catalog_sync_sdk.conformance.harness``'s own module docstring draws exactly this
distinction: "respx is for unit tests against synthetic responses ... vcrpy cassettes are
for a recorded fixture of what a real endpoint actually returned, replayed without live
credentials in CI."

Three cassettes, three call shapes (mirroring the Databricks module's own three-cassette
structure):

* ``qlik_connector_setup_and_read_data_product.yaml`` — the full public entry points
  (``Connector.setup()`` then ``Connector.read()``), OAuth2 token fetch included. Qlik's
  ``setup()`` does no I/O itself (the token is fetched lazily on first use — see
  ``__init__.py``), so this cassette's first interaction is the token POST triggered by
  the ``read()`` call, not by ``setup()``.
* ``qlik_read_data_product_direct.yaml`` — ``read.read_data_product()`` called directly
  against a bare ``HttpEndpoint`` (no OAuth), asserting on the full mapped
  ``DataProduct`` — tags, owners (``ownerId`` + ``keyContacts``), status from
  ``activated``, and the ``ETag`` becoming ``source_revision``.
* ``qlik_read_dataset_direct.yaml`` — ``read.read_dataset()`` for a single Items-API
  item, no OAuth, no listing — asserting ``secureQri`` becomes the primary native key and
  ``meta.tags`` becomes the neutral ``tags``.
"""

from __future__ import annotations

import vcr

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import DataProductStatus, EntityType, IdentityRef, PartyRole
from qlabs_connector_qlik import Connector, read

from .conftest import ENDPOINT, TENANT_BASE_URL, TENANT_ID, build_ctx

PRODUCT_ID = "8894f0d9c3a4446edd5f3e48"
DATASET_ITEM_ID = "item-conformance-orders-1"


def _product_ref() -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=PRODUCT_ID,
        tenant_id=TENANT_ID,
    )


def _dataset_ref() -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key=DATASET_ITEM_ID,
        tenant_id=TENANT_ID,
        secondary_keys={"id": DATASET_ITEM_ID},
    )


async def test_connector_setup_and_read_data_product_end_to_end(qlik_vcr: vcr.VCR) -> None:
    """``Connector.setup()`` (no I/O) then ``Connector.read()`` on a ``DATA_PRODUCT``
    ref — the connector's own two public entry points, exercised exactly as the engine
    would call them, played back from a hand-authored cassette."""
    with qlik_vcr.use_cassette("qlik_connector_setup_and_read_data_product.yaml"):
        connector = Connector()
        await connector.setup(build_ctx())
        try:
            data_product = await connector.read(_product_ref())
        finally:
            await connector.close()

    assert data_product.name == "Sales Analytics Data Product"
    assert data_product.description is not None
    assert data_product.description.text == "Curated sales datasets"
    assert data_product.status is DataProductStatus.ACTIVE
    assert data_product.tags[0].key == "sales"
    for field in ("name", "description", "status", "tags"):
        assert field in data_product.field_envelopes


async def test_read_data_product_direct_delivers_the_full_mapping(qlik_vcr: vcr.VCR) -> None:
    async with HttpEndpoint(TENANT_BASE_URL, auth=("Bearer", "test-token")) as http:
        with qlik_vcr.use_cassette("qlik_read_data_product_direct.yaml"):
            data_product = await read.read_data_product(http, _product_ref(), endpoint=ENDPOINT)

    assert data_product.name == "Sales Analytics Data Product"
    assert data_product.documentation is not None
    assert data_product.documentation.text == "# Sales\nCurated sales datasets."
    assert {tag.key for tag in data_product.tags} == {"sales", "revenue"}
    # ownerId (role OWNER) plus keyContacts, deduped by user id (read.py's own rule).
    owner_ids = {party.party_id for party in data_product.owners}
    assert owner_ids == {"6909d8524392dbbab822c7f7", "7a1b2c3d4e5f60718293a4b5"}
    steward = next(p for p in data_product.owners if p.party_id == "7a1b2c3d4e5f60718293a4b5")
    assert steward.role is PartyRole.STEWARD
    assert data_product.placement == "610a2f3b4c5d6e7f8a9b0c1d"
    # The ETag response header becomes source_revision on every field envelope.
    assert data_product.field_envelopes["name"].source_revision == 'W/"rev-9"'
    # Unmapped raw keys survive byte-identical in custom_attributes.
    assert data_product.custom_attributes["pendingChangesCount"] == 0
    assert data_product.custom_attributes["qri"] == f"qri:data-product://{PRODUCT_ID}"


async def test_read_dataset_direct_delivers_one_item(qlik_vcr: vcr.VCR) -> None:
    async with HttpEndpoint(TENANT_BASE_URL, auth=("Bearer", "test-token")) as http:
        with qlik_vcr.use_cassette("qlik_read_dataset_direct.yaml"):
            dataset = await read.read_dataset(http, _dataset_ref(), endpoint=ENDPOINT)

    assert dataset.name == "orders"
    identity = dataset.identity_for(ENDPOINT)
    assert identity is not None
    expected_secure_qri = (
        "qdf-secure:acme-conformance-tenant:610a2f3b4c5d6e7f8a9b0c1d:ds-orders-1"
    )
    assert identity.native_key == expected_secure_qri
    assert identity.secondary_keys["id"] == DATASET_ITEM_ID
    assert identity.secondary_keys["resourceId"] == "ds-orders-1"
    assert dataset.physical_ref == identity.native_key
    assert {tag.key for tag in dataset.tags} == {"finance", "orders"}
    assert len(dataset.owners) == 1
    assert dataset.owners[0].party_id == "6909d8524392dbbab822c7f7"
