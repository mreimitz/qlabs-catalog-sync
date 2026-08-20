"""A cycle that fails on its own terms -- ``run_cycle`` still returns a report (per its
documented contract: it never raises for a connector/engine failure), with
``RunStatus.FAILED`` and the error(s) that caused it. ``RunRecorder.finish`` is what
this task's DoD means by "a crashed cycle leaves the run marked failed": most crashes
are exactly this case, not the process-death case ``test_crash_recovery.py`` covers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from run_history_helpers import seed_product

from qlabs_catalog_sync.runs.models import RunRecordStatus
from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.sync.loop import SyncLoop
from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector

STARTED_AT = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_an_auth_failure_during_preflight_is_recorded_as_failed_with_its_error(
    recorder: RunRecorder,
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
) -> None:
    seed_product(source, "sales.orders")
    source.fail_next("healthcheck", AuthError("credentials rejected", endpoint="fake-source"))
    loop = make_loop(create_missing=True)

    run_id = await recorder.start(
        pair=loop.pair.name,
        source_endpoint=loop.source_endpoint,
        target_endpoint=loop.target_endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        dry_run=False,
        started_at=STARTED_AT,
    )
    report = await loop.run_cycle(EntityType.DATA_PRODUCT)  # never raises -- returns FAILED
    await recorder.finish(run_id, report)

    assert report.status.value == "failed"
    assert report.committed is False

    run = await recorder.get_run(run_id)
    assert run is not None
    assert run.status is RunRecordStatus.FAILED
    assert run.committed is False
    assert run.watermark_advanced is False
    # Nothing was read or written -- the failure was in preflight, before any record.
    assert run.created_count == 0
    assert run.read_count == 0

    errors = await recorder.list_errors(run_id)
    assert len(errors) == 1
    assert errors[0].kind == "AuthError"
    assert errors[0].message == "credentials rejected"
    assert errors[0].endpoint == "fake-source"
    assert errors[0].fatal is True
    assert errors[0].retryable is False
