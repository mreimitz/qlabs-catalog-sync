"""Property 2 -- a crash between the target write landing and the cycle's commit is
safe on retry.

``sync/loop.py``'s own module docstring names this the sharp case: the loop does every
write first and commits bindings, envelopes and the watermark together in **one**
``unit_of_work``, so a crash after a write reached the target but before that commit
leaves the target changed and the engine's own memory unchanged. Replay has to be safe
by construction. Here, "safe" is checked three ways: (1) the target's own store really
holds the new value before the retry (the crash was not a no-op in disguise), (2) the
engine's state store is byte-identical to before the crashed cycle, and (3) the retry
converges without a second write landing or (in the covered case) a duplicate object
appearing.

What ``FakeConnector.update`` actually does on replay -- and why this proves the
property rather than merely asserting it
------------------------------------------------------------------------------------

The real Qlik connector pre-reads before it PATCHes specifically so a replay is a
no-op. ``FakeConnector`` is the target under test here, and it does not pre-read; it
gets to the same place a different way. The retry's diff is computed against the
*stored* target envelope (stale -- the crash prevented it from ever being updated), so
the retry is not empty and a real ``update`` call goes out again, carrying the stale
``expected_revision``. Because the crashed-but-landed write already bumped the target's
revision, ``FakeConnector`` -- built with ``ConcurrencyMode.ETAG`` -- answers with a
:class:`~qlabs_catalog_sync_sdk.exceptions.ConflictError`, which
``SyncLoop._apply_update`` reacts to exactly as RS-07 section 2 step 6 says: re-read the
target's *current* state and re-diff against it before ever retrying the write. That
re-diff finds the source and the freshly-read target already agree, so there is nothing
left to send -- ``_apply_update`` returns ``WriteResult.no_op(...)`` directly, without
issuing a second ``update`` call at all. In other words: the crash-recovery story here
is not a bespoke code path -- it rides the same optimistic-concurrency retry the loop
already has for a genuinely concurrent edit, and that retry is smart enough not to
resend a write it can already tell would be a no-op. That is worth knowing when reading
the assertions below: the retry cycle's call log shows one ``update`` call (which
fails with the conflict) and one ``read`` call (the re-read that proves convergence),
not a second ``update``.
"""

from __future__ import annotations

from collections.abc import Callable

from restart_helpers import (
    CrashAfterWrite,
    FailFromNth,
    seed_product,
    snapshot_state,
    write_calls,
)

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import RecordOutcome, RunStatus, SyncLoop
from qlabs_catalog_sync_sdk.exceptions import TransientError
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def test_an_update_whose_write_landed_before_the_crash_converges_as_a_no_op(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    crashing = CrashAfterWrite()
    ref = seed_product(source, "sales.orders", description="Order facts")
    loop = make_loop(target=crashing, create_missing=True)
    settled = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert settled.status is RunStatus.OK

    baseline_snapshot = snapshot_state(store)
    baseline_watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert baseline_watermark is not None

    source.simulate_external_edit(ref, {"description": {"text": "v2", "format": "plain"}})
    crashing.crash_on.add("update")
    crashing.reset_call_log()

    crashed = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert crashed.status is RunStatus.FAILED
    assert crashed.committed is False
    assert crashed.records == ()  # the failing record never got as far as a RecordReport

    # The write really did land: the target's own store now holds "v2"...
    update_calls = crashing.calls("update")
    assert len(update_calls) == 1
    assert update_calls[0].result is not None
    crashed_ref = update_calls[0].args["ref"]
    live = await crashing.read(crashed_ref)
    assert live.description is not None
    assert live.description.text == "v2"

    # ...but the engine's own memory is exactly where the settled cycle left it.
    assert snapshot_state(store) == baseline_snapshot
    after_watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert after_watermark is not None
    assert after_watermark.watermark_token == baseline_watermark.watermark_token

    # Retry: a fresh cycle re-diffs against the stale stored envelope and resends the
    # write. See this module's docstring for exactly which path makes this converge.
    crashing.reset_call_log()
    retry = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert retry.status is RunStatus.OK
    assert retry.committed is True
    assert retry.records[0].outcome is RecordOutcome.NO_OP
    # Not zero calls -- this is what a replayed cycle looks like, not what an untouched
    # one looks like: one update attempt (rejected as a conflict against the stale
    # stored revision) and one re-read that proves the two sides already agree, with no
    # second update needed. See the module docstring for the exact mechanics.
    assert crashing.call_count("update") == 1
    assert crashing.call_count("read") == 1

    # And now the two sides have genuinely converged: a further cycle writes nothing.
    crashing.reset_call_log()
    fully_settled = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert write_calls(crashing) == []
    assert fully_settled.write_count == 0


async def test_a_created_objects_binding_survives_a_later_records_crash_in_the_same_cycle(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """The one deliberate exception ``sync/loop.py`` documents: a successful ``create``
    anchors its two identity bindings the moment it happens, not at the end of the
    cycle, because a rolled-back create binding would strand the object and duplicate
    it on the retry. Proved here by having a *second* record's write crash later in the
    same cycle: the create that already succeeded keeps its binding even though the
    cycle as a whole fails and commits nothing else.
    """
    seed_product(source, "sales.orders", description="Order facts")
    seed_product(source, "sales.returns", description="Return facts")
    crashing = FailFromNth(
        error=TransientError("qlik is having a moment", endpoint="fake-target"), method="create"
    )
    crashing.fail_from = 2  # the first create lands for real; the second never does
    loop = make_loop(target=crashing, create_missing=True)

    failed = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert failed.status is RunStatus.FAILED
    assert failed.committed is False
    created = [call for call in crashing.calls("create") if call.result is not None]
    assert len(created) == 1
    neutral_id = failed.records[0].neutral_id
    assert neutral_id is not None

    # The two bindings for the object that really was created survive the rollback...
    source_binding = await store.get_binding(neutral_id, source.name, EntityType.DATA_PRODUCT)
    target_binding = await store.get_binding(neutral_id, crashing.name, EntityType.DATA_PRODUCT)
    assert source_binding is not None
    assert target_binding is not None
    assert target_binding.identity.native_key == created[0].result.ref.native_key

    # ...while everything the failed cycle could still take back is gone: no envelope,
    # no watermark. The identity map is the one deliberate exception, not a leak.
    assert await store.fetch_envelopes(neutral_id, crashing.name) == {}
    assert await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT) is None

    crashing.fail_from = None
    crashing.reset_call_log()
    retry = await loop.run_cycle(EntityType.DATA_PRODUCT)

    outcomes = {record.native_key: record.outcome for record in retry.records}
    assert outcomes == {
        "sales.orders": RecordOutcome.NO_OP,  # found by its surviving binding
        "sales.returns": RecordOutcome.CREATED,
    }
    assert crashing.call_count("create") == 1  # no duplicate of sales.orders
    assert retry.status is RunStatus.OK


async def test_a_crash_in_the_gap_between_a_landed_create_and_its_anchor_can_duplicate(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """The one gap the immediate-anchor design does not, and cannot, close.

    ``_anchor_created_object`` commits the moment a ``create`` succeeds precisely so a
    *later* failure in the same cycle cannot strand it (the test above). But nothing can
    make one HTTP call and one DB commit atomic with each other: if the process is
    killed in the literal instant between "the target returned success" and "the anchor
    call begins" -- simulated here with a *single* record, not a second one that fails
    later -- no binding is ever written for the new object, and a retry has no way to
    recognize it, so it creates a *second* one. This is not a defect introduced by a
    coding mistake in this build; it is the inherent dual-write problem (one network
    call, one separate database commit, no distributed transaction spanning both) that
    no amount of test-only work can close, and closing it for real would need an
    idempotency key the Qlik create API would have to support. Recorded here as a
    documented, known limitation, not asserted as if it were safe.
    """
    seed_product(source, "sales.orders", description="Order facts")
    crashing = CrashAfterWrite()
    crashing.crash_on.add("create")
    loop = make_loop(target=crashing, create_missing=True)

    crashed = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert crashed.status is RunStatus.FAILED
    assert crashed.committed is False
    created = crashing.calls("create")
    assert len(created) == 1
    assert created[0].result is not None  # the create genuinely landed at the target

    # Nothing was committed -- consistent with every other crash-mid-cycle case.
    assert await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT) is None

    crashing.reset_call_log()
    retry = await loop.run_cycle(EntityType.DATA_PRODUCT)

    # No binding survived the crash (unlike the later-record-fails case above), so the
    # retry cannot recognize the object the crashed cycle already created -- it creates
    # a second one. This is the documented gap, demonstrated rather than asserted safe.
    assert retry.records[0].outcome is RecordOutcome.CREATED
    assert crashing.call_count("create") == 1
