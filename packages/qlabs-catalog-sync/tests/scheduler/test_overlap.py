"""``max_instances=1`` + ``coalesce=True``: a slow cycle causes the next trigger to be
skipped, not queued -- proved against the real ``AsyncIOScheduler``, not assumed from
``apscheduler``'s documentation and not asserted by mocking the scheduler away.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from apscheduler.events import EVENT_JOB_MAX_INSTANCES
from scheduler_helpers import ScriptedRunner, fire_now, make_pair, pump, wait_until

from qlabs_catalog_sync.scheduler import SyncScheduler


async def test_a_fire_while_the_previous_cycle_is_still_running_is_skipped_not_queued() -> None:
    """Fire once (blocks), fire again while it is still in flight, then let it finish and
    fire a third time. If overlap were queued rather than skipped, the second attempt would
    eventually still run once the first completed, for 3 total calls. Skipped means it
    vanishes entirely: exactly 2 calls, never 3, no matter how long we wait afterward.
    """
    pair = make_pair(cadence_seconds=3600)  # irrelevant: every fire is forced manually below
    runner = ScriptedRunner(pair)
    runner.block = True
    scheduler = SyncScheduler(runners=[runner], jitter_fraction=0.0)
    try:
        scheduler.start()
        job = scheduler.scheduler.get_job(pair.name)

        # Fire 1: starts and blocks inside run_cycle.
        fire_now(job)
        await pump(scheduler)
        await wait_until(lambda: runner.started.is_set())
        assert len(runner.calls) == 1

        # Fire 2: due while fire 1 is still running. apscheduler's executor must refuse to
        # submit it (MaxInstancesReachedError) rather than queue it -- listen for the real
        # event it dispatches for exactly that refusal.
        skipped = asyncio.Event()
        scheduler.scheduler.add_listener(lambda _event: skipped.set(), EVENT_JOB_MAX_INSTANCES)
        fire_now(job)
        await pump(scheduler)
        await wait_until(lambda: skipped.is_set())
        assert len(runner.calls) == 1  # run_cycle was never even called a second time

        # Let fire 1 finish.
        runner.release.set()
        await wait_until(lambda: len(runner.finished) == 1)

        # Fire 3: now that nothing is running, this one must go through normally.
        runner.block = False
        fire_now(job)
        await pump(scheduler)
        await wait_until(lambda: len(runner.calls) == 2)

        # The crucial negative: still exactly 2, never 3. Fire 2's attempt is gone for good,
        # not merely delayed -- give the loop a few more idle ticks to be sure nothing queued
        # behind it surfaces late.
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(runner.calls) == 2
    finally:
        await scheduler.shutdown()


async def test_several_missed_fires_coalesce_into_one_run_not_a_backlog() -> None:
    """Rewind ``next_run_time`` several intervals into the past (as if the process had been
    unable to check in for a while) and let the trigger's own math compute every run time
    that has technically come due. ``coalesce=True`` must collapse that backlog to exactly
    one execution -- never one call per missed interval.
    """
    pair = make_pair(cadence_seconds=1)
    runner = ScriptedRunner(pair)
    scheduler = SyncScheduler(runners=[runner], jitter_fraction=0.0)
    try:
        scheduler.start()
        job = scheduler.scheduler.get_job(pair.name)

        # Several whole intervals overdue: apscheduler would compute >1 pending run time here
        # if it were going to honor them all.
        job.modify(next_run_time=datetime.now(tz=UTC) - timedelta(seconds=5.5))
        await pump(scheduler)
        await wait_until(lambda: len(runner.calls) >= 1)
        # Give any (wrongly) queued extra executions a chance to surface before asserting.
        for _ in range(5):
            await asyncio.sleep(0)

        assert len(runner.calls) == 1
    finally:
        await scheduler.shutdown()
