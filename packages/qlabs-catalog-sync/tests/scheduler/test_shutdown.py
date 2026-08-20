"""Graceful shutdown: pause new fires immediately, let an in-flight cycle finish, and only
abandon (cancel) one that is still running past the shutdown timeout.

See ``scheduler.py``'s module docstring ("Shutdown") for the full reasoning: an abandoned
cycle is safe (the engine only ever commits inside one transaction) but wastes whatever
write-tier budget it already spent, so finishing what is already running is strictly better
than cutting it off, within a bound.
"""

from __future__ import annotations

import asyncio

from apscheduler.schedulers.base import STATE_STOPPED
from scheduler_helpers import ScriptedRunner, fire_now, make_pair, pump, wait_until

from qlabs_catalog_sync.scheduler import SyncScheduler


async def test_shutdown_with_nothing_in_flight_stops_the_scheduler_promptly() -> None:
    runner = ScriptedRunner(make_pair())
    scheduler = SyncScheduler(runners=[runner])
    scheduler.start()

    await scheduler.shutdown()

    assert scheduler.scheduler.state == STATE_STOPPED


async def test_shutdown_waits_for_an_in_flight_cycle_to_finish_rather_than_cancel_it() -> None:
    runner = ScriptedRunner(make_pair())
    runner.block = True
    scheduler = SyncScheduler(runners=[runner])
    scheduler.start()

    fire_now(scheduler.scheduler.get_job(runner.pair.name))
    await pump(scheduler)
    await wait_until(lambda: runner.started.is_set())

    shutdown_task = asyncio.create_task(scheduler.shutdown(timeout=None))
    # shutdown()'s only code before its first await is "if not started: return" and
    # scheduler.pause()" -- both synchronous. asyncio runs a task's synchronous prefix to
    # completion before yielding control back here, so after exactly one tick, pause() is
    # guaranteed to have already run and shutdown() is guaranteed to now be blocked inside
    # `await asyncio.wait(inflight, ...)`. This is not a timing guess.
    await asyncio.sleep(0)

    # New fires must be refused while a graceful shutdown is draining -- prove it against the
    # same job, forced due again, exactly as a real overlapping cadence fire would look.
    fire_now(scheduler.scheduler.get_job(runner.pair.name))
    await pump(scheduler)
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(runner.calls) == 1  # still just the one that was already in flight

    # Let the in-flight cycle finish on its own; shutdown must not have cancelled it.
    runner.release.set()
    await asyncio.wait_for(shutdown_task, timeout=1.0)

    assert runner.cancelled is False
    assert runner.finished == runner.calls
    assert scheduler.scheduler.state == STATE_STOPPED


async def test_shutdown_abandons_a_cycle_still_running_past_its_timeout() -> None:
    runner = ScriptedRunner(make_pair())
    runner.block = True
    scheduler = SyncScheduler(runners=[runner])
    scheduler.start()

    fire_now(scheduler.scheduler.get_job(runner.pair.name))
    await pump(scheduler)
    await wait_until(lambda: runner.started.is_set())

    # Never release the cycle: shutdown must give up after its bound rather than hang forever.
    # A short, deterministic real bound is unavoidable here -- proving a wall-clock timeout
    # parameter fires requires letting some real time pass -- but the outcome does not race
    # anything: the task is never released, so timing out is the only possible result.
    await scheduler.shutdown(timeout=0.05)

    assert runner.cancelled is True
    assert scheduler.scheduler.state == STATE_STOPPED
