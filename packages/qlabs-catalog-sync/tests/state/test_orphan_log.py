"""orphan_log: D4 -- record what vanished, where, and when it was first and last seen."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.models import EntityType

FIRST_SEEN_MISSING = datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC)
STILL_MISSING = datetime(2026, 8, 20, 10, 0, 0, tzinfo=UTC)
LAST_SEEN_PRESENT = datetime(2026, 8, 19, 9, 0, 0, tzinfo=UTC)


async def test_record_orphan_round_trips(store: StateStore) -> None:
    neutral_id = uuid4()

    async with store.unit_of_work() as uow:
        await uow.record_orphan(
            neutral_id,
            "databricks",
            EntityType.DATASET,
            native_key="main.sales.orders",
            last_seen_at=LAST_SEEN_PRESENT,
            observed_at=FIRST_SEEN_MISSING,
        )

    orphans = await store.list_orphans("databricks")
    assert len(orphans) == 1
    record = orphans[0]
    assert record.neutral_id == neutral_id
    assert record.endpoint == "databricks"
    assert record.entity_type is EntityType.DATASET
    assert record.native_key == "main.sales.orders"
    assert record.first_missing_at == FIRST_SEEN_MISSING
    assert record.last_missing_at == FIRST_SEEN_MISSING
    assert record.last_seen_at == LAST_SEEN_PRESENT
    assert record.resolved_at is None


async def test_repeated_detection_advances_last_missing_but_not_first(store: StateStore) -> None:
    neutral_id = uuid4()

    async with store.unit_of_work() as uow:
        await uow.record_orphan(
            neutral_id,
            "databricks",
            EntityType.DATASET,
            native_key="main.sales.orders",
            last_seen_at=LAST_SEEN_PRESENT,
            observed_at=FIRST_SEEN_MISSING,
        )
    async with store.unit_of_work() as uow:
        await uow.record_orphan(
            neutral_id,
            "databricks",
            EntityType.DATASET,
            native_key="main.sales.orders",
            last_seen_at=None,
            observed_at=STILL_MISSING,
        )

    orphans = await store.list_orphans("databricks")
    assert len(orphans) == 1
    record = orphans[0]
    assert record.first_missing_at == FIRST_SEEN_MISSING
    assert record.last_missing_at == STILL_MISSING


async def test_resolve_orphan_clears_it_from_the_unresolved_report(store: StateStore) -> None:
    neutral_id = uuid4()
    resolved_at = datetime(2026, 8, 20, 11, 0, 0, tzinfo=UTC)

    async with store.unit_of_work() as uow:
        await uow.record_orphan(
            neutral_id,
            "databricks",
            EntityType.DATASET,
            native_key="main.sales.orders",
            last_seen_at=None,
            observed_at=FIRST_SEEN_MISSING,
        )
    async with store.unit_of_work() as uow:
        await uow.resolve_orphan(neutral_id, "databricks", EntityType.DATASET, now=resolved_at)

    assert await store.list_orphans("databricks") == []
    assert (
        await store.list_orphans("databricks", unresolved_only=False)
    )[0].resolved_at == resolved_at


async def test_orphan_that_reappears_and_vanishes_again_reopens(store: StateStore) -> None:
    neutral_id = uuid4()

    async with store.unit_of_work() as uow:
        await uow.record_orphan(
            neutral_id,
            "databricks",
            EntityType.DATASET,
            native_key="main.sales.orders",
            last_seen_at=None,
            observed_at=FIRST_SEEN_MISSING,
        )
    async with store.unit_of_work() as uow:
        await uow.resolve_orphan(neutral_id, "databricks", EntityType.DATASET, now=STILL_MISSING)
    async with store.unit_of_work() as uow:
        await uow.record_orphan(
            neutral_id,
            "databricks",
            EntityType.DATASET,
            native_key="main.sales.orders",
            last_seen_at=None,
            observed_at=datetime(2026, 8, 21, 0, 0, 0, tzinfo=UTC),
        )

    orphans = await store.list_orphans("databricks")
    assert len(orphans) == 1
    assert orphans[0].resolved_at is None


async def test_list_orphans_scoped_by_endpoint(store: StateStore) -> None:
    async with store.unit_of_work() as uow:
        await uow.record_orphan(
            uuid4(),
            "databricks",
            EntityType.DATASET,
            native_key="a",
            last_seen_at=None,
            observed_at=FIRST_SEEN_MISSING,
        )
        await uow.record_orphan(
            uuid4(),
            "qlik",
            EntityType.DATASET,
            native_key="b",
            last_seen_at=None,
            observed_at=FIRST_SEEN_MISSING,
        )

    assert len(await store.list_orphans("databricks")) == 1
    assert len(await store.list_orphans("qlik")) == 1
    assert len(await store.list_orphans()) == 2
