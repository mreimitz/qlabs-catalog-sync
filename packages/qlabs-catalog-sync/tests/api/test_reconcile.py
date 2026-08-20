"""T12.9: the scheduler reconciles its job set against the configuration store (C1).

Behaviour, not mocks. Every test here runs a **real** ``ConfigService`` over a **real**
migrated SQLite database, hands it to the **real** ``ConfigStorePairSource``, and drives a
**real** ``SyncScheduler`` over a **real** ``apscheduler`` ``AsyncIOScheduler``. The only
double is the pair runner (:class:`RecordingRunner`), for exactly the reason
``tests/scheduler/scheduler_helpers.py`` gives for its own: what these tests prove is *when*
a cycle runs and *which configuration it runs under*, and what happens inside one cycle is
T2.4's test surface, already exercised there against real connectors and a real database.

Every test is written so that it **fails** if the thing C1 forbids happened:

* ``test_a_cycle_in_flight_keeps_the_rule_set_it_started_with`` reads the running cycle's
  rule set *after* the reconcile has landed, so a reconcile that mutated a live runner (or
  swapped what the running cycle uses) fails it.
* ``test_an_unchanged_generation_reads_one_number_and_nothing_else`` counts real reads on the
  configuration source, so a reconcile that reloaded or rebuilt for no change fails it.
* ``test_a_swapped_job_still_cannot_overlap_the_cycle_it_replaced`` counts invocations, which
  is the only way to tell "skipped" from "ran concurrently" from the outside.
* ``test_a_disabled_pair_is_not_scheduled`` and ``test_disabling_an_endpoint_stops_its_pairs``
  fail if a pair was scheduled while something in its chain was disabled.
* ``test_one_broken_pair_does_not_stop_the_others`` fails if a pair whose runner cannot be
  built takes any other pair's job down with it.

Two conventions worth knowing before reading further:

**Nothing waits on the clock.** Jobs are forced due with ``Job.modify(next_run_time=...)``
and the scheduler is woken with its own public ``wakeup()`` -- the same instant, non-flaky
technique ``tests/scheduler`` uses.

**The reconcile timer is deferred, and reconcile is then driven explicitly.** ``start()``
schedules the first reconcile for *now* on purpose (the store is authoritative, so converging
on it is the first thing a process should do). A test that then asserts on what one specific
reconcile pass did would be racing that timer, so :func:`defer_reconcile_job` pushes the
timer an hour out and the tests ``await scheduler.reconcile()`` themselves. The timer path
itself is not left unproven --
``test_the_scheduled_reconcile_job_picks_up_a_new_pair_on_its_own`` drives it exactly as a
running service would, with nobody calling ``reconcile`` at all.

Deliberately self-contained: no shared ``*_helpers`` module. Everything this file needs is
either a real production class or one of the three small doubles defined below, so the
verify command works on this file alone.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from apscheduler.job import Job
from pydantic import SecretStr

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.configstore.types import (
    EndpointRole,
    MatcherKind,
    RuleScope,
    SelectionDecision,
)
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.observability import HealthRegistry
from qlabs_catalog_sync.scheduler import (
    INERT_CATALOG_SCHEMA_PATTERN,
    RECONCILE_JOB_ID,
    ConfigSnapshot,
    ConfigStorePairSource,
    PairPlan,
    SyncScheduler,
)
from qlabs_catalog_sync.selection.evaluator import evaluate
from qlabs_catalog_sync.selection.rules import SelectionCandidate, SelectionRuleSet
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.sync.loop import RunStatus, SyncRunReport
from qlabs_catalog_sync_sdk.config import ConnectorConfig, ConnectorContext
from qlabs_catalog_sync_sdk.contract import (
    CapabilityManifestBase,
    Connector,
    HealthStatus,
    IdentityRef,
    ListChangedResult,
    Watermark,
)
from qlabs_catalog_sync_sdk.models import EntityType, NeutralEntity

NOW: Final[datetime] = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
ACTOR: Final[str] = "console-operator"

#: Two object-scope candidates every test decides against a pair's live rule set. Which of
#: the two is included is how "the new rules reached the next cycle" is asserted as a
#: *behaviour* rather than as "a runner object was replaced".
ANALYTICS = SelectionCandidate(
    scope=RuleScope.OBJECT, object_id="schema-1", qualified_name="analytics.sales"
)
FINANCE = SelectionCandidate(
    scope=RuleScope.OBJECT, object_id="schema-2", qualified_name="finance.ledger"
)


# --------------------------------------------------------------------------------------
# A connector registry ConfigService can validate endpoint settings against
# --------------------------------------------------------------------------------------


class _SourceConfig(ConnectorConfig):
    """A read-only source's config shape: one plain field, no secrets."""

    host: str


class _TargetConfig(ConnectorConfig):
    """The sole v1 write target's shape: one plain field and one required secret."""

    space_id: str
    api_key: SecretStr | None = None


class _StubConnector(Connector):
    """Every abstract method stubbed. Never instantiated: ``ConfigService`` only ever reads
    ``.ConfigModel`` off the class when validating an endpoint's settings."""

    def capabilities(self) -> CapabilityManifestBase:
        raise NotImplementedError

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        raise NotImplementedError

    async def healthcheck(self) -> HealthStatus:
        raise NotImplementedError

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        raise NotImplementedError

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        raise NotImplementedError


class _SourceConnector(_StubConnector):
    name = "databricks"
    ConfigModel = _SourceConfig


class _TargetConnector(_StubConnector):
    name = "qlik"
    ConfigModel = _TargetConfig


# --------------------------------------------------------------------------------------
# Doubles: a pair runner, a counting wrapper around the real source, a runner factory
# --------------------------------------------------------------------------------------


class RecordingRunner:
    """A ``PairRunner`` that remembers the plan it was built from and what it decided.

    ``run_cycle`` reads ``self.plan`` **after** any blocking is released, on purpose: that is
    what makes "a cycle in flight keeps the configuration it started with" an assertion about
    the configuration the cycle actually used, rather than about which object a reconcile
    happened to leave in a dictionary.
    """

    def __init__(self, plan: PairPlan) -> None:
        self.plan = plan
        self.pair: SyncPairConfig = plan.pair
        self.calls: list[EntityType] = []
        self.finished: list[EntityType] = []
        #: One ``(analytics_included, finance_included)`` per completed cycle, decided against
        #: whatever rule set this runner held at the moment the cycle got that far.
        self.decided: list[tuple[bool, bool]] = []
        self.block = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    @property
    def rules(self) -> SelectionRuleSet:
        return self.plan.selection_rules

    async def run_cycle(self, entity_type: EntityType) -> SyncRunReport:
        self.calls.append(entity_type)
        if self.block:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        self.decided.append(
            (evaluate(self.rules, ANALYTICS).included, evaluate(self.rules, FINANCE).included)
        )
        self.finished.append(entity_type)
        return SyncRunReport(
            pair=self.pair.name,
            source_endpoint=self.pair.source,
            target_endpoint=self.pair.target,
            entity_type=entity_type,
            status=RunStatus.OK,
            started_at=NOW,
            finished_at=NOW,
            duration_seconds=0.0,
            committed=True,
        )


class CountingSource:
    """The real :class:`ConfigStorePairSource`, with its two read paths counted.

    Counting is what turns "reconcile is cheap when nothing changed" into something a test can
    fail on: a pass that reloaded when it should not have shows up as an extra ``loads``, not
    as a slowdown nobody notices.
    """

    def __init__(self, inner: ConfigStorePairSource) -> None:
        self._inner = inner
        self.generations = 0
        self.loads = 0
        self.generation_error: Exception | None = None

    async def generation(self) -> int:
        self.generations += 1
        if self.generation_error is not None:
            raise self.generation_error
        return await self._inner.generation()

    async def load(self) -> ConfigSnapshot:
        self.loads += 1
        return await self._inner.load()


class Factory:
    """Builds :class:`RecordingRunner` s, counts builds, and can be told to fail one pair."""

    def __init__(self) -> None:
        self.built: list[RecordingRunner] = []
        self.fail_for: set[str] = set()

    async def __call__(self, plan: PairPlan) -> RecordingRunner:
        if plan.pair.name in self.fail_for:
            raise RuntimeError(f"cannot build {plan.pair.name!r}: endpoint unusable")
        runner = RecordingRunner(plan)
        self.built.append(runner)
        return runner

    def latest(self, pair_name: str) -> RecordingRunner:
        for runner in reversed(self.built):
            if runner.pair.name == pair_name:
                return runner
        raise AssertionError(f"no runner was ever built for {pair_name!r}")

    def all_for(self, pair_name: str) -> list[RecordingRunner]:
        return [runner for runner in self.built if runner.pair.name == pair_name]


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """A migrated temp-file SQLite database -- the same helper every configstore suite uses."""
    url = f"sqlite:///{tmp_path / 'config.db'}"
    upgrade_to_head(url)
    return url


@pytest.fixture
def registry() -> ConnectorRegistry:
    return ConnectorRegistry({"databricks": _SourceConnector, "qlik": _TargetConnector}, {})


@pytest.fixture
def config_service(db_url: str, registry: ConnectorRegistry) -> Iterator[ConfigService]:
    yield ConfigService.from_url(db_url, registry)


@pytest.fixture
def source(config_service: ConfigService) -> CountingSource:
    return CountingSource(ConfigStorePairSource(config_service))


@pytest.fixture
def factory() -> Factory:
    return Factory()


@pytest.fixture
def health() -> HealthRegistry:
    return HealthRegistry()


@pytest.fixture
async def scheduler(
    source: CountingSource, factory: Factory, health: HealthRegistry
) -> AsyncIterator[SyncScheduler]:
    """A started scheduler with no environment-declared pairs at all.

    Empty ``runners`` is the point: every pair these tests schedule arrives from the store
    after the process is already up, which is exactly C1's "without a restart".
    """
    built = SyncScheduler(
        runners=[],
        health=health,
        config_source=source,
        runner_factory=factory,
        run_immediately=False,
    )
    built.start()
    defer_reconcile_job(built)
    try:
        yield built
    finally:
        await built.shutdown(timeout=1.0)


# --------------------------------------------------------------------------------------
# Small builders over the real ConfigService, and scheduler-driving helpers
# --------------------------------------------------------------------------------------


async def make_endpoints(service: ConfigService, *, enabled: bool = True) -> None:
    await service.create_endpoint(
        name="dbx",
        connector="databricks",
        role=EndpointRole.SOURCE,
        settings={"host": "https://dbx.example"},
        enabled=enabled,
        actor=ACTOR,
        now=NOW,
    )
    await service.create_endpoint(
        name="qlik-acme",
        connector="qlik",
        role=EndpointRole.TARGET,
        settings={"space_id": "space-1"},
        secret_ref="env:QLIK_ACME",
        enabled=enabled,
        actor=ACTOR,
        now=NOW,
    )


async def make_pair(
    service: ConfigService,
    *,
    name: str = "pair-a",
    cadence_seconds: int = 600,
    enabled: bool = True,
    pattern: str | None = "analytics.*",
) -> uuid.UUID:
    row = await service.create_sync_pair(
        name=name,
        source="dbx",
        target="qlik-acme",
        target_space="Sales",
        entity_types=[EntityType.DATA_PRODUCT],
        cadence_seconds=cadence_seconds,
        enabled=enabled,
        actor=ACTOR,
        now=NOW,
    )
    if pattern is not None:
        await service.create_selection_rule(
            pair_id=row.id,
            scope=RuleScope.OBJECT,
            decision=SelectionDecision.INCLUDE,
            matcher_kind=MatcherKind.GLOB,
            pattern=pattern,
            actor=ACTOR,
            now=NOW,
        )
    return row.id


def defer_reconcile_job(scheduler: SyncScheduler) -> None:
    """Push the reconcile timer an hour out so a test can drive reconcile deterministically.

    ``Job.modify`` is the same real, public apscheduler API ``tests/scheduler`` drives jobs
    with; the job, its trigger and its interval are otherwise untouched.
    """
    job = scheduler.scheduler.get_job(RECONCILE_JOB_ID)
    assert job is not None
    job.modify(next_run_time=datetime.now(tz=UTC) + timedelta(hours=1))


def fire_now(job: Job) -> None:
    """Force ``job`` due right now, regardless of its real cadence."""
    job.modify(next_run_time=datetime.now(tz=UTC) - timedelta(milliseconds=1))


async def pump(scheduler: SyncScheduler) -> None:
    """One round of apscheduler's due-job processing, plus an event-loop tick."""
    scheduler.scheduler.wakeup()
    await asyncio.sleep(0)


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def fire_and_finish(
    scheduler: SyncScheduler, runner: RecordingRunner, *, timeout: float = 2.0
) -> None:
    """Force one complete cycle of ``runner``'s pair and wait for it to finish.

    Retries the fire while the pair is idle rather than assuming one wakeup is enough: the
    executor frees a job's instance slot from a done-callback, which can land a tick after the
    cycle's own coroutine returned.
    """
    before = len(runner.finished)

    async def _drive() -> None:
        while len(runner.finished) <= before:
            if not scheduler.is_running(runner.pair.name):
                job = scheduler.scheduler.get_job(runner.pair.name)
                assert job is not None, f"{runner.pair.name!r} has no job to fire"
                fire_now(job)
                await pump(scheduler)
            await asyncio.sleep(0)

    await asyncio.wait_for(_drive(), timeout=timeout)


# ======================================================================================
# Adding, changing and removing pairs without a restart
# ======================================================================================


async def test_a_pair_added_through_the_config_service_starts_firing_without_a_restart(
    scheduler: SyncScheduler, config_service: ConfigService, factory: Factory
) -> None:
    """C1's headline claim, and T12.9's first DoD sentence."""
    assert scheduler.scheduled_pairs == ()

    await make_endpoints(config_service)
    await make_pair(config_service)

    result = await scheduler.reconcile()

    assert result.added == ("pair-a",)
    assert scheduler.scheduled_pairs == ("pair-a",)
    runner = factory.latest("pair-a")
    await fire_and_finish(scheduler, runner)
    assert runner.finished == [EntityType.DATA_PRODUCT]


async def test_the_scheduled_reconcile_job_picks_up_a_new_pair_on_its_own(
    source: CountingSource, factory: Factory, config_service: ConfigService
) -> None:
    """Nobody calls ``reconcile`` here: the timer does, exactly as it does in a live service."""
    built = SyncScheduler(runners=[], config_source=source, runner_factory=factory)
    built.start()
    try:
        await make_endpoints(config_service)
        await make_pair(config_service)

        job = built.scheduler.get_job(RECONCILE_JOB_ID)
        assert job is not None
        fire_now(job)
        await pump(built)
        await wait_until(lambda: built.scheduled_pairs == ("pair-a",))

        runner = factory.latest("pair-a")
        await fire_and_finish(built, runner)
        assert runner.finished == [EntityType.DATA_PRODUCT]
    finally:
        await built.shutdown(timeout=1.0)


async def test_a_changed_cadence_takes_effect_on_the_next_fire(
    scheduler: SyncScheduler, config_service: ConfigService
) -> None:
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service, cadence_seconds=600)
    await scheduler.reconcile()
    job = scheduler.scheduler.get_job("pair-a")
    assert job is not None
    assert job.trigger.interval == timedelta(seconds=600)

    await config_service.update_sync_pair(pair_id, cadence_seconds=120, actor=ACTOR, now=NOW)
    result = await scheduler.reconcile()

    assert result.updated == ("pair-a",)
    rescheduled = scheduler.scheduler.get_job("pair-a")
    assert rescheduled is not None
    assert rescheduled.trigger.interval == timedelta(seconds=120)


async def test_a_rule_change_decides_the_next_cycles_scope(
    scheduler: SyncScheduler, config_service: ConfigService, factory: Factory
) -> None:
    """C1 meeting C3/C4: the *new rules*, not merely a new runner, decide the next cycle.

    Fails if a reconcile rebuilt the runner but left it deciding by the old rule set.
    """
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service, pattern="analytics.*")
    await scheduler.reconcile()

    first = factory.latest("pair-a")
    await fire_and_finish(scheduler, first)
    assert first.decided == [(True, False)]  # analytics in, finance out

    rules = await config_service.list_selection_rules(pair_id, RuleScope.OBJECT)
    await config_service.update_selection_rule(
        rules[0].id, pattern="finance.*", actor=ACTOR, now=NOW
    )
    await scheduler.reconcile()

    second = factory.latest("pair-a")
    assert second is not first
    await fire_and_finish(scheduler, second)
    assert second.decided == [(False, True)]  # finance in, analytics out


async def test_a_pair_deleted_through_the_console_stops_firing(
    scheduler: SyncScheduler, config_service: ConfigService, factory: Factory
) -> None:
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service)
    await scheduler.reconcile()
    runner = factory.latest("pair-a")
    await fire_and_finish(scheduler, runner)

    await config_service.delete_sync_pair(pair_id, actor=ACTOR, now=NOW)
    result = await scheduler.reconcile()

    assert result.removed == ("pair-a",)
    assert scheduler.scheduled_pairs == ()
    assert scheduler.scheduler.get_job("pair-a") is None
    # Nothing can fire it again: there is no job left to force due.
    await pump(scheduler)
    assert runner.calls == [EntityType.DATA_PRODUCT]


# ======================================================================================
# Disabled means not scheduled
# ======================================================================================


async def test_a_disabled_pair_is_not_scheduled(
    scheduler: SyncScheduler, config_service: ConfigService, factory: Factory
) -> None:
    """``sync_pairs.enabled`` defaults to False; a disabled pair must never fire."""
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service, enabled=False)

    result = await scheduler.reconcile()

    assert result.added == ()
    assert scheduler.scheduled_pairs == ()
    assert factory.built == []

    await config_service.update_sync_pair(pair_id, enabled=True, actor=ACTOR, now=NOW)
    assert (await scheduler.reconcile()).added == ("pair-a",)
    assert scheduler.scheduled_pairs == ("pair-a",)

    await config_service.update_sync_pair(pair_id, enabled=False, actor=ACTOR, now=NOW)
    assert (await scheduler.reconcile()).removed == ("pair-a",)
    assert scheduler.scheduled_pairs == ()


async def test_disabling_an_endpoint_stops_its_pairs(
    scheduler: SyncScheduler, config_service: ConfigService
) -> None:
    """``endpoints.enabled`` defaults to False too, and a pair is only as enabled as its chain."""
    await make_endpoints(config_service)
    await make_pair(config_service)
    await scheduler.reconcile()
    assert scheduler.scheduled_pairs == ("pair-a",)

    await config_service.update_endpoint("qlik-acme", enabled=False, actor=ACTOR, now=NOW)
    result = await scheduler.reconcile()

    assert result.removed == ("pair-a",)
    assert scheduler.scheduled_pairs == ()


async def test_a_pair_whose_endpoints_were_never_enabled_never_schedules(
    scheduler: SyncScheduler, config_service: ConfigService
) -> None:
    await make_endpoints(config_service, enabled=False)
    await make_pair(config_service, enabled=True)

    assert (await scheduler.reconcile()).added == ()
    assert scheduler.scheduled_pairs == ()


# ======================================================================================
# Cheap when nothing changed
# ======================================================================================


async def test_an_unchanged_generation_reads_one_number_and_nothing_else(
    scheduler: SyncScheduler,
    config_service: ConfigService,
    source: CountingSource,
    factory: Factory,
) -> None:
    """Fails if reconcile reloaded the configuration, or rebuilt a runner, for no change."""
    await make_endpoints(config_service)
    await make_pair(config_service)
    await scheduler.reconcile()

    loads_after_first = source.loads
    builds_after_first = len(factory.built)
    generations_after_first = source.generations

    for _ in range(5):
        result = await scheduler.reconcile()
        assert result.reloaded is False
        assert result.changed is False

    assert source.loads == loads_after_first, "reconcile reloaded on an unchanged generation"
    assert len(factory.built) == builds_after_first, "reconcile rebuilt on an unchanged generation"
    assert source.generations == generations_after_first + 5, "the cheap probe must still run"


async def test_editing_one_pair_does_not_rebuild_another(
    scheduler: SyncScheduler, config_service: ConfigService, factory: Factory
) -> None:
    """The generation is global; a rebuild is not. Editing A must not churn B's runner."""
    await make_endpoints(config_service)
    await make_pair(config_service, name="pair-a")
    pair_b = await make_pair(config_service, name="pair-b")
    await scheduler.reconcile()
    assert len(factory.all_for("pair-a")) == 1
    assert len(factory.all_for("pair-b")) == 1

    await config_service.update_sync_pair(pair_b, cadence_seconds=42, actor=ACTOR, now=NOW)
    result = await scheduler.reconcile()

    assert result.updated == ("pair-b",)
    assert len(factory.all_for("pair-a")) == 1, "pair-a was rebuilt by an edit to pair-b"
    assert len(factory.all_for("pair-b")) == 2


# ======================================================================================
# A cycle already in flight keeps the configuration it started with
# ======================================================================================


async def test_a_cycle_in_flight_keeps_the_rule_set_it_started_with(
    scheduler: SyncScheduler, config_service: ConfigService, factory: Factory
) -> None:
    """C1's safety sentence, asserted from inside the running cycle.

    The blocked cycle evaluates its rule set only *after* the reconcile has landed, so this
    fails if a reconcile mutated the live runner's rules or swapped what the running cycle
    uses. It also fails if the reconcile cancelled the cycle.
    """
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service, pattern="analytics.*")
    await scheduler.reconcile()
    running = factory.latest("pair-a")
    running.block = True

    job = scheduler.scheduler.get_job("pair-a")
    assert job is not None
    fire_now(job)
    await pump(scheduler)
    await asyncio.wait_for(running.started.wait(), timeout=2.0)
    assert scheduler.is_running("pair-a")

    rules = await config_service.list_selection_rules(pair_id, RuleScope.OBJECT)
    await config_service.update_selection_rule(
        rules[0].id, pattern="finance.*", actor=ACTOR, now=NOW
    )
    result = await scheduler.reconcile()
    assert result.updated == ("pair-a",)

    # The mid-flight cycle now runs to completion and reports what *it* decided.
    running.release.set()
    await wait_until(lambda: bool(running.finished))
    assert running.cancelled is False
    assert running.decided == [(True, False)], "a cycle in flight saw a mid-flight rule change"

    # ...and the new rule set decides the next fire.
    rebuilt = factory.latest("pair-a")
    assert rebuilt is not running
    await fire_and_finish(scheduler, rebuilt)
    assert rebuilt.decided == [(False, True)]


async def test_a_swapped_job_still_cannot_overlap_the_cycle_it_replaced(
    scheduler: SyncScheduler, config_service: ConfigService, factory: Factory
) -> None:
    """``max_instances=1`` per pair is unchanged across a reconcile.

    Counting invocations is the only way to tell "skipped" from "ran concurrently" from the
    outside; this fails if the replacement job started a second cycle for the same pair while
    the first was still in flight.
    """
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service)
    await scheduler.reconcile()
    running = factory.latest("pair-a")
    running.block = True

    job = scheduler.scheduler.get_job("pair-a")
    assert job is not None
    fire_now(job)
    await pump(scheduler)
    await asyncio.wait_for(running.started.wait(), timeout=2.0)

    await config_service.update_sync_pair(pair_id, cadence_seconds=30, actor=ACTOR, now=NOW)
    await scheduler.reconcile()
    rebuilt = factory.latest("pair-a")
    assert rebuilt is not running

    # Exactly one job for the pair, and forcing it due while the old cycle runs is skipped.
    assert pair_job_ids(scheduler) == ["pair-a"]
    swapped = scheduler.scheduler.get_job("pair-a")
    assert swapped is not None
    for _ in range(3):
        fire_now(swapped)
        await pump(scheduler)
    assert rebuilt.calls == [], "a reconcile-created job overlapped a cycle already running"

    running.release.set()
    await wait_until(lambda: bool(running.finished))
    # Once the first cycle is done, the replacement job fires normally.
    await fire_and_finish(scheduler, rebuilt)
    assert rebuilt.finished == [EntityType.DATA_PRODUCT]


async def test_reconciling_repeatedly_never_creates_a_second_job_for_a_pair(
    scheduler: SyncScheduler, config_service: ConfigService
) -> None:
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service)
    for cadence in (100, 200, 300):
        await config_service.update_sync_pair(
            pair_id, cadence_seconds=cadence, actor=ACTOR, now=NOW
        )
        await scheduler.reconcile()
    assert pair_job_ids(scheduler) == ["pair-a"]


async def test_removing_a_pair_mid_cycle_does_not_abandon_the_cycle(
    scheduler: SyncScheduler, config_service: ConfigService, factory: Factory
) -> None:
    """A console delete is a configuration change, not an emergency stop.

    The job goes at once (it can never fire again), but the cycle already running -- whose
    writes have already spent Qlik write-tier budget -- is left to finish and commit, exactly
    as the shutdown path deliberately does. Fails if the cycle was cancelled, or if the
    scheduler lost track of it.
    """
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service)
    await scheduler.reconcile()
    running = factory.latest("pair-a")
    running.block = True

    job = scheduler.scheduler.get_job("pair-a")
    assert job is not None
    fire_now(job)
    await pump(scheduler)
    await asyncio.wait_for(running.started.wait(), timeout=2.0)

    await config_service.delete_sync_pair(pair_id, actor=ACTOR, now=NOW)
    result = await scheduler.reconcile()

    assert result.removed == ("pair-a",)
    assert scheduler.scheduler.get_job("pair-a") is None
    assert scheduler.scheduled_pairs == ()
    # Still tracked while it is genuinely still running.
    assert scheduler.is_running("pair-a") is True
    assert "pair-a" in scheduler.pairs

    running.release.set()
    await wait_until(lambda: bool(running.finished))
    assert running.cancelled is False
    assert running.finished == [EntityType.DATA_PRODUCT]
    await wait_until(lambda: not scheduler.is_running("pair-a"))
    assert scheduler.pairs == ()


# ======================================================================================
# A broken configuration must not take the scheduler down
# ======================================================================================


async def test_one_broken_pair_does_not_stop_the_others(
    scheduler: SyncScheduler,
    config_service: ConfigService,
    factory: Factory,
    health: HealthRegistry,
) -> None:
    """A runner that will not build -- a vanished connector, credentials that no longer
    resolve -- is reported and retried, and every other pair reconciles regardless."""
    await make_endpoints(config_service)
    await make_pair(config_service, name="pair-ok")
    await make_pair(config_service, name="pair-broken")
    factory.fail_for.add("pair-broken")

    result = await scheduler.reconcile()

    assert result.added == ("pair-ok",)
    assert result.failed == ("pair-broken",)
    assert scheduler.scheduled_pairs == ("pair-ok",)
    # The scheduler is still alive and the healthy pair still syncs.
    await fire_and_finish(scheduler, factory.latest("pair-ok"))
    # ...and the problem is surfaced rather than swallowed.
    snapshot = health.snapshot()
    assert snapshot["status"] == "degraded"
    assert snapshot["components"]["pair-broken"]["healthy"] is False
    assert "endpoint unusable" in str(snapshot["components"]["pair-broken"]["detail"])


async def test_a_pair_that_starts_building_again_is_picked_up(
    scheduler: SyncScheduler, config_service: ConfigService, factory: Factory
) -> None:
    """A broken pair is retried, and a configuration write cancels the retry backoff."""
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service, name="pair-a")
    factory.fail_for.add("pair-a")
    assert (await scheduler.reconcile()).failed == ("pair-a",)
    assert scheduler.scheduled_pairs == ()

    factory.fail_for.clear()
    # A write bumps the generation, which clears the backoff: the operator's fix is tried now.
    await config_service.update_sync_pair(pair_id, cadence_seconds=300, actor=ACTOR, now=NOW)
    result = await scheduler.reconcile()

    assert result.added == ("pair-a",)
    assert scheduler.scheduled_pairs == ("pair-a",)


async def test_an_unreadable_pair_keeps_the_job_it_already_had(
    scheduler: SyncScheduler, config_service: ConfigService, factory: Factory
) -> None:
    """A bad edit must not unschedule a pair that was syncing fine a moment ago.

    An enabled pair with no entity types cannot become a ``SyncPairConfig`` at all. It is
    reported as unreadable and its existing job is left exactly where it was -- a different,
    and much better, outcome than treating "will not translate" as "was deleted".
    """
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service)
    await scheduler.reconcile()
    runner = factory.latest("pair-a")

    await config_service.update_sync_pair(pair_id, entity_types=[], actor=ACTOR, now=NOW)
    result = await scheduler.reconcile()

    assert result.unreadable == ("pair-a",)
    assert result.removed == ()
    assert scheduler.scheduled_pairs == ("pair-a",)
    # The old, still-valid configuration keeps running.
    await fire_and_finish(scheduler, runner)
    assert runner.finished == [EntityType.DATA_PRODUCT]


async def test_a_store_that_cannot_be_read_leaves_every_pair_running(
    scheduler: SyncScheduler,
    config_service: ConfigService,
    source: CountingSource,
    factory: Factory,
) -> None:
    """The scheduled reconcile swallows a store failure; the job set is untouched."""
    await make_endpoints(config_service)
    await make_pair(config_service)
    await scheduler.reconcile()
    runner = factory.latest("pair-a")

    source.generation_error = RuntimeError("state store unreachable")

    # Awaited directly, the caller is told.
    with pytest.raises(RuntimeError, match="state store unreachable"):
        await scheduler.reconcile()

    # Driven by its own job, it is logged and survived: the reconcile job is still scheduled
    # and every pair still fires.
    reconcile_job = scheduler.scheduler.get_job(RECONCILE_JOB_ID)
    assert reconcile_job is not None
    fire_now(reconcile_job)
    await pump(scheduler)
    await asyncio.sleep(0)

    assert scheduler.scheduled_pairs == ("pair-a",)
    assert scheduler.scheduler.get_job(RECONCILE_JOB_ID) is not None
    await fire_and_finish(scheduler, runner)
    assert runner.finished == [EntityType.DATA_PRODUCT]


# ======================================================================================
# Wiring, defaults and refusals
# ======================================================================================


def pair_job_ids(scheduler: SyncScheduler) -> list[str]:
    """Every registered job that is a sync pair -- i.e. everything but the reconcile job."""
    return [job.id for job in scheduler.scheduler.get_jobs() if job.id != RECONCILE_JOB_ID]


def make_fixed_runner(name: str = "fixed") -> RecordingRunner:
    """A runner for a pair declared the RM-01 way: process configuration, no store."""
    pair = SyncPairConfig(
        name=name,
        source="dbx",
        target="qlik-acme",
        catalog_schema_patterns=["sales.*"],
        target_space="Sales",
        entity_types=[EntityType.DATA_PRODUCT],
    )
    return RecordingRunner(
        PairPlan(pair=pair, selection_rules=SelectionRuleSet.build(), fingerprint="fixed")
    )


async def test_reconcile_is_registered_as_an_ordinary_job(scheduler: SyncScheduler) -> None:
    job = scheduler.scheduler.get_job(RECONCILE_JOB_ID)
    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.trigger.interval == timedelta(seconds=scheduler.reconcile_interval_seconds)


async def test_a_scheduler_without_a_config_source_is_unchanged_by_this_task() -> None:
    """Reconcile is optional, exactly as run history is: no store, no reconcile job, no change."""
    built = SyncScheduler(runners=[make_fixed_runner()])
    built.start()
    try:
        assert built.scheduler.get_job(RECONCILE_JOB_ID) is None
        assert pair_job_ids(built) == ["fixed"]
        assert built.generation is None
        with pytest.raises(RuntimeError, match="fixed pair set"):
            await built.reconcile()
    finally:
        await built.shutdown(timeout=1.0)


def test_no_runners_and_no_config_source_is_still_rejected() -> None:
    """The existing guarantee is untouched: a scheduler with nothing to do is a mistake."""
    with pytest.raises(ValueError, match="at least one"):
        SyncScheduler(runners=[])


def test_a_config_source_without_a_runner_factory_is_refused(source: CountingSource) -> None:
    with pytest.raises(ValueError, match="must be supplied together"):
        SyncScheduler(runners=[], config_source=source)


def test_the_reconcile_job_id_is_reserved(source: CountingSource, factory: Factory) -> None:
    with pytest.raises(ValueError, match="reserved job id"):
        SyncScheduler(
            runners=[make_fixed_runner(RECONCILE_JOB_ID)],
            config_source=source,
            runner_factory=factory,
        )


def test_a_non_positive_reconcile_interval_is_refused(
    source: CountingSource, factory: Factory
) -> None:
    with pytest.raises(ValueError, match="reconcile_interval_seconds"):
        SyncScheduler(
            runners=[],
            config_source=source,
            runner_factory=factory,
            reconcile_interval_seconds=0,
        )


async def test_the_store_is_authoritative_over_the_pairs_a_process_started_with(
    config_service: ConfigService, source: CountingSource, factory: Factory
) -> None:
    """A restart adopts every pair from the store, and drops ones the store no longer has.

    ``serve`` builds its initial runners from the process's config file, but once reconcile is
    on the database is the source of truth (C1) -- the file may be months stale. So the first
    pass adopts from the store rather than assuming the two already agree.
    """
    await make_endpoints(config_service)
    await make_pair(config_service, name="from-store")

    built = SyncScheduler(
        runners=[make_fixed_runner("from-yaml")],
        config_source=source,
        runner_factory=factory,
    )
    built.start()
    defer_reconcile_job(built)
    try:
        result = await built.reconcile()
        assert result.added == ("from-store",)
        assert result.removed == ("from-yaml",)
        assert built.scheduled_pairs == ("from-store",)
    finally:
        await built.shutdown(timeout=1.0)


async def test_a_pairs_jitter_override_is_honoured(
    scheduler: SyncScheduler, config_service: ConfigService
) -> None:
    """``sync_pairs.jitter_seconds`` exists to override the computed window; NULL keeps it."""
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service, cadence_seconds=600)
    await scheduler.reconcile()
    job = scheduler.scheduler.get_job("pair-a")
    assert job is not None
    assert job.trigger.jitter == 60.0  # min(600 * 0.10, 60)

    await config_service.update_sync_pair(pair_id, jitter_seconds=3.0, actor=ACTOR, now=NOW)
    await scheduler.reconcile()
    overridden = scheduler.scheduler.get_job("pair-a")
    assert overridden is not None
    assert overridden.trigger.jitter == 3.0


async def test_a_reconcile_landing_during_shutdown_installs_nothing(
    scheduler: SyncScheduler, config_service: ConfigService, factory: Factory
) -> None:
    """Shutdown pauses first, so no *new* reconcile is ever processed -- but one already
    dispatched keeps running for a moment, and it must not install a job into a scheduler
    that is going down."""
    await make_endpoints(config_service)
    await make_pair(config_service)

    await scheduler.shutdown(timeout=1.0)
    result = await scheduler.reconcile()

    assert result.added == ()
    assert scheduler.scheduled_pairs == ()
    assert factory.built == []


# ======================================================================================
# Row -> plan translation
# ======================================================================================


async def test_a_plan_carries_the_stored_pair_and_its_compiled_rules(
    config_service: ConfigService,
) -> None:
    await make_endpoints(config_service)
    await make_pair(config_service, cadence_seconds=123, pattern="analytics.*")

    snapshot = await ConfigStorePairSource(config_service).load()

    assert len(snapshot.plans) == 1
    plan = snapshot.plans[0]
    assert plan.pair.name == "pair-a"
    assert plan.pair.cadence_seconds == 123
    assert plan.pair.target_space == "Sales"
    assert plan.pair.entity_types == [EntityType.DATA_PRODUCT]
    # The D1 projection is the object-scope include globs, in order.
    assert plan.pair.catalog_schema_patterns == ["analytics.*"]
    assert evaluate(plan.selection_rules, ANALYTICS).included is True
    assert evaluate(plan.selection_rules, FINANCE).included is False


async def test_a_pair_with_no_glob_include_rule_projects_to_an_inert_pattern(
    config_service: ConfigService,
) -> None:
    """The label falls closed, never open: no include glob means the rule set selects nothing."""
    await make_endpoints(config_service)
    await make_pair(config_service, pattern=None)

    snapshot = await ConfigStorePairSource(config_service).load()

    plan = snapshot.plans[0]
    assert plan.pair.catalog_schema_patterns == [INERT_CATALOG_SCHEMA_PATTERN]
    assert evaluate(plan.selection_rules, ANALYTICS).included is False


async def test_a_per_object_override_reaches_the_plans_rule_set(
    config_service: ConfigService,
) -> None:
    """C3's overrides are loaded alongside the rules, not left behind by the scheduler."""
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service, pattern="analytics.*")
    await config_service.create_selection_override(
        pair_id=pair_id,
        scope=RuleScope.OBJECT,
        object_id="analytics.sales",
        decision=SelectionDecision.EXCLUDE,
        reason="pinned out by the operator",
        actor=ACTOR,
        now=NOW,
    )

    snapshot = await ConfigStorePairSource(config_service).load()

    assert evaluate(snapshot.plans[0].selection_rules, ANALYTICS).included is False


async def test_restrict_to_keeps_a_narrowed_process_narrow(
    config_service: ConfigService,
) -> None:
    """``serve --pair`` must not have every other pair reappear the moment reconcile runs."""
    await make_endpoints(config_service)
    await make_pair(config_service, name="pair-a")
    await make_pair(config_service, name="pair-b")

    snapshot = await ConfigStorePairSource(config_service, restrict_to={"pair-a"}).load()

    assert [plan.pair.name for plan in snapshot.plans] == ["pair-a"]


async def test_an_enabled_pair_that_will_not_translate_is_a_failure_not_an_absence(
    config_service: ConfigService,
) -> None:
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service)
    await config_service.update_sync_pair(pair_id, entity_types=[], actor=ACTOR, now=NOW)

    snapshot = await ConfigStorePairSource(config_service).load()

    assert snapshot.plans == ()
    assert [failure.pair for failure in snapshot.failures] == ["pair-a"]
    assert "entity_types" in snapshot.failures[0].reason


async def test_a_disabled_pair_is_never_even_translated(
    config_service: ConfigService,
) -> None:
    """C6's half-registered pair is absent, not broken -- it must not degrade anything."""
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service, enabled=False)
    await config_service.update_sync_pair(pair_id, entity_types=[], actor=ACTOR, now=NOW)

    snapshot = await ConfigStorePairSource(config_service).load()

    assert snapshot.plans == ()
    assert snapshot.failures == ()


async def test_the_fingerprint_covers_the_endpoints_a_pair_is_built_from(
    config_service: ConfigService,
) -> None:
    """Change detection is over meaning: a write that changed nothing must not churn a runner,
    and an endpoint edit must, because a runner is built from that endpoint."""
    await make_endpoints(config_service)
    pair_id = await make_pair(config_service)
    source = ConfigStorePairSource(config_service)
    first = (await source.load()).plans[0].fingerprint

    # A no-op write does not change the plan.
    await config_service.update_sync_pair(
        pair_id, cadence_seconds=600, actor=ACTOR, now=NOW + timedelta(hours=1)
    )
    assert (await source.load()).plans[0].fingerprint == first

    # An endpoint's settings are part of what a runner is built from.
    await config_service.update_endpoint(
        "dbx", settings={"host": "https://other.example"}, actor=ACTOR, now=NOW
    )
    assert (await source.load()).plans[0].fingerprint != first
