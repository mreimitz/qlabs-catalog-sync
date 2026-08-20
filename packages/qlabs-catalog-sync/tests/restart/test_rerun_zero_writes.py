"""Property 1 -- re-run over unchanged source data is a zero-write no-op.

RS-07 section 2 step 8 and the architecture doc's "an idempotent re-run performs zero
writes" claim, tested the only honest way: against the *target connector's* recorded
call log (never the run report alone -- "the report says zero" is not proof of
anything a real Qlik tenant would notice) and against the raw state-store rows (never
just one known field, per the T8.4 brief's "make the state assertions strong").
"""

from __future__ import annotations

from collections.abc import Callable

from restart_helpers import seed_product, snapshot_state, write_calls

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import RecordOutcome, RunStatus, SyncLoop
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def test_rerun_over_unchanged_data_issues_zero_target_calls_and_zero_state_churn(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """The whole claim, on the one log that cannot lie about it."""
    seed_product(source, "sales.orders", description="Order facts", tags=[("tier", "gold")])
    seed_product(source, "sales.returns", description="Return facts")
    loop = make_loop(create_missing=True)

    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert first.status is RunStatus.OK
    assert {record.outcome for record in first.records} == {RecordOutcome.CREATED}

    baseline_snapshot = snapshot_state(store)
    baseline_watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert baseline_watermark is not None
    target.reset_call_log()
    source.reset_call_log()

    second = await loop.run_cycle(EntityType.DATA_PRODUCT)

    # Zero writes -- not a no-op write, no write at all. The source's own changelog has
    # nothing past the watermark (both objects' only event was their creation, already
    # consumed), so there is nothing to relist and nothing to read either.
    assert write_calls(target) == []
    assert second.status is RunStatus.OK
    assert second.write_count == 0
    assert second.records == ()
    assert source.call_count("list_changed") == 1
    assert source.call_count("read") == 0

    # No state churn: every stored identity/envelope/orphan row is byte-identical to
    # what the first cycle left behind, and the watermark's actual resume position (not
    # the whole row -- last_run_at legitimately advances on every committed cycle, a
    # no-op one included) is unchanged.
    assert snapshot_state(store) == baseline_snapshot
    after_watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert after_watermark is not None
    assert after_watermark.watermark_token == baseline_watermark.watermark_token


async def test_a_relisted_but_unchanged_object_short_circuits_before_the_write_path(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """The other half of "zero writes": even when the object *is* relisted (the
    watermark rewound, so the source really does report it again), an unchanged
    checksum still never reaches the write path. Proves the short-circuit is the
    checksum, not the watermark quietly hiding the record from a second listing.
    """
    seed_product(source, "sales.orders", description="Order facts")
    loop = make_loop(create_missing=True)
    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    neutral_id = first.records[0].neutral_id
    assert neutral_id is not None

    async with store.unit_of_work() as uow:
        await uow.advance_watermark(
            loop.pair.name,
            source.name,
            EntityType.DATA_PRODUCT,
            None,
            run_at=first.started_at,
        )
    baseline_snapshot = snapshot_state(store)
    target.reset_call_log()

    second = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert [record.outcome for record in second.records] == [RecordOutcome.UNCHANGED]
    assert second.records[0].changed_fields == ()
    assert write_calls(target) == []
    assert source.call_count("read") == 2  # it really was read again
    assert snapshot_state(store) == baseline_snapshot


async def test_a_reordered_order_insensitive_array_is_not_a_change(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """The phantom diff that would rewrite every product every cycle stays killed.

    Belongs in this suite, not just T2.4's own tests, because it is exactly the kind of
    false idempotency break that would silently turn "zero-write re-run" into "the
    engine rewrites everything every cycle, forever" without ever failing a naive test
    that only checks the report's counts.
    """
    ref = seed_product(source, "sales.orders", tags=[("tier", "gold"), ("owner", "sales")])
    loop = make_loop(create_missing=True)
    await loop.run_cycle(EntityType.DATA_PRODUCT)

    source.simulate_external_edit(
        ref,
        {"tags": [{"key": "owner", "value": "sales"}, {"key": "tier", "value": "gold"}]},
    )
    baseline_snapshot = snapshot_state(store)
    target.reset_call_log()

    report = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert [record.outcome for record in report.records] == [RecordOutcome.UNCHANGED]
    assert write_calls(target) == []
    assert snapshot_state(store) == baseline_snapshot


async def test_a_genuine_change_is_written_once_and_then_settles_back_to_zero(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """Idempotency is not inertia: a real change is written, and only that one write."""
    ref = seed_product(source, "sales.orders", description="Order facts")
    loop = make_loop(create_missing=True)
    await loop.run_cycle(EntityType.DATA_PRODUCT)

    source.simulate_external_edit(
        ref, {"description": {"text": "Now with returns", "format": "plain"}}
    )
    target.reset_call_log()
    second = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert write_calls(target) == ["update"]
    assert second.records[0].outcome is RecordOutcome.WRITTEN
    assert second.records[0].changed_fields == ("description",)

    baseline_snapshot = snapshot_state(store)
    target.reset_call_log()
    third = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert write_calls(target) == []
    assert third.write_count == 0
    assert snapshot_state(store) == baseline_snapshot
