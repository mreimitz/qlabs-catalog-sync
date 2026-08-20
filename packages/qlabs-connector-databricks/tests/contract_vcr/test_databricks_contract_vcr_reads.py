"""VCR contract tests -- Databricks OAuth + schema/table read shapes (T8.5).

See ``conftest.py`` for what this directory is for and why every cassette here is
hand-authored, never captured from a live workspace. This file pins the OAuth M2M
token exchange (form-encoded body, HTTP Basic client auth, ``scope=all-apis`` -- a real
difference from Qlik's JSON-bodied OAuth2, see
``test_qlik_contract_vcr_reads.py`` in the sibling package) plus a single-page
``/schemas`` + ``/tables`` listing, exercised through the connector's own public
entry points.
"""

from __future__ import annotations

from collections.abc import Callable

import vcr

from qlabs_connector_databricks import Connector

from .conftest import build_ctx, schema_ref


async def test_connector_setup_and_read_pins_oauth_and_schema_shape(
    databricks_contract_vcr: vcr.VCR,
    fake_workspace_client_factory: Callable[..., object],
) -> None:
    """``Connector.setup()`` (OAuth M2M) followed by ``Connector.read()`` on a
    ``DATA_PRODUCT`` ref -- the connector's own two public entry points, called
    exactly as the engine would call them."""
    connector = Connector(workspace_client_factory=fake_workspace_client_factory)
    ref = schema_ref(native_key="11112222-3333-4444-5555-666677778888", full_name="prod.sales")
    with databricks_contract_vcr.use_cassette("databricks_setup_and_read_schema.yaml"):
        await connector.setup(build_ctx())
        try:
            data_product = await connector.read(ref)
        finally:
            await connector.close()

    assert data_product.name == "sales"
    assert data_product.description is not None
    assert data_product.description.text == "Sales domain schema."
    assert data_product.owners[0].email == "data-eng@contract-vcr.example"
