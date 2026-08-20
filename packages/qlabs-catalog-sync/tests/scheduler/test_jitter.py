"""Jitter: bounded, applied, and only ever a delay -- never an earlier fire.

See ``scheduler.py``'s module docstring ("Jitter: up to 10% of cadence, capped at 60
seconds") for why this magnitude was chosen against Qlik's 100 req/min write tier.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from scheduler_helpers import ScriptedRunner, make_pair

from qlabs_catalog_sync.scheduler import (
    DEFAULT_JITTER_CAP_SECONDS,
    DEFAULT_JITTER_FRACTION,
    SyncScheduler,
    jitter_seconds_for,
)


def test_jitter_is_a_fraction_of_cadence() -> None:
    assert jitter_seconds_for(600, fraction=0.10, cap_seconds=60.0) == 60.0
    assert jitter_seconds_for(100, fraction=0.10, cap_seconds=60.0) == 10.0


def test_jitter_is_capped_regardless_of_cadence() -> None:
    huge_cadence = 3600 * 24
    assert jitter_seconds_for(huge_cadence, fraction=0.10, cap_seconds=60.0) == 60.0


def test_jitter_is_never_negative_for_a_tiny_cadence() -> None:
    assert jitter_seconds_for(1, fraction=0.10, cap_seconds=60.0) >= 0.0


def test_jitter_is_zero_for_a_non_positive_cadence_or_fraction() -> None:
    assert jitter_seconds_for(0) == 0.0
    assert jitter_seconds_for(-5) == 0.0
    assert jitter_seconds_for(900, fraction=0.0) == 0.0


def test_the_mvp_default_cadence_gets_the_documented_window() -> None:
    """900s (15 minutes, ``SyncPairConfig.cadence_seconds``'s own default) -> 60s, the cap."""
    assert jitter_seconds_for(900) == DEFAULT_JITTER_CAP_SECONDS
    assert DEFAULT_JITTER_FRACTION == 0.10


async def test_registered_jobs_carry_the_computed_jitter_window() -> None:
    pair = make_pair(cadence_seconds=100)
    scheduler = SyncScheduler(
        runners=[ScriptedRunner(pair)], jitter_fraction=0.10, jitter_cap_seconds=60.0
    )
    try:
        scheduler.start()
        job = scheduler.scheduler.get_job(pair.name)
        assert job.trigger.jitter == 10.0
    finally:
        await scheduler.shutdown()


def test_jitter_only_ever_delays_a_fire_never_advances_it() -> None:
    """``IntervalTrigger`` (``apscheduler.triggers.base.BaseTrigger._apply_jitter``) adds
    ``random.uniform(0, jitter)`` -- always >= 0. Drawn many times against a fixed base fire
    time, every result must land in ``[base, base + jitter]``, proving jitter cannot cause two
    pairs sharing a cadence to fire *earlier* than their configured interval."""
    from apscheduler.triggers.interval import IntervalTrigger

    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
    trigger = IntervalTrigger(seconds=900, jitter=60, timezone=UTC, start_date=now)
    base = now  # the un-jittered first fire is exactly start_date

    draws = [trigger.get_next_fire_time(None, now) for _ in range(200)]
    assert all(base <= draw <= base + timedelta(seconds=60) for draw in draws)


def test_jitter_actually_spreads_fires_apart_not_a_no_op() -> None:
    """A jitter window that never varies the fire time would defeat the whole point (spreading
    a synchronized fleet apart). Assert the 200 draws above are not all identical -- vanishing
    odds under a real ``random.uniform`` draw unless jitter were silently a no-op."""
    from apscheduler.triggers.interval import IntervalTrigger

    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
    trigger = IntervalTrigger(seconds=900, jitter=60, timezone=UTC, start_date=now)

    random.seed(1234)  # deterministic, not order-dependent on other tests' PRNG state
    draws = {trigger.get_next_fire_time(None, now) for _ in range(50)}
    assert len(draws) > 1
