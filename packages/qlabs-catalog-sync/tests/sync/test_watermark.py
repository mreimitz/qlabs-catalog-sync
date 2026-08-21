"""Watermark safety: only what the source proposed, only on success, never past open work."""

from __future__ import annotations

from collections.abc import Callable

from sync_helpers import bind, cursor_position, data_product, seed_product

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import RecordOutcome, RunStatus, SkipReason, SyncLoop
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def test_the_watermark_is_whatever_the_source_proposed_and_nothing_else(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """The committed token is byte-for-byte the connector's own ``next_watermark``."""
    seed_product(source, "sales.orders")
    seed_product(source, "sales.returns")

    await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    proposed = source.calls("list_changed")[-1].result.next_watermark
    row = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert row is not None
    assert row.watermark_token == proposed.model_dump_json()


async def test_a_paged_listing_processes_every_page_and_commits_the_last(
    make_loop: Callable[..., SyncLoop],
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """``has_more`` is followed within the cycle; the committed position is the final page."""
    paged = FakeConnector.read_only_source(name="fake-source", list_changed_page_size=1)
    for key in ("sales.orders", "sales.returns", "sales.refunds"):
        seed_product(paged, key)

    report = await make_loop(source=paged, create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    assert report.pages == 3
    assert report.has_more is False
    assert len(report.records) == 3
    row = await store.get_watermark(pair.name, paged.name, EntityType.DATA_PRODUCT)
    assert row is not None
    assert cursor_position(row.watermark_token) == "3"


async def test_a_bounded_cycle_never_advances_past_the_pages_it_did_not_reach(
    make_loop: Callable[..., SyncLoop],
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """Stopping early commits the last processed page, and says there is more to come."""
    paged = FakeConnector.read_only_source(name="fake-source", list_changed_page_size=1)
    for key in ("sales.orders", "sales.returns", "sales.refunds"):
        seed_product(paged, key)

    report = await make_loop(source=paged, create_missing=True, max_pages=1).run_cycle(
        EntityType.DATA_PRODUCT
    )

    assert report.pages == 1
    assert report.has_more is True
    assert len(report.records) == 1
    row = await store.get_watermark(pair.name, paged.name, EntityType.DATA_PRODUCT)
    assert row is not None
    assert cursor_position(row.watermark_token) == "1"

    # The next cycle resumes exactly where this one stopped.
    paged.reset_call_log()
    second = await make_loop(source=paged, create_missing=True, max_pages=1).run_cycle(
        EntityType.DATA_PRODUCT
    )
    assert [record.native_key for record in second.records] == ["sales.returns"]


async def test_a_record_with_work_outstanding_holds_the_watermark(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """An object that could not be synced is never left behind by an advancing watermark."""
    seed_product(source, "sales.orders")
    loop = make_loop()  # creation off: there is no counterpart and none may be invented

    report = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert report.status is RunStatus.PARTIAL
    assert report.committed is True
    assert report.watermark_advanced is False
    assert report.watermark_held_by == ("sales.orders",)
    row = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert row is not None
    assert cursor_position(row.watermark_token) is None
    assert row.last_status == RunStatus.PARTIAL.value

    # And it is listed again next cycle rather than forgotten.
    source.reset_call_log()
    second = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert [record.native_key for record in second.records] == ["sales.orders"]


async def test_a_later_page_cannot_advance_past_an_earlier_held_one(
    make_loop: Callable[..., SyncLoop],
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """Page two succeeding does not license skipping page one's unfinished work."""
    paged = FakeConnector.read_only_source(name="fake-source", list_changed_page_size=1)
    seed_product(paged, "sales.orders")  # page 1: no counterpart, holds the watermark
    settled = seed_product(paged, "sales.returns", description="Return facts")
    existing = target.seed(  # page 2: bound, and genuinely out of date, so it writes
        data_product("returns", description="stale"), native_key="qlik-returns"
    )
    await bind(store, settled, existing)

    report = await make_loop(source=paged, target=target).run_cycle(EntityType.DATA_PRODUCT)

    assert report.pages == 2
    outcomes = {record.native_key: record.outcome for record in report.records}
    assert outcomes["sales.orders"] is RecordOutcome.SKIPPED
    assert outcomes["sales.returns"] is RecordOutcome.WRITTEN
    assert report.watermark_held_by == ("sales.orders",)
    row = await store.get_watermark(pair.name, paged.name, EntityType.DATA_PRODUCT)
    assert row is not None
    assert cursor_position(row.watermark_token) is None  # still at the very beginning


async def test_an_unreadable_stored_watermark_falls_back_to_a_full_scan(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """A corrupt token is not a reason to refuse to run: a rescan is always safe here."""
    seed_product(source, "sales.orders")
    async with store.unit_of_work() as uow:
        await uow.advance_watermark(
            pair.name,
            source.name,
            EntityType.DATA_PRODUCT,
            "not json at all",
            run_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )

    report = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    assert [record.outcome for record in report.records] == [RecordOutcome.CREATED]
    assert report.status is RunStatus.OK


async def test_an_entity_type_the_pair_does_not_sync_runs_no_cycle_at_all(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """An unscheduled entity type is reported, not raised, and touches nothing."""
    seed_product(source, "sales.orders")

    report = await make_loop().run_cycle(EntityType.DATASET)

    assert report.status is RunStatus.SKIPPED
    assert report.records == ()
    assert report.errors[0].operation == "capabilities"
    assert source.call_count("list_changed") == 0
    assert await store.get_watermark(pair.name, source.name, EntityType.DATASET) is None


async def test_an_entity_type_the_target_does_not_support_runs_no_cycle_at_all(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, pair: SyncPairConfig
) -> None:
    """The Qlik-shaped manifest declares glossary unsupported (D5); it is never scheduled."""
    glossary_pair = pair.model_copy(update={"entity_types": [EntityType.GLOSSARY_TERM]})

    report = await make_loop(pair=glossary_pair).run_cycle(EntityType.GLOSSARY_TERM)

    assert report.status is RunStatus.SKIPPED
    assert "does not support" in report.errors[0].message
    assert source.call_count("list_changed") == 0


async def test_skip_reasons_are_stable_codes(
    make_loop: Callable[..., SyncLoop], source: FakeConnector
) -> None:
    """T2.8 and T8.1 assert on these strings, so they are part of the contract."""
    seed_product(source, "sales.orders")
    seed_product(source, "hr.people")

    report = await make_loop().run_cycle(EntityType.DATA_PRODUCT)

    reasons = {record.native_key: record.reason for record in report.records}
    assert reasons == {
        "sales.orders": SkipReason.NO_TARGET_BINDING,
        "hr.people": SkipReason.NOT_SELECTED,
    }
    assert SkipReason.NO_TARGET_BINDING.value == "no_target_binding"
    assert SkipReason.NOT_SELECTED.value == "not_selected"
