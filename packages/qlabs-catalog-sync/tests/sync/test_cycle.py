"""The canonical cycle end to end: read, resolve, diff, write, persist, advance.

These are the T2.4 definition-of-done tests for the happy path. Everything is asserted
against real state -- the migrated database, the target connector's own store, and its
recorded call log -- rather than against the loop's return value alone.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sync_helpers import cursor_position, data_product, seed_product, write_calls

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.observability import (
    METRIC_CYCLE_DURATION_SECONDS,
    METRIC_READS_TOTAL,
    METRIC_SKIPS_TOTAL,
    METRIC_WRITES_APPLIED_TOTAL,
    METRIC_WRITES_PLANNED_TOTAL,
)
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import (
    ACTIVATION_FIELD,
    RecordOutcome,
    RunStatus,
    SkipReason,
    SyncLoop,
)
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def test_one_cycle_reads_resolves_writes_persists_and_advances(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """One cycle carries a source object all the way into the target and into state."""
    seed_product(source, "sales.orders", description="Order facts", tags=[("tier", "gold")])

    report = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    assert report.status is RunStatus.OK
    assert report.committed is True
    assert report.pages == 1
    assert [record.outcome for record in report.records] == [RecordOutcome.CREATED]

    # The target really holds it, and the loop really called the write path exactly once.
    assert write_calls(target) == ["create"]
    created = target.calls("create")[0].result
    assert created is not None
    stored_entity = await target.read(created.ref)
    assert stored_entity.model_dump()["name"] == "orders"

    # Identity: bound on both sides, and the target binding points at the key the target
    # itself returned -- nothing was matched by name.
    record = report.records[0]
    assert record.neutral_id is not None
    product = EntityType.DATA_PRODUCT
    source_binding = await store.get_binding(record.neutral_id, source.name, product)
    target_binding = await store.get_binding(record.neutral_id, target.name, product)
    assert source_binding is not None and source_binding.identity.native_key == "sales.orders"
    assert target_binding is not None
    assert target_binding.identity.native_key == created.ref.native_key
    assert target_binding.confirmed is True

    # Envelopes are persisted against the *target* endpoint: that is the diff baseline.
    envelopes = await store.fetch_envelopes(record.neutral_id, target.name)
    assert envelopes["name"].value == "orders"
    assert envelopes["name"].checksum is not None
    assert envelopes["name"].source_revision == created.source_revision

    # And the watermark advanced to exactly what the source proposed.
    watermark = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert watermark is not None
    proposed = source.calls("list_changed")[0].result
    assert watermark.watermark_token == proposed.next_watermark.model_dump_json()
    assert cursor_position(watermark.watermark_token) == "1"
    assert watermark.last_status == RunStatus.OK.value
    assert report.watermark_advanced is True


async def test_an_existing_confirmed_counterpart_is_updated_not_created(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """With a confirmed binding, the loop updates the bound object and never creates one."""
    seed_product(source, "sales.orders", description="Order facts")
    existing = target.seed(data_product("orders"), native_key="qlik-orders")

    loop = make_loop()

    # Discover the neutral id the engine will mint by running one cycle with creation off,
    # then bind that id to the pre-existing target object and run again.
    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    neutral_id = first.records[0].neutral_id
    assert neutral_id is not None
    assert first.records[0].reason is SkipReason.NO_TARGET_BINDING

    async with store.unit_of_work() as uow:
        await uow.bind_identity(neutral_id, existing, confirmed=True, now=first.started_at)

    target.reset_call_log()
    second = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert second.records[0].outcome is RecordOutcome.WRITTEN
    assert write_calls(target) == ["update"]
    assert second.records[0].target_native_key == "qlik-orders"


async def test_a_dry_run_plans_everything_and_applies_nothing(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """T2.8's seam: the full plan, with zero mutations to Qlik *and* zero to the state store."""
    seed_product(source, "sales.orders", description="Order facts", tags=[("tier", "gold")])

    report = await make_loop(create_missing=True, dry_run=True).run_cycle(EntityType.DATA_PRODUCT)

    record = report.records[0]
    assert report.dry_run is True
    assert report.committed is False
    assert record.outcome is RecordOutcome.CREATED
    assert record.changed_fields  # it says what it would carry across

    assert write_calls(target) == []
    assert await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT) is None
    assert record.neutral_id is not None
    assert await store.get_binding(record.neutral_id, source.name, EntityType.DATA_PRODUCT) is None
    assert await store.fetch_envelopes(record.neutral_id, target.name) == {}


async def test_an_update_drops_unwritable_fields_and_reports_the_reason(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """A field the target declares ``ro``/``na`` is never written, and is reported as dropped."""
    seed_product(source, "sales.orders", description="Order facts")
    existing = target.seed(data_product("something else"), native_key="qlik-orders")

    loop = make_loop()
    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    neutral_id = first.records[0].neutral_id
    assert neutral_id is not None
    async with store.unit_of_work() as uow:
        await uow.bind_identity(neutral_id, existing, confirmed=True, now=first.started_at)

    report = await loop.run_cycle(EntityType.DATA_PRODUCT)
    record = report.records[0]

    dropped = {item.field: item.reason.value for item in record.dropped}
    assert dropped == {"glossary_term_refs": "not_applicable", "placement": "read_only"}
    assert "glossary_term_refs" not in record.written_fields
    assert "placement" not in record.written_fields
    # And the same information is reachable from the run report as a whole.
    assert {name for _, item in report.dropped_fields for name in (item.field,)} == set(dropped)


async def test_activation_is_opt_in_and_off_by_default(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """Decision D7: without the pair opting in, ``status`` is never written."""
    seed_product(source, "sales.orders", description="Order facts")
    existing = target.seed(data_product("orders"), native_key="qlik-orders")

    loop = make_loop()
    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    neutral_id = first.records[0].neutral_id
    assert neutral_id is not None
    async with store.unit_of_work() as uow:
        await uow.bind_identity(neutral_id, existing, confirmed=True, now=first.started_at)

    report = await loop.run_cycle(EntityType.DATA_PRODUCT)
    record = report.records[0]

    assert [item.field for item in record.withheld] == [ACTIVATION_FIELD]
    assert record.withheld[0].reason == "activation_not_opted_in"
    assert ACTIVATION_FIELD not in record.written_fields
    update = target.calls("update")[0]
    assert ACTIVATION_FIELD not in update.args["diff"].field_names
    # Nothing was persisted for it either, so it stays visible next cycle.
    envelopes = await store.fetch_envelopes(neutral_id, target.name)
    assert ACTIVATION_FIELD not in envelopes


async def test_activation_is_written_once_the_pair_opts_in(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
) -> None:
    """The same pair with ``activation_opt_in`` set does write ``status``."""
    seed_product(source, "sales.orders", description="Order facts")
    existing = target.seed(data_product("orders"), native_key="qlik-orders")
    opted_in = pair.model_copy(update={"activation_opt_in": True})

    loop = make_loop(pair=opted_in)
    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    neutral_id = first.records[0].neutral_id
    assert neutral_id is not None
    async with store.unit_of_work() as uow:
        await uow.bind_identity(neutral_id, existing, confirmed=True, now=first.started_at)

    report = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert report.records[0].withheld == ()


async def test_the_catalog_schema_selector_scopes_the_pair(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, target: FakeConnector
) -> None:
    """Decision D1: an object outside the pair's patterns is filtered, not read or written."""
    seed_product(source, "sales.orders")
    seed_product(source, "hr.people")

    report = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    outcomes = {record.native_key: record.outcome for record in report.records}
    assert outcomes == {
        "sales.orders": RecordOutcome.CREATED,
        "hr.people": RecordOutcome.FILTERED,
    }
    # Filtered objects are never even read, and never hold the watermark back.
    assert [call.args["ref"].native_key for call in source.calls("read")] == ["sales.orders"]
    assert report.watermark_held_by == ()
    assert report.status is RunStatus.OK


async def test_the_loop_emits_only_the_declared_metrics(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    metrics: Any,
) -> None:
    """T2.7 already named these; the loop emits them and invents none of its own."""
    seed_product(source, "sales.orders")

    await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    assert metrics.total(METRIC_READS_TOTAL) == 1
    assert metrics.total(METRIC_WRITES_PLANNED_TOTAL) == 1
    assert metrics.total(METRIC_WRITES_APPLIED_TOTAL) == 1
    assert metrics.total(METRIC_SKIPS_TOTAL) == 0
    assert [name for name, _, _ in metrics.observations] == [METRIC_CYCLE_DURATION_SECONDS]
    emitted = {name for name, _, _ in metrics.counters}
    assert emitted <= {
        METRIC_READS_TOTAL,
        METRIC_WRITES_PLANNED_TOTAL,
        METRIC_WRITES_APPLIED_TOTAL,
        METRIC_SKIPS_TOTAL,
    }


async def test_the_source_is_never_written_to(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, target: FakeConnector
) -> None:
    """Upstream only: v1's sole write target is Qlik, and the loop proves it structurally."""
    seed_product(source, "sales.orders")

    await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    assert write_calls(source) == []
    assert "delete" not in write_calls(target)


async def test_a_pair_with_the_same_endpoint_twice_is_rejected(
    pair: SyncPairConfig, source: FakeConnector, store: StateStore, resolver: object
) -> None:
    """A pair must have two distinct endpoints; a loop over one is refused at construction."""
    import pytest

    with pytest.raises(ValueError, match="same endpoint"):
        SyncLoop(
            pair=pair,
            source=source,
            target=source,
            store=store,
            resolver=resolver,  # type: ignore[arg-type]
        )
