"""VCR contract tests -- Qlik write shapes (T8.5).

See ``conftest.py`` for what this directory pins and why every cassette is
hand-authored, never captured from a live tenant. This file pins:

* the users-API owner-email lookup (decision D3) plus the create POST body it feeds --
  ``keyContacts`` built from the resolved ``userId``, never from the raw email;
* the replace-only JSON Patch update (never ``add``/``remove``) against the closed
  eight-path enum, with the idempotency pre-read GET (``write.py`` module docstring,
  point 12a) and the ``if-match`` header carrying ``FieldDiff.expected_revision``.
"""

from __future__ import annotations

import vcr

from qlabs_catalog_sync_sdk.contract import EntityType, WriteOutcome
from qlabs_catalog_sync_sdk.envelope import to_json_value
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    FieldChange,
    FieldDiff,
    FieldUpdateMode,
    Party,
    PartyRole,
    Tag,
    TextField,
)
from qlabs_connector_qlik import Connector, write

from .conftest import (
    ENDPOINT,
    SPACE_ID,
    TENANT_BASE_URL,
    TENANT_ID,
    build_ctx,
    no_dataset_identity_binding,
    product_ref,
)


async def test_setup_and_create_pins_owner_resolution_and_post_body(
    qlik_contract_vcr: vcr.VCR,
) -> None:
    """``Connector.setup()`` then ``Connector.create()`` on a ``DataProduct`` carrying
    one owner email -- the connector's own two public entry points. Pins the users-API
    lookup (D3) and the create POST body (no /v1 segment) it feeds."""
    product = DataProduct(
        name="Contract VCR Create Product",
        description=TextField.plain("Created by T8.5"),
        documentation=TextField.markdown("# Contract VCR\nCreated by T8.5."),
        tags=[Tag(key="sales")],
        owners=[Party(email="ada@acme.example", role=PartyRole.OWNER)],
    )
    with qlik_contract_vcr.use_cassette("qlik_setup_and_create_with_owner_resolution.yaml"):
        connector = Connector()
        await connector.setup(build_ctx())
        try:
            result = await connector.create(product)
        finally:
            await connector.close()

    assert result.outcome is WriteOutcome.CREATED
    assert result.ref.native_key == "c1a2b3c4d5e6f7a8b9c0d1e2"
    assert result.source_revision == 'W/"contract-vcr-create-rev-1"'
    assert "owners" in result.written_fields
    assert result.skipped_fields == []


async def test_update_pins_preread_and_replace_only_patch_with_if_match(
    qlik_contract_vcr: vcr.VCR,
) -> None:
    """``write.QlikWriter.update()`` called directly against a plain ``HttpEndpoint``
    (no OAuth) -- the same ``update()`` the connector calls at runtime."""
    async with HttpEndpoint(TENANT_BASE_URL, auth=("Bearer", "contract-vcr-static-token")) as http:
        writer = write.build_writer(
            http,
            endpoint=ENDPOINT,
            tenant_id=TENANT_ID,
            space_id=SPACE_ID,
            dataset_identity_lookup=no_dataset_identity_binding,
        )
        one_change = FieldChange(
            field="name",
            mode=FieldUpdateMode.REPLACE,
            value=to_json_value("Updated Product Name"),
        )
        diff = FieldDiff(
            entity_type=EntityType.DATA_PRODUCT,
            changes=[one_change],
            expected_revision='W/"contract-vcr-update-rev-1"',
        )
        with qlik_contract_vcr.use_cassette("qlik_update_data_product_patch.yaml"):
            result = await writer.update(product_ref("d4e5f6a7b8c9d0e1f2a3b4c5"), diff)

    assert result.outcome is WriteOutcome.UPDATED
    assert result.written_fields == ["name"]
    assert result.source_revision == 'W/"contract-vcr-update-rev-2"'
