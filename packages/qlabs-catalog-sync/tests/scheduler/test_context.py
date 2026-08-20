"""Every ``run_cycle`` call the scheduler makes happens inside ``bind_sync_context(pair=...)``,
so any log line emitted from anywhere inside a cycle -- this module's own or deep inside
``SyncLoop`` -- carries the pair name without having to thread it through every call site.
"""

from __future__ import annotations

from scheduler_helpers import ScriptedRunner, fire_now, make_pair, pump, wait_until

from qlabs_catalog_sync.scheduler import SyncScheduler
from qlabs_catalog_sync_sdk.models import EntityType


async def test_run_cycle_is_called_with_the_pair_bound_in_log_context() -> None:
    pair = make_pair(name="acme-sales", entity_types=[EntityType.DATA_PRODUCT, EntityType.DATASET])
    runner = ScriptedRunner(pair)
    scheduler = SyncScheduler(runners=[runner])
    try:
        scheduler.start()
        fire_now(scheduler.scheduler.get_job(pair.name))
        await pump(scheduler)
        await wait_until(lambda: len(runner.calls) == 2)

        assert len(runner.bound_contexts) == 2
        for bound in runner.bound_contexts:
            assert bound.get("pair") == "acme-sales"
    finally:
        await scheduler.shutdown()


async def test_two_pairs_never_see_each_others_bound_context() -> None:
    """``bind_sync_context`` rides ``contextvars``, task-local by construction (T2.7 already
    proves this under concurrency for the loop itself); this is the scheduler-level version
    of the same guarantee -- two pairs firing back to back never leak one's pair name into
    the other's cycle."""
    pair_a = make_pair(name="a")
    pair_b = make_pair(name="b")
    runner_a = ScriptedRunner(pair_a)
    runner_b = ScriptedRunner(pair_b)
    scheduler = SyncScheduler(runners=[runner_a, runner_b])
    try:
        scheduler.start()
        fire_now(scheduler.scheduler.get_job("a"))
        fire_now(scheduler.scheduler.get_job("b"))
        await pump(scheduler)
        await wait_until(lambda: len(runner_a.calls) == 1 and len(runner_b.calls) == 1)

        assert runner_a.bound_contexts[0]["pair"] == "a"
        assert runner_b.bound_contexts[0]["pair"] == "b"
    finally:
        await scheduler.shutdown()
