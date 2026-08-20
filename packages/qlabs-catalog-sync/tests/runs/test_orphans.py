"""Decision D4: orphans are recorded consistently with ``orphan_log`` -- and the two
cannot disagree, because ``run_items`` never duplicates ``orphan_log``'s own fields.

``orphan_log`` (T2.2) is already the authoritative record of a vanished source object --
first/last seen missing, whether it has since been resolved. A ``run_items`` row for an
``ORPHANED`` outcome carries only what identifies that ``orphan_log`` row
(``neutral_id``, ``endpoint``, plus the run's own ``entity_type``) — never a copy of its
resolution state. This is what makes disagreement structurally impossible rather than
merely untested: there is only one place ``resolved_at`` is ever written.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from run_history_helpers import seed_product

from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import RecordOutcome, SyncLoop
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector

STARTED_AT = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


async def _cycle(recorder: RunRecorder, loop: SyncLoop, *, started_at: datetime) -> object:
    run_id = await recorder.start(
        pair=loop.pair.name,
        source_endpoint=loop.source_endpoint,
        target_endpoint=loop.target_endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        dry_run=False,
        started_at=started_at,
    )
    report = await loop.run_cycle(EntityType.DATA_PRODUCT)
    await recorder.finish(run_id, report)
    return run_id


@pytest.mark.asyncio
async def test_an_orphan_soft_references_the_authoritative_orphan_log_row(
    recorder: RunRecorder,
    store: StateStore,
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
) -> None:
    ref = seed_product(source, "sales.orders")
    loop = make_loop(create_missing=True)
    await _cycle(recorder, loop, started_at=STARTED_AT)  # cycle 1: creates and binds it

    source.vanish(ref)
    orphan_run_id = await _cycle(recorder, loop, started_at=STARTED_AT + timedelta(minutes=15))

    items = await recorder.list_items(orphan_run_id)
    assert len(items) == 1
    item = items[0]
    assert item.outcome is RecordOutcome.ORPHANED
    assert item.neutral_id is not None
    assert item.endpoint == "fake-source"

    # The join a console's "this run's orphans" view would perform: run_items identifies
    # the orphan_log row, orphan_log is where its resolution state actually lives.
    open_orphans = await store.list_orphans("fake-source", unresolved_only=True)
    matching = [
        o for o in open_orphans if o.neutral_id == item.neutral_id and o.endpoint == item.endpoint
    ]
    assert len(matching) == 1
    assert matching[0].resolved_at is None
    assert matching[0].entity_type is EntityType.DATA_PRODUCT


@pytest.mark.asyncio
async def test_a_resolved_orphan_is_current_only_in_orphan_log_not_in_the_old_run_item(
    recorder: RunRecorder,
    store: StateStore,
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
) -> None:
    """The two "cannot disagree" precisely because run_items never claims a resolution
    state to begin with -- proven here by resolving the orphan and checking that the
    *only* place that becomes visible is orphan_log."""
    ref = seed_product(source, "sales.orders")
    loop = make_loop(create_missing=True)
    await _cycle(recorder, loop, started_at=STARTED_AT)

    source.vanish(ref)
    orphan_run_id = await _cycle(recorder, loop, started_at=STARTED_AT + timedelta(minutes=15))
    orphan_item = (await recorder.list_items(orphan_run_id))[0]

    # The object reappears at the source (a schema recreated, a transient glitch cleared).
    seed_product(source, "sales.orders")
    await _cycle(recorder, loop, started_at=STARTED_AT + timedelta(minutes=30))

    resolved = await store.list_orphans("fake-source", unresolved_only=False)
    matching = [o for o in resolved if o.neutral_id == orphan_item.neutral_id]
    assert len(matching) == 1
    assert matching[0].resolved_at is not None

    # The historical run_items row is untouched -- it correctly still says "orphaned",
    # because that is what this run observed at the time. It carries no resolved_at
    # column to go stale: there is nothing here that could contradict orphan_log.
    replayed = await recorder.list_items(orphan_run_id)
    assert replayed[0].outcome is RecordOutcome.ORPHANED
    assert not hasattr(replayed[0], "resolved_at")

    still_open = await store.list_orphans("fake-source", unresolved_only=True)
    assert all(o.neutral_id != orphan_item.neutral_id for o in still_open)
