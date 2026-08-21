"""``reconcile_bindings`` -- the general "``list_changed`` silently dropped it" backstop.

Nothing here ever calls ``list_changed``: every check goes straight through
``source.read`` against a caller-supplied identity binding, which is exactly how this
detects the object T2.4's ``run_cycle`` structurally cannot see -- one that a
connector's delta listing simply stops mentioning, rather than reporting
``ChangeKind.DELETED`` the way ``qlabs_connector_databricks`` derives from its own
snapshot diff.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from orphans_helpers import bind, seed_product, write_calls

from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.orphans import reconcile_bindings
from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 20, 12, 5, 0, tzinfo=UTC)


async def test_detects_an_object_list_changed_never_mentions_again(
    store: StateStore, source: FakeConnector, target: FakeConnector
) -> None:
    """The core gap this task closes, proven the way the task asks: a fresh call log
    shows this pass never goes anywhere near ``list_changed``."""
    ref = seed_product(source, "sales.orders")
    neutral_id = await bind(store, ref)
    binding = await store.get_binding(neutral_id, source.name, EntityType.DATA_PRODUCT)
    assert binding is not None

    source.vanish(ref)  # appends a changelog DELETED entry, but nobody ever reads it
    source.reset_call_log()

    report = await reconcile_bindings(store, source, [binding], now=NOW)

    assert [orphan.neutral_id for orphan in report.newly_missing] == [neutral_id]
    assert report.still_missing == ()
    assert report.resolved == ()
    stored = await store.list_orphans(source.name, unresolved_only=True)
    assert [record.neutral_id for record in stored] == [neutral_id]
    assert [entry.method for entry in source.call_log] == ["read"]
    assert write_calls(source) == []
    assert write_calls(target) == []


async def test_a_second_independent_check_confirms_a_still_missing_object(
    store: StateStore, source: FakeConnector
) -> None:
    ref = seed_product(source, "sales.orders")
    neutral_id = await bind(store, ref)
    binding = await store.get_binding(neutral_id, source.name, EntityType.DATA_PRODUCT)
    assert binding is not None
    source.vanish(ref)

    first = await reconcile_bindings(store, source, [binding], now=NOW)
    second = await reconcile_bindings(store, source, [binding], now=LATER)

    assert [o.neutral_id for o in first.newly_missing] == [neutral_id]
    assert second.newly_missing == ()
    assert [o.neutral_id for o in second.still_missing] == [neutral_id]

    stored = (await store.list_orphans(source.name, unresolved_only=True))[0]
    assert stored.first_missing_at == NOW  # unchanged
    assert stored.last_missing_at == LATER  # advanced


async def test_an_object_that_returns_is_resolved(store: StateStore, source: FakeConnector) -> None:
    ref = seed_product(source, "sales.orders", description="Order facts")
    neutral_id = await bind(store, ref)
    binding = await store.get_binding(neutral_id, source.name, EntityType.DATA_PRODUCT)
    assert binding is not None
    source.vanish(ref)
    await reconcile_bindings(store, source, [binding], now=NOW)
    assert await store.list_orphans(source.name, unresolved_only=True)

    seed_product(source, "sales.orders", description="Order facts")  # it is back

    report = await reconcile_bindings(store, source, [binding], now=LATER)

    assert list(report.resolved) == [neutral_id]
    assert report.missing == ()
    assert await store.list_orphans(source.name, unresolved_only=True) == []


async def test_an_object_never_missing_is_left_alone(
    store: StateStore, source: FakeConnector
) -> None:
    ref = seed_product(source, "sales.orders")
    neutral_id = await bind(store, ref)
    binding = await store.get_binding(neutral_id, source.name, EntityType.DATA_PRODUCT)
    assert binding is not None

    report = await reconcile_bindings(store, source, [binding], now=NOW)

    assert report.missing == ()
    assert report.resolved == ()
    assert await store.list_orphans(source.name, unresolved_only=True) == []


async def test_a_failed_check_is_never_treated_as_a_deletion(
    store: StateStore, source: FakeConnector
) -> None:
    """A permissions glitch (or any other typed connector error) from ``read`` must
    never masquerade as evidence the object is gone -- that would be exactly the false
    alarm the confirmation policy exists to avoid."""
    ref = seed_product(source, "sales.orders")
    neutral_id = await bind(store, ref)
    binding = await store.get_binding(neutral_id, source.name, EntityType.DATA_PRODUCT)
    assert binding is not None
    source.fail_next("read", AuthError("token expired", endpoint=source.name))

    report = await reconcile_bindings(store, source, [binding], now=NOW)

    assert report.missing == ()
    assert report.resolved == ()
    assert len(report.inconclusive) == 1
    assert report.inconclusive[0].neutral_id == neutral_id
    assert "AuthError" in (report.inconclusive[0].detail or "")
    assert await store.list_orphans(source.name, unresolved_only=True) == []


async def test_a_binding_for_the_wrong_endpoint_is_rejected(
    store: StateStore, source: FakeConnector, target: FakeConnector
) -> None:
    ref = seed_product(target, "qlik-orders")  # bound at the *target*, not this source
    neutral_id = await bind(store, ref)
    binding = await store.get_binding(neutral_id, target.name, EntityType.DATA_PRODUCT)
    assert binding is not None

    with pytest.raises(ValueError, match=target.name):
        await reconcile_bindings(store, source, [binding], now=NOW)


async def test_empty_bindings_is_a_no_op(store: StateStore, source: FakeConnector) -> None:
    report = await reconcile_bindings(store, source, [], now=NOW)
    assert report.checked == 0
    assert report.missing == ()
    assert report.resolved == ()
