"""Dry runs are recorded, exactly like a real cycle, distinguished by ``dry_run`` and
``committed`` -- never silently dropped and never conflated with a committed cycle. See
``runs.recorder``'s module docstring ("Dry runs are recorded") for the reasoning.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from run_history_helpers import seed_product

from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.sync.loop import SyncLoop
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector

STARTED_AT = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_a_dry_run_is_recorded_with_dry_run_true_and_nothing_committed(
    recorder: RunRecorder,
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
) -> None:
    seed_product(source, "sales.orders")
    loop = make_loop(create_missing=True, dry_run=True)

    run_id = await recorder.start(
        pair=loop.pair.name,
        source_endpoint=loop.source_endpoint,
        target_endpoint=loop.target_endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        dry_run=loop.dry_run,
        started_at=STARTED_AT,
    )
    report = await loop.run_cycle(EntityType.DATA_PRODUCT)
    await recorder.finish(run_id, report)

    assert report.dry_run is True
    assert report.committed is False

    run = await recorder.get_run(run_id)
    assert run is not None
    assert run.dry_run is True
    assert run.committed is False
    assert run.watermark_advanced is False
    # The plan phase still ran and still planned a create -- a dry run previews, it does
    # not do nothing.
    assert run.created_count == 1

    # A default "real cycles only" console view filters on either column without this
    # run ever having been thrown away.
    real_only = await recorder.list_runs(pair="db-to-qlik")
    assert len(real_only) == 1  # still listed -- filtering is the reader's job, not ours
    assert real_only[0].dry_run is True
