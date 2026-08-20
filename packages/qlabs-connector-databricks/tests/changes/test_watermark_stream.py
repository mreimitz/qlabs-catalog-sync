"""``list_changed`` respects the requested entity type and the watermark's stream.

* A ``since`` watermark for a different endpoint or entity type is rejected outright —
  the engine should never be able to hand this connector another stream's resume state
  by mistake.
* The returned ``next_watermark`` is always on the exact stream that was requested.
* A ``ChangeRef`` that disagrees with its ``ListChangedResult``'s watermark stream is
  rejected by the SDK's own contract — proven directly against the real
  ``ListChangedResult`` validator (this module never constructs one that would trip it,
  which is exactly what the rest of this test suite already demonstrates every time it
  asserts on a real result; this test pins down *why* that is safe).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from qlabs_catalog_sync_sdk.contract import (
    ChangeKind,
    ChangeRef,
    EntityType,
    IdentityRef,
    ListChangedResult,
    Watermark,
)
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.changes import list_changed

from .conftest import CATALOGS_PATH, ENDPOINT, METASTORE_ID, catalog, mock_single_page


async def test_since_for_a_different_endpoint_is_rejected(http: HttpEndpoint) -> None:
    mismatched = Watermark.initial("some-other-endpoint", EntityType.DATA_PRODUCT)

    with pytest.raises(ValueError, match="stream"):
        await list_changed(
            http,
            EntityType.DATA_PRODUCT,
            mismatched,
            endpoint=ENDPOINT,
        )


async def test_since_for_a_different_entity_type_is_rejected(http: HttpEndpoint) -> None:
    mismatched = Watermark.initial(ENDPOINT, EntityType.DATASET)

    with pytest.raises(ValueError, match="stream"):
        await list_changed(
            http,
            EntityType.DATA_PRODUCT,
            mismatched,
            endpoint=ENDPOINT,
        )


async def test_next_watermark_is_on_the_requested_stream(respx_mock, http: HttpEndpoint) -> None:
    mock_single_page(
        respx_mock, CATALOGS_PATH, params={}, items_key="catalogs", items=[catalog("main")]
    )
    mock_single_page(
        respx_mock,
        "/api/2.1/unity-catalog/schemas",
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[],
    )

    result = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
    )

    assert result.next_watermark.endpoint == ENDPOINT
    assert result.next_watermark.entity_type is EntityType.DATA_PRODUCT


def test_a_change_ref_on_the_wrong_stream_is_rejected_by_the_contract() -> None:
    """The SDK's own guard: ``ListChangedResult`` refuses a ``ChangeRef`` whose
    endpoint/entity_type disagrees with ``next_watermark``'s stream. This is what makes
    it safe that ``changes.py`` always builds every ``ChangeRef`` from the same
    ``endpoint``/``entity_type`` it was asked for and passes to ``next_watermark``."""
    watermark = Watermark.at(ENDPOINT, EntityType.DATA_PRODUCT, datetime(2026, 1, 1, tzinfo=UTC))
    wrong_stream_change = ChangeRef(
        ref=IdentityRef(
            endpoint=ENDPOINT,
            entity_type=EntityType.DATASET,  # disagrees with the watermark above
            native_key="tbl-id::main.sales.orders",
            tenant_id=METASTORE_ID,
        ),
        kind=ChangeKind.UPSERT,
    )

    with pytest.raises(ValidationError):
        ListChangedResult(changes=[wrong_stream_change], next_watermark=watermark)
