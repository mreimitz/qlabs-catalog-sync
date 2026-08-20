"""A failing pair must not take down the scheduler, and must not stop any other pair.

Also covers the health-degradation escalation this module adds on top of
:class:`~qlabs_catalog_sync.observability.HealthRegistry`, and confirms a ``FAILED``
:class:`~qlabs_catalog_sync.sync.loop.SyncRunReport` (the documented, expected shape of a
failed cycle) is handled as a normal outcome rather than treated like an exception.
"""

from __future__ import annotations

import asyncio

from scheduler_helpers import ScriptedRunner, fire_now, make_pair, pump, wait_until

from qlabs_catalog_sync.observability import HealthRegistry
from qlabs_catalog_sync.scheduler import SyncScheduler
from qlabs_catalog_sync.sync.loop import RunStatus
from qlabs_catalog_sync_sdk.models import EntityType


async def test_one_pair_raising_does_not_stop_another_pair_or_the_scheduler() -> None:
    flaky = ScriptedRunner(make_pair(name="flaky"))
    flaky.raises = True
    steady = ScriptedRunner(make_pair(name="steady"))

    scheduler = SyncScheduler(runners=[flaky, steady])
    try:
        scheduler.start()

        fire_now(scheduler.scheduler.get_job("flaky"))
        await pump(scheduler)
        await wait_until(lambda: scheduler.consecutive_failures("flaky") == 1)

        # The scheduler itself, and the other pair's job, are unaffected.
        assert {job.id for job in scheduler.scheduler.get_jobs()} == {"flaky", "steady"}

        fire_now(scheduler.scheduler.get_job("steady"))
        await pump(scheduler)
        await wait_until(lambda: len(steady.calls) == 1)
        assert scheduler.consecutive_failures("steady") == 0
    finally:
        await scheduler.shutdown()


async def test_a_failed_report_is_a_normal_outcome_not_an_exception() -> None:
    """``run_cycle`` returning ``RunStatus.FAILED`` (never raising -- see the loop's
    documented contract) must be absorbed exactly like any other report: no exception
    surfaces, the job stays registered, and the failure is counted."""
    runner = ScriptedRunner(make_pair(), status=RunStatus.FAILED)
    scheduler = SyncScheduler(runners=[runner])
    try:
        scheduler.start()
        fire_now(scheduler.scheduler.get_job(runner.pair.name))
        await pump(scheduler)
        await wait_until(lambda: len(runner.calls) == 1)

        assert scheduler.consecutive_failures(runner.pair.name) == 1
        assert scheduler.scheduler.get_job(runner.pair.name) is not None
    finally:
        await scheduler.shutdown()


async def test_repeated_failure_marks_the_pair_degraded_and_recovery_clears_it() -> None:
    health = HealthRegistry()
    runner = ScriptedRunner(make_pair(name="p"), status=RunStatus.FAILED)
    scheduler = SyncScheduler(runners=[runner], health=health, degraded_after=2)
    try:
        scheduler.start()
        job = scheduler.scheduler.get_job("p")

        fire_now(job)
        await pump(scheduler)
        await wait_until(lambda: len(runner.calls) == 1)
        # One failure is not yet a pattern (degraded_after=2).
        assert health.snapshot()["components"].get("p", {}).get("healthy", True) is not False

        fire_now(job)
        await pump(scheduler)
        await wait_until(lambda: len(runner.calls) == 2)
        assert health.snapshot()["components"]["p"]["healthy"] is False
        assert health.snapshot()["status"] == "degraded"

        # Recovery: the next non-failed fire resets the counter and marks it healthy again.
        runner.status = RunStatus.OK
        fire_now(job)
        await pump(scheduler)
        await wait_until(lambda: len(runner.calls) == 3)
        assert scheduler.consecutive_failures("p") == 0
        assert health.snapshot()["components"]["p"]["healthy"] is True
    finally:
        await scheduler.shutdown()


async def test_skipped_status_does_not_count_toward_degradation() -> None:
    """``RunStatus.SKIPPED`` means this pair/entity-type combination is not configured to
    run at all -- a standing fact, not a transient failure. It must never itself flip a pair
    to degraded no matter how many times it fires."""
    health = HealthRegistry()
    runner = ScriptedRunner(make_pair(name="p"), status=RunStatus.SKIPPED)
    scheduler = SyncScheduler(runners=[runner], health=health, degraded_after=1)
    try:
        scheduler.start()
        job = scheduler.scheduler.get_job("p")
        for i in range(3):
            fire_now(job)
            await pump(scheduler)
            await wait_until(lambda i=i: len(runner.calls) == i + 1)
            # apscheduler decrements its own running-instance count from a done-callback on
            # the submitted task, which needs a few more ticks after the coroutine itself
            # returns -- settle before forcing the job due again, or this fire can race that
            # bookkeeping and be (wrongly, for this test's purposes) skipped as an overlap.
            for _ in range(5):
                await asyncio.sleep(0)

        assert scheduler.consecutive_failures("p") == 0
        assert health.snapshot()["components"]["p"]["healthy"] is True
    finally:
        await scheduler.shutdown()


async def test_a_pair_syncing_two_entity_types_runs_both_even_after_a_prior_failed_fire() -> None:
    """Confirms a genuinely unexpected exception only aborts the *current* fire's remaining
    entity types (see the module docstring) -- the pair as a whole keeps being scheduled and
    both entity types run cleanly on the next fire."""
    pair = make_pair(entity_types=[EntityType.DATA_PRODUCT, EntityType.DATASET])
    runner = ScriptedRunner(pair)
    runner.raises = True
    scheduler = SyncScheduler(runners=[runner])
    try:
        scheduler.start()
        job = scheduler.scheduler.get_job(pair.name)

        fire_now(job)
        await pump(scheduler)
        await wait_until(lambda: scheduler.consecutive_failures(pair.name) == 1)
        assert runner.calls == [EntityType.DATA_PRODUCT]  # crashed before DATASET ran

        runner.raises = False
        fire_now(job)
        await pump(scheduler)
        await wait_until(lambda: len(runner.calls) == 3)
        assert runner.calls[1:] == [EntityType.DATA_PRODUCT, EntityType.DATASET]
        assert scheduler.consecutive_failures(pair.name) == 0
    finally:
        await scheduler.shutdown()
