"""Decision D4, end to end and made checkable: the engine issues no delete or
deactivate call in any code path this task adds -- on top of the immediate path T2.4
already shipped and already proves in ``tests/sync/test_orphans_and_errors.py``.

The call-log assertions below are only meaningful because ``target.delete()`` is a
real, working operation here (:class:`FakeConnector` mutates an in-memory store, it
does not stub the call out) -- the first test in this file proves that directly, so
"zero delete calls" through the rest of it is evidence of restraint, not of dead code.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from orphans_helpers import bind, data_product, seed_product, write_calls

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync import orphans as orphans_module
from qlabs_catalog_sync.sync.loop import SyncLoop
from qlabs_catalog_sync.sync.orphans import (
    recheck_open_orphans,
    reconcile_bindings,
    summarize_orphans,
)
from qlabs_catalog_sync_sdk.exceptions import NotFound
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def _no_sleep(seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------------------
# Making the guarantee non-vacuous
# ---------------------------------------------------------------------------------------


async def test_delete_is_real_not_dead_code_so_the_assertions_below_mean_something(
    target: FakeConnector,
) -> None:
    """The Qlik connector genuinely implements ``delete()`` (T3.7, for contract
    completeness); this proves the fake used below does too, by actually using it on a
    throwaway object. If it were a stub, "zero delete calls" downstream would prove
    nothing."""
    ref = target.seed(data_product("throwaway"), native_key="qlik-throwaway")

    await target.delete(ref)

    with pytest.raises(NotFound):
        await target.read(ref)


def test_the_orphans_module_contains_no_delete_or_write_call_at_all() -> None:
    """Structural, like ``sync.loop``'s own version of this test: there is no branch in
    this module's source that could reach a write, let alone a delete."""
    body = inspect.getsource(orphans_module)
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith(("#", "*", '"'))
    )
    assert ".delete(" not in code
    assert ".deactivate(" not in code
    assert ".create(" not in code
    assert ".update(" not in code


# ---------------------------------------------------------------------------------------
# The whole lifecycle, in one cycle-spanning scenario
# ---------------------------------------------------------------------------------------


async def test_a_full_cycle_including_the_orphan_path_never_deletes_or_deactivates(
    store: StateStore,
    source: FakeConnector,
    target: FakeConnector,
    pair: SyncPairConfig,
    resolver: IdentityResolver,
    clock: Callable[[], datetime],
) -> None:
    loop = SyncLoop(
        pair=pair,
        source=source,
        target=target,
        store=store,
        resolver=resolver,
        clock=clock,
        sleep=_no_sleep,
        create_missing=True,
    )

    # A normal sync: the object is created at the target.
    ref = seed_product(source, "sales.orders", description="Order facts")
    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    neutral_id = first.records[0].neutral_id
    assert neutral_id is not None
    target_binding = await store.get_binding(neutral_id, target.name, EntityType.DATA_PRODUCT)
    assert target_binding is not None

    # 1. The immediate path (T2.4, already shipped): the object vanishes and
    #    ``run_cycle`` catches it the same cycle, via ``list_changed``'s DELETED change.
    source.vanish(ref)
    second = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert second.orphans and second.orphans[0].neutral_id == neutral_id

    # 2. The lifecycle layer this task adds: an independent recheck, outside any
    #    list_changed page, confirms it is still gone rather than resolving it back on
    #    a false "present".
    recheck = await recheck_open_orphans(
        store, source, entity_type=EntityType.DATA_PRODUCT, now=clock()
    )
    assert [o.neutral_id for o in recheck.still_missing] == [neutral_id]

    # 3. The general reconciliation path -- the one that never consults list_changed at
    #    all -- run directly against the identity binding, the answer to "an object
    #    stopped appearing and nothing raised".
    source_binding = await store.get_binding(neutral_id, source.name, EntityType.DATA_PRODUCT)
    assert source_binding is not None
    reconciled = await reconcile_bindings(store, source, [source_binding], now=clock())
    assert [o.neutral_id for o in reconciled.still_missing] == [neutral_id]

    # It is reportable and actionable, never just a count.
    digest = summarize_orphans(
        await store.list_orphans(source.name, unresolved_only=True), now=clock()
    )
    assert len(digest.confirmed) == 1
    assert "never deletes or deactivates" in digest.headline()

    # Through all of that -- two run_cycles, a recheck, and a reconcile pass -- the
    # target object is exactly where it was: read still succeeds...
    still_there = await target.read(target_binding.identity)
    assert still_there.name == "orders"

    # ...and neither connector has ever seen a delete call, nor has the read-only
    # source ever seen any write call at all.
    assert target.call_count("delete") == 0
    assert source.call_count("delete") == 0
    assert write_calls(source) == []


# ---------------------------------------------------------------------------------------
# Restart safety
# ---------------------------------------------------------------------------------------


async def test_the_orphan_log_survives_a_restart(tmp_path: Path, source: FakeConnector) -> None:
    """The orphan log is a durability guarantee, not an in-memory convenience: close
    the store, reopen a fresh one against the same database file, and everything this
    module needs is still there and still works."""
    db_url = f"sqlite:///{tmp_path / 'restart.db'}"
    upgrade_to_head(db_url)

    first_store = StateStore.from_url(db_url)
    try:
        ref = seed_product(source, "sales.orders")
        neutral_id = await bind(first_store, ref)
        source.vanish(ref)
        binding = await first_store.get_binding(neutral_id, source.name, EntityType.DATA_PRODUCT)
        assert binding is not None
        await reconcile_bindings(first_store, source, [binding], now=source.clock.now())
    finally:
        await first_store.aclose()

    second_store = StateStore.from_url(db_url)
    try:
        reopened = await second_store.list_orphans(source.name, unresolved_only=True)
        assert [record.neutral_id for record in reopened] == [neutral_id]

        # And the lifecycle functions work fine against the reopened store: a recheck
        # still finds it missing, still without ever touching a write path.
        source.reset_call_log()
        recheck = await recheck_open_orphans(
            second_store, source, entity_type=EntityType.DATA_PRODUCT, now=source.clock.now()
        )
        assert [o.neutral_id for o in recheck.still_missing] == [neutral_id]
        assert write_calls(source) == []

        digest = summarize_orphans(reopened, now=source.clock.now())
        assert digest.headline() != "No orphans outstanding."
    finally:
        await second_store.aclose()
