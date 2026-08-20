"""``RunRecorder.start``/``finish`` against a real ``SyncRunReport``.

The report driving these tests comes from an actual ``SyncLoop.run_cycle`` call against
real ``FakeConnector`` instances (per the task's own instruction: prove the recorder
against what the loop really emits, not a hand-built dataclass). ``conftest.py``'s
``make_loop`` builds the loop; ``run_history_helpers.seed_product`` seeds the source.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from run_history_helpers import seed_product

from qlabs_catalog_sync.runs.models import RunRecordStatus
from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.sync.loop import RecordOutcome, SyncLoop
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector

STARTED_AT = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


async def _run_and_record(
    recorder: RunRecorder, loop: SyncLoop, entity_type: EntityType, *, dry_run: bool = False
) -> tuple[object, object]:
    """Drive one cycle exactly the way the recommended scheduler wiring would."""
    run_id = await recorder.start(
        pair=loop.pair.name,
        source_endpoint=loop.source_endpoint,
        target_endpoint=loop.target_endpoint,
        entity_type=entity_type,
        dry_run=dry_run,
        started_at=STARTED_AT,
    )
    report = await loop.run_cycle(entity_type)
    await recorder.finish(run_id, report)
    return run_id, report


@pytest.mark.asyncio
async def test_a_clean_create_is_recorded_and_produces_no_run_item(
    recorder: RunRecorder,
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
) -> None:
    seed_product(source, "sales.orders", description="Order events")
    loop = make_loop(create_missing=True)

    run_id, report = await _run_and_record(recorder, loop, EntityType.DATA_PRODUCT)

    run = await recorder.get_run(run_id)
    assert run is not None
    assert run.status is RunRecordStatus(report.status.value)
    assert run.status is RunRecordStatus.OK
    assert run.pair == "db-to-qlik"
    assert run.source_endpoint == "fake-source"
    assert run.target_endpoint == "fake-target"
    assert run.entity_type is EntityType.DATA_PRODUCT
    assert run.dry_run is False
    assert run.committed is True
    assert run.created_count == 1
    assert run.write_count == 1
    assert run.started_at == STARTED_AT
    assert run.finished_at == report.finished_at
    assert run.duration_seconds == report.duration_seconds

    # A clean create -- nothing withheld, nothing unresolved, nothing held back -- is not
    # a run_items row. See runs.models's module docstring / is_reportable.
    items = await recorder.list_items(run_id)
    assert items == []


@pytest.mark.asyncio
async def test_counts_reconcile_with_the_report_for_every_outcome(
    recorder: RunRecorder,
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
) -> None:
    """The DoD: 'counts reconcile with what the loop actually wrote'."""
    seed_product(source, "sales.orders")
    # Outside the pair's "sales.*" selection pattern -- filtered, not synced.
    seed_product(source, "marketing.leads")
    loop = make_loop(create_missing=True)

    run_id, report = await _run_and_record(recorder, loop, EntityType.DATA_PRODUCT)

    run = await recorder.get_run(run_id)
    assert run is not None
    assert run.read_count == report.read_count
    assert run.created_count == report.count(RecordOutcome.CREATED)
    assert run.filtered_count == 1
    assert run.created_count == 1
    assert run.written_count == 0
    assert run.unchanged_count == 0
    assert run.no_op_count == 0
    assert run.skipped_count == 0
    assert run.orphaned_count == 0
    assert run.failed_count == 0
    total_recorded = (
        run.created_count
        + run.written_count
        + run.unchanged_count
        + run.no_op_count
        + run.skipped_count
        + run.orphaned_count
        + run.filtered_count
        + run.failed_count
    )
    assert total_recorded == len(report.records)

    # The filtered record is counted, but -- the big-metastore case -- it is not a row.
    items = await recorder.list_items(run_id)
    assert items == []


@pytest.mark.asyncio
async def test_finish_cannot_be_called_twice_for_the_same_run(
    recorder: RunRecorder,
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
) -> None:
    seed_product(source, "sales.orders")
    loop = make_loop(create_missing=True)
    run_id, report = await _run_and_record(recorder, loop, EntityType.DATA_PRODUCT)

    with pytest.raises(RuntimeError, match="already finished"):
        await recorder.finish(run_id, report)


@pytest.mark.asyncio
async def test_finish_on_an_unknown_run_id_raises_lookup_error(
    recorder: RunRecorder,
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
) -> None:
    seed_product(source, "sales.orders")
    loop = make_loop(create_missing=True)
    report = await loop.run_cycle(EntityType.DATA_PRODUCT)

    with pytest.raises(LookupError):
        await recorder.finish(uuid.uuid4(), report)


@pytest.mark.asyncio
async def test_get_run_returns_none_for_an_unknown_id(recorder: RunRecorder) -> None:
    assert await recorder.get_run(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_list_runs_filters_by_pair_entity_type_and_status(
    recorder: RunRecorder,
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
) -> None:
    seed_product(source, "sales.orders")
    loop = make_loop(create_missing=True)
    await _run_and_record(recorder, loop, EntityType.DATA_PRODUCT)

    matches = await recorder.list_runs(
        pair="db-to-qlik", entity_type=EntityType.DATA_PRODUCT, status=RunRecordStatus.OK
    )
    assert len(matches) == 1

    no_matches = await recorder.list_runs(pair="does-not-exist")
    assert no_matches == []
