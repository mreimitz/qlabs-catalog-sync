"""Decision D4 (never delete) and the reaction to each typed SDK exception."""

from __future__ import annotations

import inspect
from collections.abc import Callable

from sync_helpers import bind, data_product, seed_product, write_calls

from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync import loop as loop_module
from qlabs_catalog_sync.sync.loop import RecordOutcome, RunStatus, SkipReason, SyncLoop
from qlabs_catalog_sync_sdk.exceptions import (
    AuthError,
    CapabilityError,
    NotFound,
    TransientError,
)
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector

# ---------------------------------------------------------------------------------------
# D4: never delete
# ---------------------------------------------------------------------------------------


async def test_a_vanished_source_object_becomes_an_orphan_and_is_never_deleted(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """Decision D4, end to end: recorded in the orphan log, surfaced, and nothing removed."""
    ref = seed_product(source, "sales.orders")
    loop = make_loop(create_missing=True)
    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    neutral_id = first.records[0].neutral_id
    assert neutral_id is not None

    source.vanish(ref)
    target.reset_call_log()

    report = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert [record.outcome for record in report.records] == [RecordOutcome.ORPHANED]
    assert [orphan.native_key for orphan in report.orphans] == ["sales.orders"]
    assert report.orphans[0].endpoint == source.name

    stored = await store.list_orphans(source.name, unresolved_only=True)
    assert [record.neutral_id for record in stored] == [neutral_id]

    # Nothing was removed anywhere, and the target was not touched at all.
    assert write_calls(target) == []
    assert target.call_count("delete") == 0


async def test_an_orphan_that_comes_back_is_resolved(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """A reappearing object stops being reported as missing."""
    ref = seed_product(source, "sales.orders", description="Order facts")
    loop = make_loop(create_missing=True)
    await loop.run_cycle(EntityType.DATA_PRODUCT)
    source.vanish(ref)
    await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert await store.list_orphans(source.name, unresolved_only=True)

    seed_product(source, "sales.orders", description="Order facts")  # it is back
    await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert await store.list_orphans(source.name, unresolved_only=True) == []


async def test_an_object_gone_between_listing_and_read_is_an_orphan_too(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """``NotFound`` on ``read`` means the same thing as a reported deletion: it is gone."""
    ref = seed_product(source, "sales.orders")
    loop = make_loop(create_missing=True)
    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    neutral_id = first.records[0].neutral_id

    # A change is reported, but the object disappears before the read lands.
    source.simulate_external_edit(ref, {"name": "renamed"})
    source.fail_next("read", NotFound("gone", endpoint=source.name, native_key="sales.orders"))
    target.reset_call_log()

    report = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert [record.outcome for record in report.records] == [RecordOutcome.ORPHANED]
    assert [orphan.neutral_id for orphan in report.orphans] == [neutral_id]
    assert write_calls(target) == []


async def test_a_deletion_for_an_object_never_bound_here_is_simply_reported(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, target: FakeConnector
) -> None:
    """Nothing was ever synced for it, so there is nothing to orphan and nothing outstanding."""
    ref = seed_product(source, "sales.orders")
    source.vanish(ref)

    report = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    reasons = [record.reason for record in report.records if record.reason is not None]
    assert SkipReason.DELETED_UNKNOWN_OBJECT in reasons
    assert report.orphans == ()
    assert target.call_count("delete") == 0


def test_the_loop_module_contains_no_delete_call_at_all() -> None:
    """Decision D4 is structural, not a runtime check: there is no path that could delete."""
    body = inspect.getsource(loop_module)
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith(("#", "*", '"'))
    )
    assert ".delete(" not in code
    assert ".deactivate(" not in code


# ---------------------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------------------


async def test_a_transient_error_is_retried_and_then_gives_up_without_committing(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    store: StateStore,
) -> None:
    """Retried the configured number of times, then the cycle fails rather than skipping on."""
    seed_product(source, "sales.orders")
    for _ in range(3):  # one first attempt plus two retries
        source.fail_next("read", TransientError("flaky", endpoint=source.name))

    report = await make_loop(create_missing=True, retry_attempts=2).run_cycle(
        EntityType.DATA_PRODUCT
    )

    assert report.status is RunStatus.FAILED
    assert report.committed is False
    assert source.call_count("read") == 3
    assert report.errors[0].kind == "TransientError"
    assert report.errors[0].retryable is True


async def test_a_rate_limit_hint_is_counted_and_honored(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, metrics: object
) -> None:
    """``retry_after_seconds`` is the SDK's 429 signal; it drives both the metric and the wait."""
    from qlabs_catalog_sync.observability import METRIC_RATE_LIMITED_TOTAL

    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    seed_product(source, "sales.orders")
    source.fail_next(
        "read", TransientError("429", endpoint=source.name, retry_after_seconds=12.5)
    )

    report = await make_loop(create_missing=True, sleep=record_sleep).run_cycle(
        EntityType.DATA_PRODUCT
    )

    assert report.status is RunStatus.OK
    assert slept == [12.5]
    assert metrics.total(METRIC_RATE_LIMITED_TOTAL) == 1  # type: ignore[attr-defined]


async def test_an_auth_error_quarantines_the_endpoint_and_commits_nothing(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    store: StateStore,
) -> None:
    """Not retried: the same bad credentials would fail the same way. The endpoint is named."""
    from qlabs_catalog_sync.observability import HealthRegistry

    seed_product(source, "sales.orders")
    health = HealthRegistry()
    source.fail_next("list_changed", AuthError("token rejected", endpoint=source.name))

    report = await make_loop(create_missing=True, health=health).run_cycle(
        EntityType.DATA_PRODUCT
    )

    assert report.status is RunStatus.FAILED
    assert report.quarantined_endpoints == (source.name,)
    assert source.call_count("list_changed") == 1  # not retried
    assert health.snapshot()["status"] == "degraded"


async def test_a_conflict_is_re_read_re_diffed_and_retried_once(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """RS-07 step 6: an out-of-band edit is reconciled against fresh target state, then written."""
    source_ref = seed_product(source, "sales.orders", description="Order facts")
    existing = target.seed(data_product("orders", description="stale"), native_key="qlik-orders")
    await bind(store, source_ref, existing)
    loop = make_loop()
    await loop.run_cycle(EntityType.DATA_PRODUCT)

    # Someone edits the Qlik object by hand, invalidating the revision we hold...
    target.simulate_external_edit(
        existing, {"description": {"text": "hand edit", "format": "plain"}}
    )
    # ...and the source changes too, so there is genuinely something to write.
    source.simulate_external_edit(source_ref, {"description": {"text": "v2", "format": "plain"}})
    target.reset_call_log()

    report = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert report.records[0].outcome is RecordOutcome.WRITTEN
    assert target.call_count("update") == 2  # the rejected attempt, then the re-diffed one
    assert target.call_count("read") == 1  # the re-read between them
    final = await target.read(existing)
    assert final.model_dump()["description"]["text"] == "v2"  # source wins, v1's default


async def test_a_capability_refusal_is_skipped_reported_and_never_retried(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """The plan was wrong, so retrying would send the same invalid request again."""
    source_ref = seed_product(source, "sales.orders", description="Order facts")
    existing = target.seed(data_product("orders", description="stale"), native_key="qlik-orders")
    await bind(store, source_ref, existing)
    target.fail_next(
        "update",
        CapabilityError("field is ro", endpoint=target.name, operation="update", field="name"),
    )

    report = await make_loop().run_cycle(EntityType.DATA_PRODUCT)

    record = report.records[0]
    assert record.outcome is RecordOutcome.SKIPPED
    assert record.reason is SkipReason.CAPABILITY_REFUSED
    assert record.holds_watermark is True
    assert target.call_count("update") == 1  # never retried
    assert report.errors[0].kind == "CapabilityError"
    assert report.errors[0].retryable is False
    assert report.status is RunStatus.PARTIAL


async def test_a_stale_target_binding_is_reported_not_repaired(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """``NotFound`` from the target means the binding is stale; v1 neither deletes nor rebinds."""
    source_ref = seed_product(source, "sales.orders", description="Order facts")
    ghost = target.seed(data_product("orders"), native_key="qlik-orders")
    await bind(store, source_ref, ghost)
    target.vanish(ghost)
    target.reset_call_log()

    report = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    record = report.records[0]
    assert record.outcome is RecordOutcome.SKIPPED
    assert record.reason is SkipReason.TARGET_NOT_FOUND
    assert record.holds_watermark is True
    assert target.call_count("create") == 0  # no silent re-creation
    binding = await store.get_binding(
        record.neutral_id, target.name, EntityType.DATA_PRODUCT  # type: ignore[arg-type]
    )
    assert binding is not None  # the stale binding is left for a human to resolve


async def test_an_unhealthy_endpoint_quarantines_the_pair_before_anything_is_read(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, target: FakeConnector
) -> None:
    """Pre-flight: a red endpoint stops the cycle instead of failing halfway through a write."""
    from qlabs_catalog_sync_sdk.contract import HealthStatus

    seed_product(source, "sales.orders")

    class _Unhealthy(type(target)):  # type: ignore[misc]
        async def healthcheck(self) -> HealthStatus:
            return HealthStatus.unhealthy(self.name, "tenant unreachable")

    unhealthy = _Unhealthy(manifest=target.capabilities())
    report = await make_loop(target=unhealthy, create_missing=True).run_cycle(
        EntityType.DATA_PRODUCT
    )

    assert report.status is RunStatus.FAILED
    assert report.quarantined_endpoints == (unhealthy.name,)
    assert source.call_count("list_changed") == 0


async def test_an_unexpected_engine_error_fails_the_cycle_instead_of_the_process(
    make_loop: Callable[..., SyncLoop], source: FakeConnector
) -> None:
    """A bug must surface as a failed cycle, not take down the scheduler running every pair."""
    seed_product(source, "sales.orders")
    source.fail_next("list_changed", RuntimeError("something the engine did not expect"))

    report = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    assert report.status is RunStatus.FAILED
    assert report.committed is False
    assert report.errors[0].kind == "EngineError"
    assert "RuntimeError" in report.errors[0].message
