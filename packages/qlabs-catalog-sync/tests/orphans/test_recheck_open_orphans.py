"""``recheck_open_orphans`` -- the lifecycle check for orphans ``run_cycle`` already
flagged: reconfirm, or resolve, independently of any further ``list_changed`` page.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from orphans_helpers import bind, seed_product

from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.orphans import recheck_open_orphans
from qlabs_catalog_sync_sdk.models import EntityType, IdentityRef
from qlabs_catalog_sync_sdk.testing import DEFAULT_TENANT_ID, FakeConnector

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 20, 13, 0, 0, tzinfo=UTC)


async def test_reconfirms_an_orphan_run_cycle_already_flagged(
    store: StateStore, source: FakeConnector
) -> None:
    ref = seed_product(source, "sales.orders")
    neutral_id = await bind(store, ref)
    source.vanish(ref)
    async with store.unit_of_work() as uow:
        await uow.record_orphan(
            neutral_id,
            source.name,
            EntityType.DATA_PRODUCT,
            native_key="sales.orders",
            last_seen_at=None,
            observed_at=NOW,
        )

    report = await recheck_open_orphans(
        store, source, entity_type=EntityType.DATA_PRODUCT, now=LATER
    )

    assert [o.neutral_id for o in report.still_missing] == [neutral_id]
    stored = (await store.list_orphans(source.name, unresolved_only=True))[0]
    assert stored.first_missing_at == NOW
    assert stored.last_missing_at == LATER


async def test_resolves_an_orphan_that_has_come_back(
    store: StateStore, source: FakeConnector
) -> None:
    """A schema recreated, a transient glitch cleared: the object is there again."""
    ref = seed_product(source, "sales.orders", description="Order facts")
    neutral_id = await bind(store, ref)
    source.vanish(ref)
    async with store.unit_of_work() as uow:
        await uow.record_orphan(
            neutral_id,
            source.name,
            EntityType.DATA_PRODUCT,
            native_key="sales.orders",
            last_seen_at=None,
            observed_at=NOW,
        )
    seed_product(source, "sales.orders", description="Order facts")

    report = await recheck_open_orphans(
        store, source, entity_type=EntityType.DATA_PRODUCT, now=LATER
    )

    assert list(report.resolved) == [neutral_id]
    assert await store.list_orphans(source.name, unresolved_only=True) == []


async def test_no_open_orphans_is_a_no_op(store: StateStore, source: FakeConnector) -> None:
    report = await recheck_open_orphans(store, source, entity_type=EntityType.DATA_PRODUCT, now=NOW)
    assert report.checked == 0


async def test_scoped_to_one_entity_type(store: StateStore, source: FakeConnector) -> None:
    """An open orphan of a different entity type is out of scope for this call, and is
    left exactly as it was."""
    dataset_ref = IdentityRef(
        endpoint=source.name,
        entity_type=EntityType.DATASET,
        native_key="main.sales.orders_tbl",
        tenant_id=DEFAULT_TENANT_ID,
    )
    neutral_id = await bind(store, dataset_ref)
    async with store.unit_of_work() as uow:
        await uow.record_orphan(
            neutral_id,
            source.name,
            EntityType.DATASET,
            native_key="main.sales.orders_tbl",
            last_seen_at=None,
            observed_at=NOW,
        )

    report = await recheck_open_orphans(
        store, source, entity_type=EntityType.DATA_PRODUCT, now=LATER
    )

    assert report.checked == 0
    still_open = await store.list_orphans(source.name, unresolved_only=True)
    assert [record.neutral_id for record in still_open] == [neutral_id]
    assert still_open[0].last_missing_at == NOW  # untouched by this call


async def test_an_orphan_row_with_no_binding_is_skipped_not_crashed(
    store: StateStore, source: FakeConnector
) -> None:
    """Defensive: an ``orphan_log`` row should always have come from a binding, but if
    one is somehow missing, this must not raise -- it is logged and skipped."""
    neutral_id = uuid.uuid4()
    async with store.unit_of_work() as uow:
        await uow.record_orphan(
            neutral_id,
            source.name,
            EntityType.DATA_PRODUCT,
            native_key="sales.ghost",
            last_seen_at=None,
            observed_at=NOW,
        )

    report = await recheck_open_orphans(
        store, source, entity_type=EntityType.DATA_PRODUCT, now=LATER
    )

    assert report.checked == 0
