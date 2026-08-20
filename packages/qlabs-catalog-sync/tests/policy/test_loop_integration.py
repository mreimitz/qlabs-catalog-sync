"""End-to-end :class:`SyncLoop` behavior with :class:`ManualEditWritePolicy` wired in as
``write_policy`` -- the actual T2.5 wiring an engine process uses, not a stand-in for it
(``conftest.make_loop`` defaults every loop it builds to exactly this policy).

Complements ``test_policy_unit.py``: those tests pin down what one call to ``withhold``
decides, these prove the decision survives being embedded in a real, committed,
re-runnable cycle -- including what a withheld field looks like two and three cycles
later, which only a real state store can answer honestly.

``description`` is a :class:`~qlabs_catalog_sync_sdk.models.TextField`, not a bare
string, so every out-of-band edit below goes through :func:`TextField.plain` -- both
connectors re-``read`` their own store on the loop's next cycle, and a bare string would
fail that re-materialization exactly as it does on a real, badly-typed hand edit.
"""

from __future__ import annotations

from collections.abc import Callable

from policy_helpers import bind, data_product, seed_product

from qlabs_catalog_sync.config import ManualEditMode, ManualEditPolicy, SyncPairConfig
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import SyncLoop
from qlabs_catalog_sync.sync.policy import MANUAL_EDIT_WITHHELD_REASON
from qlabs_catalog_sync_sdk.models import EntityType, TextField
from qlabs_catalog_sync_sdk.testing import FakeConnector


def _preserve_local(pair: SyncPairConfig) -> SyncPairConfig:
    return pair.model_copy(
        update={"manual_edit_policy": ManualEditPolicy(default=ManualEditMode.PRESERVE_LOCAL)}
    )


async def test_source_wins_overwrites_a_hand_edited_field_by_default(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """No configuration at all -> v1's default -> the next sync overwrites the edit."""
    source_ref = seed_product(source, "sales.orders", description="Order facts, v2")
    existing = target.seed(
        data_product("orders", description="Order facts, v1"), native_key="qlik-orders"
    )
    await bind(store, source_ref, existing)
    target.simulate_external_edit(existing, {"description": TextField.plain("someone's hand edit")})

    report = await make_loop().run_cycle(EntityType.DATA_PRODUCT)

    record = report.records[0]
    assert record.withheld == ()
    assert "description" in record.written_fields
    stored_entity = await target.read(existing)
    assert stored_entity.model_dump()["description"]["text"] == "Order facts, v2"
    # source_wins never needed evidence of the edit, so it never re-read for it; the one
    # read below is this assertion's own.
    assert target.call_count("read") == 1


async def test_preserve_local_withholds_the_field_and_reports_why(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    source_ref = seed_product(source, "sales.orders", description="Order facts, v1")
    existing = target.seed(
        data_product("orders", description="Order facts, v1"), native_key="qlik-orders"
    )
    await bind(store, source_ref, existing)
    loop = make_loop(pair=_preserve_local(pair))

    # Cycle 1 establishes the engine's own recorded baseline; matching what is already
    # live is not itself a manual edit.
    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert first.records[0].withheld == ()

    # A human edits Qlik directly; the source then changes independently of that edit.
    target.simulate_external_edit(existing, {"description": TextField.plain("a human's hand edit")})
    source.simulate_external_edit(source_ref, {"description": TextField.plain("Order facts, v2")})

    second = await loop.run_cycle(EntityType.DATA_PRODUCT)
    record = second.records[0]

    assert {item.field: item.reason for item in record.withheld} == {
        "description": MANUAL_EDIT_WITHHELD_REASON
    }
    assert [
        (native_key, item.field, item.reason) for native_key, item in second.withheld_fields
    ] == [("sales.orders", "description", MANUAL_EDIT_WITHHELD_REASON)]
    assert "description" not in record.written_fields
    stored_entity = await target.read(existing)
    assert stored_entity.model_dump()["description"]["text"] == "a human's hand edit"


async def test_a_field_the_target_never_had_is_not_a_manual_edit_on_a_first_sync(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """A pre-existing (bootstrap-matched) Qlik object the engine has never synced before
    has no recorded baseline for any field, even under preserve_local -- so the first
    sync writes through normally instead of treating every difference as an edit."""
    source_ref = seed_product(source, "sales.orders", description="Order facts")
    existing = target.seed(
        data_product("stale name", description="stale"), native_key="qlik-orders"
    )
    await bind(store, source_ref, existing)

    report = await make_loop(pair=_preserve_local(pair)).run_cycle(EntityType.DATA_PRODUCT)

    record = report.records[0]
    assert record.withheld == ()
    assert {"name", "description"} <= set(record.written_fields)
    stored_entity = await target.read(existing)
    dumped = stored_entity.model_dump()
    assert dumped["name"] == "orders"
    assert dumped["description"]["text"] == "Order facts"


async def test_withheld_field_is_excluded_from_the_write_and_never_persisted(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """A withheld field must reach neither the target's write nor the state store --
    persisting a value the target never received would be a lie the next cycle believes."""
    source_ref = seed_product(source, "sales.orders", description="v1")
    existing = target.seed(data_product("orders", description="v1"), native_key="qlik-orders")
    await bind(store, source_ref, existing)
    loop = make_loop(pair=_preserve_local(pair))
    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    neutral_id = first.records[0].neutral_id
    assert neutral_id is not None
    # Cycle 1 converges (the seeded target already matched the seeded source), so this is
    # the baseline every later cycle diffs against -- not itself a manual edit.
    baseline = (await store.fetch_envelopes(neutral_id, target.name))["description"]
    updates_before = target.call_count("update")

    target.simulate_external_edit(existing, {"description": TextField.plain("hand edit")})
    source.simulate_external_edit(source_ref, {"description": TextField.plain("v2")})
    second = await loop.run_cycle(EntityType.DATA_PRODUCT)

    # description was the only field that differed, and it was fully withheld -- so
    # there was nothing left to send, and no update call happened for this cycle at all.
    assert target.call_count("update") == updates_before
    assert second.records[0].written_fields == ()
    # The state store's belief about the target's description is exactly what it was
    # before -- never advanced to "v2" -- so the next diff still sees the same conflict
    # instead of quietly believing "v2" landed.
    after = (await store.fetch_envelopes(neutral_id, target.name))["description"]
    assert after.checksum == baseline.checksum


async def test_preserve_local_keeps_protecting_the_edit_across_further_source_changes(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """The open design question the task calls out, answered and pinned down here: under
    preserve_local, a further source-side change does not resurrect source-wins for an
    already-preserved field. The human's edit is preserved indefinitely -- not merely
    for the one cycle it was first detected on -- and the decision is stable rather than
    thrashing: the same withheld verdict recurs every cycle instead of flapping between
    writing and not writing.
    """
    source_ref = seed_product(source, "sales.orders", description="v1")
    existing = target.seed(data_product("orders", description="v1"), native_key="qlik-orders")
    await bind(store, source_ref, existing)
    loop = make_loop(pair=_preserve_local(pair))
    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    neutral_id = first.records[0].neutral_id
    assert neutral_id is not None
    baseline = (await store.fetch_envelopes(neutral_id, target.name))["description"]

    target.simulate_external_edit(existing, {"description": TextField.plain("a human's hand edit")})

    for source_value in ("v2", "v3", "v4"):
        source.simulate_external_edit(source_ref, {"description": TextField.plain(source_value)})
        report = await loop.run_cycle(EntityType.DATA_PRODUCT)
        record = report.records[0]

        assert {item.field: item.reason for item in record.withheld} == {
            "description": MANUAL_EDIT_WITHHELD_REASON
        }
        assert "description" not in record.written_fields
        stored_entity = await target.read(existing)
        assert stored_entity.model_dump()["description"]["text"] == "a human's hand edit"
        # Never persisted, so next cycle's baseline is unchanged -> the same comparison,
        # the same verdict, every time. Not a thrash.
        after = (await store.fetch_envelopes(neutral_id, target.name))["description"]
        assert after.checksum == baseline.checksum
