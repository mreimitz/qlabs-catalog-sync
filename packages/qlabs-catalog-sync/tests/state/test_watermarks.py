"""watermarks: round-trip write/read of the opaque per-(pair, endpoint, type) resume token."""

from __future__ import annotations

from datetime import UTC, datetime

from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.models import EntityType

RUN_AT = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


async def test_advance_watermark_round_trips_an_rfc3339_token(store: StateStore) -> None:
    async with store.unit_of_work() as uow:
        await uow.advance_watermark(
            "databricks-to-qlik",
            "databricks",
            EntityType.DATASET,
            "2026-08-20T11:59:00+00:00",
            run_at=RUN_AT,
        )

    watermark = await store.get_watermark("databricks-to-qlik", "databricks", EntityType.DATASET)
    assert watermark is not None
    assert watermark.watermark_token == "2026-08-20T11:59:00+00:00"
    assert watermark.last_status == "ok"
    assert watermark.last_run_at == RUN_AT
    assert watermark.updated_at == RUN_AT


async def test_advance_watermark_round_trips_an_opaque_page_cursor(store: StateStore) -> None:
    """The token is stored opaquely -- a page cursor is just as valid as a timestamp."""
    async with store.unit_of_work() as uow:
        await uow.advance_watermark(
            "collibra-to-qlik",
            "collibra",
            EntityType.GLOSSARY_TERM,
            "page-cursor:eyJvZmZzZXQiOiA0MjB9",
            status="partial",
            run_at=RUN_AT,
        )

    watermark = await store.get_watermark("collibra-to-qlik", "collibra", EntityType.GLOSSARY_TERM)
    assert watermark is not None
    assert watermark.watermark_token == "page-cursor:eyJvZmZzZXQiOiA0MjB9"
    assert watermark.last_status == "partial"


async def test_advance_watermark_allows_a_null_token(store: StateStore) -> None:
    """Before a source has ever been polled, there is no token yet."""
    async with store.unit_of_work() as uow:
        await uow.advance_watermark(
            "databricks-to-qlik",
            "databricks",
            EntityType.DATA_PRODUCT,
            None,
            status="error",
            run_at=RUN_AT,
        )

    watermark = await store.get_watermark(
        "databricks-to-qlik", "databricks", EntityType.DATA_PRODUCT
    )
    assert watermark is not None
    assert watermark.watermark_token is None
    assert watermark.last_status == "error"


async def test_advance_watermark_upserts_rather_than_duplicates(store: StateStore) -> None:
    later = datetime(2026, 8, 20, 12, 30, 0, tzinfo=UTC)

    async with store.unit_of_work() as uow:
        await uow.advance_watermark(
            "databricks-to-qlik", "databricks", EntityType.DATASET, "cursor-1", run_at=RUN_AT
        )
    async with store.unit_of_work() as uow:
        await uow.advance_watermark(
            "databricks-to-qlik", "databricks", EntityType.DATASET, "cursor-2", run_at=later
        )

    watermark = await store.get_watermark("databricks-to-qlik", "databricks", EntityType.DATASET)
    assert watermark is not None
    assert watermark.watermark_token == "cursor-2"
    assert watermark.updated_at == later


async def test_watermark_is_scoped_per_sync_pair_endpoint_and_entity_type(
    store: StateStore,
) -> None:
    async with store.unit_of_work() as uow:
        await uow.advance_watermark(
            "databricks-to-qlik", "databricks", EntityType.DATASET, "ds-cursor", run_at=RUN_AT
        )
        await uow.advance_watermark(
            "databricks-to-qlik",
            "databricks",
            EntityType.DATA_PRODUCT,
            "dp-cursor",
            run_at=RUN_AT,
        )

    dataset_wm = await store.get_watermark("databricks-to-qlik", "databricks", EntityType.DATASET)
    product_wm = await store.get_watermark(
        "databricks-to-qlik", "databricks", EntityType.DATA_PRODUCT
    )
    assert dataset_wm is not None and dataset_wm.watermark_token == "ds-cursor"
    assert product_wm is not None and product_wm.watermark_token == "dp-cursor"


async def test_unset_watermark_returns_none(store: StateStore) -> None:
    assert await store.get_watermark("nope", "nowhere", EntityType.CATEGORY) is None
