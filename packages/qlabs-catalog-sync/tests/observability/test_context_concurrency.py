"""Context bound for one pair must never leak into another pair running concurrently.

This is the bug that makes logs untrustworthy: if two sync pairs run as concurrent asyncio
tasks and their bound context bleeds together, every log line becomes ambiguous about which
pair it actually describes. Proven here with real ``asyncio.gather`` concurrency and real
``structlog.testing.capture_logs`` log emission — not by inspecting ``bind_sync_context`` in
isolation.
"""

from __future__ import annotations

import asyncio

import structlog

from qlabs_catalog_sync.observability import (
    REDACTION_TEST_PROCESSORS,
    bind_sync_context,
    get_logger,
)


async def _run_pair(
    name: str, *, barrier: asyncio.Barrier, iterations: int
) -> list[dict[str, object]]:
    """Simulate one pair's cycle: bind context, then interleave with other tasks."""
    seen: list[dict[str, object]] = []
    with bind_sync_context(pair=name, endpoint=f"{name}-endpoint"):
        for _ in range(iterations):
            # The barrier forces every task to be mid-block at the same moments, maximizing
            # the chance a real leak would show up as a wrong `pair`/`endpoint` value.
            await barrier.wait()
            get_logger().info("cycle step")
            seen.append(dict(structlog.contextvars.get_contextvars()))
            await asyncio.sleep(0)
    return seen


async def test_concurrent_pairs_never_see_each_others_bound_context() -> None:
    pair_names = ["db_prod_to_qlik_acme", "db_dev_to_qlik_acme", "db_prod_to_qlik_partner"]
    iterations = 5
    barrier = asyncio.Barrier(len(pair_names))

    with structlog.testing.capture_logs(processors=REDACTION_TEST_PROCESSORS) as entries:
        results = await asyncio.gather(
            *(_run_pair(name, barrier=barrier, iterations=iterations) for name in pair_names)
        )

    # Each task's own contextvars snapshots only ever named its own pair.
    for name, snapshots in zip(pair_names, results, strict=True):
        assert len(snapshots) == iterations
        for snapshot in snapshots:
            assert snapshot["pair"] == name
            assert snapshot["endpoint"] == f"{name}-endpoint"

    # And the actual emitted log lines agree: every "cycle step" line names exactly the pair
    # whose task emitted it, never a neighbor's.
    assert len(entries) == len(pair_names) * iterations
    for entry in entries:
        assert entry["endpoint"] == f"{entry['pair']}-endpoint"

    # After every task has finished, nothing lingers in the (now top-level) context.
    assert structlog.contextvars.get_contextvars() == {}


async def test_a_task_started_after_binding_does_not_inherit_the_parents_context() -> None:
    """Context set in the spawning task must not silently propagate to a plain new task.

    ``asyncio.Task`` copies the *current* context at creation time, so a task created while
    context is bound *does* start with a copy of it (this is normal, expected contextvars
    behavior, not a leak) — what must never happen is that binding inside the *child* task
    changes what the *parent* (or a sibling) sees, which is exactly what the nested-block
    unwind semantics in ``bind_sync_context`` guarantee.
    """

    async def child_rebinds_and_reports() -> dict[str, object]:
        with bind_sync_context(pair="child_pair"):
            return dict(structlog.contextvars.get_contextvars())

    with bind_sync_context(pair="parent_pair"):
        child_snapshot = await asyncio.create_task(child_rebinds_and_reports())
        parent_snapshot_after = dict(structlog.contextvars.get_contextvars())

    assert child_snapshot["pair"] == "child_pair"
    assert parent_snapshot_after["pair"] == "parent_pair"
