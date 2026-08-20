"""One job per pair, registered with the pair's cadence and this task's overlap guards."""

from __future__ import annotations

from datetime import timedelta

import pytest
from scheduler_helpers import ScriptedRunner, make_pair

from qlabs_catalog_sync.scheduler import SyncScheduler


async def test_one_job_per_pair_with_the_configured_cadence() -> None:
    pairs = [make_pair(name="a", cadence_seconds=60), make_pair(name="b", cadence_seconds=900)]
    scheduler = SyncScheduler(runners=[ScriptedRunner(pair) for pair in pairs])
    try:
        scheduler.start()
        jobs = {job.id: job for job in scheduler.scheduler.get_jobs()}
        assert set(jobs) == {"a", "b"}
        assert jobs["a"].trigger.interval == timedelta(seconds=60)
        assert jobs["b"].trigger.interval == timedelta(seconds=900)
    finally:
        await scheduler.shutdown()


async def test_a_pair_syncing_several_entity_types_is_still_one_job() -> None:
    """The scheduler grain matches the config's grain: one cadence per pair, so one job per
    pair, regardless of how many entity types it walks per fire (see the module docstring's
    "One job per pair" section for why per-entity-type jobs would invent a cadence that does
    not exist in ``SyncPairConfig``)."""
    from qlabs_catalog_sync_sdk.models import EntityType

    pair = make_pair(entity_types=[EntityType.DATA_PRODUCT, EntityType.DATASET])
    scheduler = SyncScheduler(runners=[ScriptedRunner(pair)])
    try:
        scheduler.start()
        assert len(scheduler.scheduler.get_jobs()) == 1
    finally:
        await scheduler.shutdown()


async def test_every_job_gets_max_instances_one_and_coalesce_true() -> None:
    pairs = [make_pair(name="a"), make_pair(name="b")]
    scheduler = SyncScheduler(runners=[ScriptedRunner(pair) for pair in pairs])
    try:
        scheduler.start()
        for job in scheduler.scheduler.get_jobs():
            assert job.max_instances == 1
            assert job.coalesce is True
    finally:
        await scheduler.shutdown()


def test_duplicate_pair_name_across_runners_is_rejected() -> None:
    pair = make_pair(name="dupe")
    with pytest.raises(ValueError, match="duplicate pair name"):
        SyncScheduler(runners=[ScriptedRunner(pair), ScriptedRunner(pair)])


def test_no_runners_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        SyncScheduler(runners=[])


def test_degraded_after_must_be_positive() -> None:
    with pytest.raises(ValueError, match="degraded_after"):
        SyncScheduler(runners=[ScriptedRunner(make_pair())], degraded_after=0)


async def test_starting_twice_raises() -> None:
    scheduler = SyncScheduler(runners=[ScriptedRunner(make_pair())])
    scheduler.start()
    try:
        with pytest.raises(RuntimeError, match="twice"):
            scheduler.start()
    finally:
        await scheduler.shutdown()


async def test_shutdown_before_start_is_a_harmless_no_op() -> None:
    scheduler = SyncScheduler(runners=[ScriptedRunner(make_pair())])
    await scheduler.shutdown()  # must not raise
