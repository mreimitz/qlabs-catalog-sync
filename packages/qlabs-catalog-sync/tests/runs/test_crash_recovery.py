"""What happens when a cycle never produces a report -- and the case this codebase's
own tooling cannot fully close: the process itself dying between ``start`` and
``finish``/``fail``.

:func:`test_a_crashed_process_leaves_a_run_stuck_until_the_next_process_reaps_it` is the
dishonest case the task calls for explicitly: it proves a run *can* be left stuck at
``RUNNING`` (nothing here silently hides that), and that :meth:`RunRecorder.reap_stale`
is what actually closes it out -- if that method were missing or a no-op, this test's
final assertion would fail.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from qlabs_catalog_sync.runs.models import RunRecordStatus
from qlabs_catalog_sync.runs.recorder import STALE_RUN_MESSAGE, RunRecorder
from qlabs_catalog_sync_sdk.models import EntityType

STARTED_AT = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


async def _start(recorder: RunRecorder, *, started_at: datetime = STARTED_AT) -> uuid.UUID:
    return await recorder.start(
        pair="db-to-qlik",
        source_endpoint="fake-source",
        target_endpoint="fake-target",
        entity_type=EntityType.DATA_PRODUCT,
        dry_run=False,
        started_at=started_at,
    )


@pytest.mark.asyncio
async def test_a_crashed_process_leaves_a_run_stuck_until_the_next_process_reaps_it(
    recorder: RunRecorder,
) -> None:
    # Simulate the process dying immediately after start(): nothing else ever runs.
    run_id = await _start(recorder)

    stuck = await recorder.get_run(run_id)
    assert stuck is not None
    assert stuck.status is RunRecordStatus.RUNNING
    assert stuck.finished_at is None

    # Nothing about the row resolves itself -- it is still exactly as stuck a moment
    # later, without an explicit sweep.
    still_stuck = await recorder.get_run(run_id)
    assert still_stuck is not None
    assert still_stuck.status is RunRecordStatus.RUNNING

    # The next process's startup sweep is what actually closes it out.
    reaped = await recorder.reap_stale(now=STARTED_AT + timedelta(hours=1))
    assert reaped == (run_id,)

    closed = await recorder.get_run(run_id)
    assert closed is not None
    assert closed.status is RunRecordStatus.FAILED
    assert closed.committed is False
    assert closed.finished_at == STARTED_AT + timedelta(hours=1)
    assert closed.duration_seconds is None
    assert closed.error_count == 1

    errors = await recorder.list_errors(run_id)
    assert len(errors) == 1
    assert errors[0].kind == "RunReaped"
    assert errors[0].message == STALE_RUN_MESSAGE
    assert errors[0].fatal is True


@pytest.mark.asyncio
async def test_reap_stale_does_not_touch_a_run_that_already_finished(
    recorder: RunRecorder,
) -> None:
    run_id = await _start(recorder)
    await recorder.fail(run_id, message="boom", finished_at=STARTED_AT + timedelta(seconds=5))

    reaped = await recorder.reap_stale(now=STARTED_AT + timedelta(hours=1))

    assert run_id not in reaped
    run = await recorder.get_run(run_id)
    assert run is not None
    assert run.finished_at == STARTED_AT + timedelta(seconds=5)  # untouched by the sweep


@pytest.mark.asyncio
async def test_reap_stale_with_older_than_leaves_a_recent_run_alone(
    recorder: RunRecorder,
) -> None:
    run_id = await _start(recorder, started_at=STARTED_AT)

    # "now" is only five minutes later; a one-hour grace window must not reap this yet.
    reaped = await recorder.reap_stale(
        now=STARTED_AT + timedelta(minutes=5), older_than=timedelta(hours=1)
    )

    assert reaped == ()
    run = await recorder.get_run(run_id)
    assert run is not None
    assert run.status is RunRecordStatus.RUNNING


@pytest.mark.asyncio
async def test_fail_closes_out_a_run_the_cycle_never_finished(recorder: RunRecorder) -> None:
    """The case sync/loop.py's own _load_watermark gap (called before run_cycle's try
    block) needs: an exception escapes run_cycle entirely, with no report to finish()
    with. See runs.recorder's module docstring."""
    run_id = await _start(recorder)

    await recorder.fail(
        run_id,
        message="StateStoreError: could not read the watermark",
        finished_at=STARTED_AT + timedelta(seconds=2),
        kind="StateStoreError",
        endpoint="fake-source",
    )

    run = await recorder.get_run(run_id)
    assert run is not None
    assert run.status is RunRecordStatus.FAILED
    assert run.committed is False
    assert run.duration_seconds is None

    errors = await recorder.list_errors(run_id)
    assert len(errors) == 1
    assert errors[0].kind == "StateStoreError"
    assert errors[0].fatal is True
    assert errors[0].endpoint == "fake-source"


@pytest.mark.asyncio
async def test_fail_on_an_unknown_run_id_raises_lookup_error(recorder: RunRecorder) -> None:
    with pytest.raises(LookupError):
        await recorder.fail(uuid.uuid4(), message="boom", finished_at=STARTED_AT)


@pytest.mark.asyncio
async def test_fail_cannot_be_called_on_a_run_already_closed_out(recorder: RunRecorder) -> None:
    run_id = await _start(recorder)
    await recorder.fail(run_id, message="first failure", finished_at=STARTED_AT)

    with pytest.raises(RuntimeError, match="already finished"):
        await recorder.fail(run_id, message="second failure", finished_at=STARTED_AT)
