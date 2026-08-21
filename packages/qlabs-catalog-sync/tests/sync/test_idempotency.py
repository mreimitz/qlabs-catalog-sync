"""Idempotency: a re-run over unchanged source data performs zero writes.

Every assertion here is against the target connector's recorded call log and against the
state store, never against a return value alone -- "the loop said it skipped" is not
evidence that it did not write.
"""

from __future__ import annotations

from collections.abc import Callable

from sync_helpers import seed_product, write_calls

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import RecordOutcome, RunStatus, SyncLoop
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def test_a_rerun_over_unchanged_data_performs_zero_writes(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """The whole claim, proved on the call log: nothing at all is sent the second time."""
    seed_product(source, "sales.orders", description="Order facts", tags=[("tier", "gold")])
    loop = make_loop(create_missing=True)

    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert first.records[0].outcome is RecordOutcome.CREATED
    neutral_id = first.records[0].neutral_id
    assert neutral_id is not None

    envelopes_after_first = await store.fetch_envelopes(neutral_id, target.name)
    watermark_after_first = await store.get_watermark(
        pair.name, source.name, EntityType.DATA_PRODUCT
    )
    target.reset_call_log()

    second = await loop.run_cycle(EntityType.DATA_PRODUCT)

    # Zero writes -- not a no-op write, no write at all.
    assert write_calls(target) == []
    assert second.write_count == 0
    assert second.status is RunStatus.OK

    # And no state churn: the same envelopes, the same watermark position.
    assert await store.fetch_envelopes(neutral_id, target.name) == envelopes_after_first
    watermark_after_second = await store.get_watermark(
        pair.name, source.name, EntityType.DATA_PRODUCT
    )
    assert watermark_after_second is not None
    assert watermark_after_first is not None
    assert watermark_after_second.watermark_token == watermark_after_first.watermark_token


async def test_an_unchanged_checksum_short_circuits_before_the_write_path(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """A re-listed but unchanged object is diffed to nothing and never reaches ``update``.

    The object is deliberately re-listed (by rewinding the watermark) so the loop really
    does read it again -- proving the short-circuit is the *checksum*, not the watermark
    quietly hiding the record.
    """
    seed_product(source, "sales.orders", description="Order facts")
    loop = make_loop(create_missing=True)
    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    neutral_id = first.records[0].neutral_id
    assert neutral_id is not None

    # Rewind the watermark so the same object is listed again from scratch.
    async with store.unit_of_work() as uow:
        await uow.advance_watermark(
            loop.pair.name,
            source.name,
            EntityType.DATA_PRODUCT,
            None,
            run_at=first.started_at,
        )
    target.reset_call_log()

    second = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert [record.outcome for record in second.records] == [RecordOutcome.UNCHANGED]
    assert second.records[0].changed_fields == ()
    assert target.call_count("update") == 0
    assert target.call_count("create") == 0
    assert source.call_count("read") == 2  # it really was read again


async def test_a_real_source_change_is_written_and_only_the_changed_field_is_sent(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """Idempotency is not inertia: a genuine change still produces exactly one minimal write."""
    ref = seed_product(source, "sales.orders", description="Order facts")
    loop = make_loop(create_missing=True)
    await loop.run_cycle(EntityType.DATA_PRODUCT)

    source.simulate_external_edit(
        ref, {"description": {"text": "Now with returns", "format": "plain"}}
    )
    target.reset_call_log()

    report = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert [record.outcome for record in report.records] == [RecordOutcome.WRITTEN]
    assert report.records[0].changed_fields == ("description",)
    assert target.call_count("update") == 1
    assert target.calls("update")[0].args["diff"].field_names == ["description"]

    # A third cycle over the now-settled state writes nothing again.
    target.reset_call_log()
    third = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert write_calls(target) == []
    assert third.write_count == 0


async def test_a_reordered_order_insensitive_array_is_not_a_change(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, target: FakeConnector
) -> None:
    """The phantom diff that would rewrite every product every cycle stays killed."""
    ref = seed_product(source, "sales.orders", tags=[("tier", "gold"), ("owner", "sales")])
    loop = make_loop(create_missing=True)
    await loop.run_cycle(EntityType.DATA_PRODUCT)

    source.simulate_external_edit(
        ref,
        {"tags": [{"key": "owner", "value": "sales"}, {"key": "tier", "value": "gold"}]},
    )
    target.reset_call_log()

    report = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert [record.outcome for record in report.records] == [RecordOutcome.UNCHANGED]
    assert write_calls(target) == []
