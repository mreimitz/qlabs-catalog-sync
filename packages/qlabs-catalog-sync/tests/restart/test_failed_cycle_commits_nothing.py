"""Property 4 -- a failed cycle commits nothing, at every point a failure can occur.

Three failure points, each proved against a *non-trivial* baseline (two synced records
already sitting in the state store, not an empty one -- an empty-store comparison would
pass even if ``unit_of_work``'s transaction boundary were deleted entirely, since there
would be nothing for a bug to leave behind):

1. a failure while reading the source (:func:`test_a_read_failure_commits_nothing`);
2. a failure while writing the target
   (:func:`test_a_write_failure_commits_nothing`);
3. a failure inside the state store's own commit -- no connector involved at all
   (:func:`test_a_failure_inside_the_state_stores_own_commit_persists_nothing`).

In every case: the run report says ``FAILED``/``committed=False``, every state-store
row is byte-identical to the pre-failure baseline, and the watermark's resume position
did not move.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from restart_helpers import make_commit_crash, seed_product, snapshot_state, write_calls

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import RecordOutcome, RunStatus, SyncLoop
from qlabs_catalog_sync_sdk.exceptions import TransientError
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def test_a_read_failure_commits_nothing(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    first_ref = seed_product(source, "sales.orders", description="Order facts")
    second_ref = seed_product(source, "sales.returns", description="Return facts")
    settled = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)
    assert settled.status is RunStatus.OK

    baseline_snapshot = snapshot_state(store)
    baseline_watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert baseline_watermark is not None

    source.simulate_external_edit(first_ref, {"description": {"text": "v2", "format": "plain"}})
    source.simulate_external_edit(second_ref, {"description": {"text": "v2", "format": "plain"}})
    source.fail_next(
        "read", TransientError("databricks is having a moment", endpoint="fake-source")
    )
    target.reset_call_log()

    # retry_attempts=0: one failure is enough to abort, so this is a clean single-point
    # injection rather than a story about backoff/retry (already covered elsewhere).
    failed = await make_loop(create_missing=True, retry_attempts=0).run_cycle(
        EntityType.DATA_PRODUCT
    )

    assert failed.status is RunStatus.FAILED
    assert failed.committed is False
    assert [error.kind for error in failed.errors] == ["TransientError"]
    assert write_calls(target) == []  # the cycle never got as far as a write at all

    assert snapshot_state(store) == baseline_snapshot
    after_watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert after_watermark is not None
    assert after_watermark.watermark_token == baseline_watermark.watermark_token


async def test_a_write_failure_commits_nothing(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    ref = seed_product(source, "sales.orders", description="Order facts")
    settled = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)
    assert settled.status is RunStatus.OK
    neutral_id = settled.records[0].neutral_id
    assert neutral_id is not None

    baseline_snapshot = snapshot_state(store)
    baseline_watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert baseline_watermark is not None

    source.simulate_external_edit(ref, {"description": {"text": "v2", "format": "plain"}})
    target.fail_next("update", TransientError("qlik is having a moment", endpoint="fake-target"))

    failed = await make_loop(create_missing=True, retry_attempts=0).run_cycle(
        EntityType.DATA_PRODUCT
    )

    assert failed.status is RunStatus.FAILED
    assert failed.committed is False
    assert [error.kind for error in failed.errors] == ["TransientError"]

    # fail_next raises before any state is touched (FakeConnector's own guarantee), so
    # the target's real store was never mutated -- check it directly, not just the log.
    target_binding = await store.get_binding(neutral_id, target.name, EntityType.DATA_PRODUCT)
    assert target_binding is not None
    live = await target.read(target_binding.identity)
    assert live.description is not None
    assert live.description.text == "Order facts"  # not "v2" -- the write never landed

    assert snapshot_state(store) == baseline_snapshot
    after_watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert after_watermark is not None
    assert after_watermark.watermark_token == baseline_watermark.watermark_token


async def test_a_failure_inside_the_state_stores_own_commit_persists_nothing(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No connector fails here at all: the write reaches the target cleanly, and only
    the state store's own transaction commit is made to raise -- the most direct proof
    available that ``unit_of_work``'s rollback, not any connector-level guard, is what
    makes "a failed cycle commits nothing" true.
    """
    ref = seed_product(source, "sales.orders", description="Order facts")
    settled = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)
    assert settled.status is RunStatus.OK

    baseline_snapshot = snapshot_state(store)
    baseline_watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert baseline_watermark is not None

    source.simulate_external_edit(ref, {"description": {"text": "v2", "format": "plain"}})
    target.reset_call_log()
    make_commit_crash(store, monkeypatch, error=RuntimeError("disk died exactly at commit"))

    loop = make_loop(create_missing=True)
    failed = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert failed.status is RunStatus.FAILED
    assert failed.committed is False
    assert [error.kind for error in failed.errors] == ["EngineError"]  # not a connector error

    # The write DID reach the target cleanly -- this failure has nothing to do with it.
    assert target.call_count("update") == 1
    assert target.calls("update")[0].result is not None

    # And yet, because the state store's own commit never completed, nothing is
    # persisted: the envelope table still shows the old value, the watermark has not
    # moved. If ``unit_of_work``'s ``except`` clause did not roll the session back (or
    # if the envelope/binding writes and the watermark advance were committed as two
    # separate transactions instead of one), this specific assertion is what would
    # catch it -- both halves of the write are staged in the very same session whose
    # commit this test makes fail.
    assert snapshot_state(store) == baseline_snapshot
    after_watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert after_watermark is not None
    assert after_watermark.watermark_token == baseline_watermark.watermark_token

    # The injected crash is single-shot and has already fired, so no explicit undo is
    # needed: retry, and the write already landed, so this converges exactly like the
    # other partially-applied-write cases in this package.
    target.reset_call_log()
    retry = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert retry.status is RunStatus.OK
    assert retry.records[0].outcome is RecordOutcome.NO_OP
