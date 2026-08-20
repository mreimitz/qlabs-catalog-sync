"""Decisions D2 (unresolved dataset members) and D3 (owner emails matching no Qlik
user) are queryable per run -- not buried in a JSON blob.

``run_history_helpers.UnresolvedRefTarget`` is a real Qlik-shaped write target that
reports one field it could not resolve on every create/update, through
``WriteResult.skipped_fields`` -- the SDK's own documented channel for D2/D3 (see
``WriteResult``'s docstring in ``qlabs_catalog_sync_sdk.contract``). The report driving
these tests is the real output of a ``SyncLoop`` cycle against it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from run_history_helpers import UnresolvedRefTarget, seed_product

from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.sync.loop import RecordOutcome, SyncLoop
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector

STARTED_AT = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_an_unresolved_owner_field_is_recorded_and_queryable(
    recorder: RunRecorder,
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
) -> None:
    seed_product(source, "sales.orders", owners=["owner@example.com"])
    loop = make_loop(create_missing=True, target=UnresolvedRefTarget(unresolved_field="owners"))

    run_id = await recorder.start(
        pair=loop.pair.name,
        source_endpoint=loop.source_endpoint,
        target_endpoint=loop.target_endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        dry_run=False,
        started_at=STARTED_AT,
    )
    report = await loop.run_cycle(EntityType.DATA_PRODUCT)
    await recorder.finish(run_id, report)

    # Confirm the report itself really carries the D2/D3 channel -- otherwise this test
    # would prove nothing about the recorder.
    assert report.target_skipped_fields
    assert report.target_skipped_fields[0] == ("sales.orders", "owners")

    items = await recorder.list_items(run_id)
    assert len(items) == 1
    item = items[0]
    assert item.native_key == "sales.orders"
    assert item.outcome is RecordOutcome.CREATED
    assert item.unresolved_fields == ("owners",)

    # "Show me everything this run could not resolve" is a plain filter over a real
    # column, not a scan through a JSON blob.
    unresolved = [it for it in items if it.unresolved_fields]
    assert unresolved == [item]


@pytest.mark.asyncio
async def test_a_run_with_no_unresolved_fields_reports_none(
    recorder: RunRecorder,
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
) -> None:
    seed_product(source, "sales.orders")
    loop = make_loop(create_missing=True)  # the plain write_target fixture, no injected skip

    run_id = await recorder.start(
        pair=loop.pair.name,
        source_endpoint=loop.source_endpoint,
        target_endpoint=loop.target_endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        dry_run=False,
        started_at=STARTED_AT,
    )
    report = await loop.run_cycle(EntityType.DATA_PRODUCT)
    await recorder.finish(run_id, report)

    items = await recorder.list_items(run_id)
    assert [it for it in items if it.unresolved_fields] == []
