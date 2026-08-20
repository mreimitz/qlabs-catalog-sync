"""VCR contract tests -- Qlik read shapes (T8.5).

See ``conftest.py`` for what this directory is for and why every cassette here is
hand-authored, never captured from a live tenant. This file pins:

* the OAuth2 client-credentials token exchange (JSON body -- Qlik, unlike Databricks,
  is not form-encoded);
* the data-governance data-product GET, **no /v1 segment** -- the real trap this task's
  brief calls out, since the Items/Users API family one line below *does* carry /v1;
* the Items API single-item GET, specifically ``resourceAttributes.secureQri`` as the
  primary dataset identity.

``test_qlik_contract_vcr_altered_cassettes.py`` reuses the second cassette here
(``qlik_read_dataset_item.yaml``) as the base for its "renamed field" proof.
"""

from __future__ import annotations

import vcr

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import DataProductStatus
from qlabs_connector_qlik import Connector, read

from .conftest import ENDPOINT, TENANT_BASE_URL, build_ctx, dataset_ref, product_ref


async def test_connector_setup_and_read_pins_oauth_and_data_product_shape(
    qlik_contract_vcr: vcr.VCR,
) -> None:
    """``Connector.setup()`` (no I/O of its own) then ``Connector.read()`` on a
    ``DATA_PRODUCT`` ref -- the connector's own two public entry points, exercised
    exactly as the engine would call them."""
    with qlik_contract_vcr.use_cassette("qlik_setup_and_read_data_product.yaml"):
        connector = Connector()
        await connector.setup(build_ctx())
        try:
            data_product = await connector.read(product_ref("9a1b2c3d4e5f60718293a4b6"))
        finally:
            await connector.close()

    assert data_product.name == "Contract VCR Data Product"
    assert data_product.status is DataProductStatus.ACTIVE
    assert {tag.key for tag in data_product.tags} == {"sales", "revenue"}
    assert data_product.field_envelopes["name"].source_revision == 'W/"contract-vcr-rev-1"'


async def test_read_dataset_item_pins_items_api_and_secure_qri(
    qlik_contract_vcr: vcr.VCR,
) -> None:
    """``read.read_dataset()`` called directly against a plain ``HttpEndpoint`` (no
    OAuth) -- pins the Items API single-item GET and the ``secureQri``-first identity
    rule (``read.py`` module docstring, point 1)."""
    async with HttpEndpoint(
        TENANT_BASE_URL, auth=("Bearer", "contract-vcr-static-token")
    ) as http:
        with qlik_contract_vcr.use_cassette("qlik_read_dataset_item.yaml"):
            dataset = await read.read_dataset(
                http, dataset_ref("item-contract-vcr-orders-1"), endpoint=ENDPOINT
            )

    assert dataset.name == "orders"
    identity = dataset.identity_for(ENDPOINT)
    assert identity is not None
    assert identity.native_key == (
        "qdf-secure:contract-vcr-tenant:710b3f4c5d6e7f8a9b0c1d2e:ds-orders-contract-1"
    )
    assert {tag.key for tag in dataset.tags} == {"finance", "orders"}
