"""One transaction: a cycle that fails part way commits nothing, and the retry converges.

The failure is injected for real -- the target's write path raises after the first write of
the cycle has genuinely landed -- so these tests exercise the interesting case, not the
trivial one where nothing had happened yet.
"""

from __future__ import annotations

from collections.abc import Callable

from sync_helpers import FailingTarget, cursor_position, seed_product

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import RecordOutcome, RunStatus, SyncLoop
from qlabs_catalog_sync_sdk.exceptions import TransientError
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector


def failing_target(method: str = "update") -> FailingTarget:
    """A Qlik-shaped target that can be told to start failing one write method."""
    return FailingTarget(
        error=TransientError("qlik is having a moment", endpoint="fake-target"),
        method=method,
    )


async def test_a_mid_cycle_failure_commits_nothing_and_leaves_the_watermark(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """The definition of done: state and watermark are exactly where the failed cycle found them."""
    first_ref = seed_product(source, "sales.orders", description="Order facts")
    second_ref = seed_product(source, "sales.returns", description="Return facts")
    target = failing_target()
    loop = make_loop(target=target, create_missing=True)

    settled = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert settled.status is RunStatus.OK
    baseline_watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert baseline_watermark is not None
    orders_id = settled.records[0].neutral_id
    returns_id = settled.records[1].neutral_id
    assert orders_id is not None and returns_id is not None
    baseline_envelopes = await store.fetch_envelopes(orders_id, target.name)

    # Both objects change at the source; the target starts failing from its second write.
    source.simulate_external_edit(first_ref, {"description": {"text": "v2", "format": "plain"}})
    source.simulate_external_edit(second_ref, {"description": {"text": "v2", "format": "plain"}})
    target.fail_from = 1  # the next write, and every one after it, fails
    target.reset_call_log()

    failed = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert failed.status is RunStatus.FAILED
    assert failed.committed is False
    assert failed.watermark_advanced is False
    assert [error.kind for error in failed.errors] == ["TransientError"]
    assert failed.errors[0].fatal is True

    # Nothing at all was persisted -- not the envelopes, not the watermark.
    assert await store.fetch_envelopes(orders_id, target.name) == baseline_envelopes
    after = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert after is not None
    assert after.watermark_token == baseline_watermark.watermark_token


async def test_a_partially_applied_cycle_converges_on_the_retry(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """A write that reached the target before the failure is reconciled, not repeated.

    The database rollback cannot undo the write that already landed. What makes that safe is
    that the next cycle re-diffs against the stale stored envelope, re-sends the same write,
    and the target answers ``NO_OP`` -- which is exactly why that outcome is structurally
    distinct from a real write.
    """
    first_ref = seed_product(source, "sales.orders", description="Order facts")
    second_ref = seed_product(source, "sales.returns", description="Return facts")
    target = failing_target()
    loop = make_loop(target=target, create_missing=True)

    await loop.run_cycle(EntityType.DATA_PRODUCT)
    source.simulate_external_edit(first_ref, {"description": {"text": "v2", "format": "plain"}})
    source.simulate_external_edit(second_ref, {"description": {"text": "v2", "format": "plain"}})

    target.fail_from = 2  # the first update of the cycle lands, the second fails forever
    target.reset_call_log()
    failed = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert failed.status is RunStatus.FAILED
    assert failed.committed is False
    applied = [call for call in target.calls("update") if call.result is not None]
    assert len(applied) == 1  # one write really did reach the target

    target.fail_from = None
    target.reset_call_log()
    retry = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert retry.status is RunStatus.OK
    assert retry.committed is True
    outcomes = {record.native_key: record.outcome for record in retry.records}
    # The one that already landed is a no-op; the one that never landed is written.
    assert outcomes == {
        "sales.orders": RecordOutcome.NO_OP,
        "sales.returns": RecordOutcome.WRITTEN,
    }

    # And a third cycle now writes nothing at all: the two sides have converged.
    target.reset_call_log()
    third = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert target.call_count("update") == 0
    assert third.write_count == 0


async def test_a_created_object_is_never_created_twice_after_a_failed_cycle(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """A create cannot be rolled back, so its binding is committed the moment it is true.

    Everything else the failed cycle planned is still discarded: no envelopes, no watermark.
    """
    seed_product(source, "sales.orders", description="Order facts")
    seed_product(source, "sales.returns", description="Return facts")
    target = failing_target("create")
    target.fail_from = 2
    loop = make_loop(target=target, create_missing=True)

    failed = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert failed.status is RunStatus.FAILED
    assert failed.committed is False
    created = [call for call in target.calls("create") if call.result is not None]
    assert len(created) == 1
    neutral_id = failed.records[0].neutral_id
    assert neutral_id is not None

    # The two bindings for the object that really was created survive the rollback...
    source_binding = await store.get_binding(neutral_id, source.name, EntityType.DATA_PRODUCT)
    target_binding = await store.get_binding(neutral_id, target.name, EntityType.DATA_PRODUCT)
    assert source_binding is not None
    assert target_binding is not None
    assert target_binding.identity.native_key == created[0].result.ref.native_key

    # ...while everything the cycle could still take back is gone.
    assert await store.fetch_envelopes(neutral_id, target.name) == {}
    assert await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT) is None

    target.fail_from = None
    target.reset_call_log()
    retry = await loop.run_cycle(EntityType.DATA_PRODUCT)

    outcomes = {record.native_key: record.outcome for record in retry.records}
    assert outcomes == {
        "sales.orders": RecordOutcome.NO_OP,  # found by its binding, already matching
        "sales.returns": RecordOutcome.CREATED,
    }
    assert target.call_count("create") == 1  # no duplicate of sales.orders
    assert retry.status is RunStatus.OK


async def test_a_transient_failure_that_recovers_within_the_cycle_commits_normally(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """A retryable error is retried, and the cycle that follows it is an ordinary one."""
    seed_product(source, "sales.orders")
    target.fail_next("create", TransientError("throttled", endpoint="fake-target"))

    report = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    assert report.status is RunStatus.OK
    assert report.committed is True
    assert report.records[0].outcome is RecordOutcome.CREATED
    assert target.call_count("create") == 2  # the failed attempt plus the successful retry
    watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert watermark is not None
    assert cursor_position(watermark.watermark_token) == "1"
