"""Identity: what the loop does with a source object that has no confirmed counterpart.

The rule the whole file exists to prove is T7.1's: a bootstrap proposal is never bound
automatically, and no absence of a binding is ever read as permission to invent one.
"""

from __future__ import annotations

from collections.abc import Callable

from sync_helpers import bind, data_product, seed_product, write_calls

from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import RecordOutcome, RunStatus, SkipReason, SyncLoop
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def test_an_unbound_source_object_is_skipped_and_reported_not_invented(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """No confirmed counterpart, creation off: nothing is written and the report says why."""
    seed_product(source, "sales.orders")

    report = await make_loop().run_cycle(EntityType.DATA_PRODUCT)

    record = report.records[0]
    assert record.outcome is RecordOutcome.SKIPPED
    assert record.reason is SkipReason.NO_TARGET_BINDING
    assert record.holds_watermark is True
    assert "identity bootstrap" in (record.detail or "")
    assert write_calls(target) == []

    # The source side *is* recorded -- minting an id for a source key claims nothing --
    # but no target binding was invented for it.
    assert record.neutral_id is not None
    assert await store.get_binding(record.neutral_id, source.name, EntityType.DATA_PRODUCT)
    assert await store.get_binding(record.neutral_id, target.name, EntityType.DATA_PRODUCT) is None


async def test_an_unconfirmed_binding_is_never_written_to_and_never_creates_a_duplicate(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """A pending bootstrap proposal blocks the write -- and blocks creation too.

    Creating here would be the worst outcome of all: a second Qlik object beside the one a
    human is in the middle of deciding about.
    """
    source_ref = seed_product(source, "sales.orders", description="Order facts")
    proposed = target.seed(data_product("orders"), native_key="qlik-orders")
    await bind(store, source_ref, proposed, confirmed=False)
    target.reset_call_log()

    report = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    record = report.records[0]
    assert record.outcome is RecordOutcome.SKIPPED
    assert record.reason is SkipReason.UNCONFIRMED_TARGET_BINDING
    assert record.holds_watermark is True
    assert write_calls(target) == []
    assert report.status is RunStatus.PARTIAL


async def test_a_confirmed_binding_is_the_licence_to_write(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """The same arrangement, once confirmed, writes the bound object and creates nothing."""
    source_ref = seed_product(source, "sales.orders", description="Order facts")
    proposed = target.seed(data_product("orders", description="stale"), native_key="qlik-orders")
    await bind(store, source_ref, proposed, confirmed=True)
    target.reset_call_log()

    report = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    assert report.records[0].outcome is RecordOutcome.WRITTEN
    assert write_calls(target) == ["update"]
    assert report.status is RunStatus.OK


async def test_the_neutral_id_is_stable_across_cycles(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, store: StateStore
) -> None:
    """A source key keeps the id the engine minted for it, cycle after cycle."""
    seed_product(source, "sales.orders")
    loop = make_loop()

    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    second = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert first.records[0].neutral_id is not None
    assert first.records[0].neutral_id == second.records[0].neutral_id


async def test_two_changes_for_one_object_in_one_listing_create_it_once(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """One object, two changelog entries, one neutral id and one created target object."""
    ref = seed_product(source, "sales.orders", description="Order facts")
    source.simulate_external_edit(ref, {"description": {"text": "v2", "format": "plain"}})

    report = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    assert len(report.records) == 2  # the source really did report it twice
    assert target.call_count("create") == 1
    ids = {record.neutral_id for record in report.records}
    assert len(ids) == 1
    assert report.records[0].outcome is RecordOutcome.CREATED
    # The second pass sees the write the first one just made, so it sends nothing more.
    assert report.records[1].outcome is not RecordOutcome.CREATED
    assert target.call_count("update") == 0


async def test_creation_is_off_by_default(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, target: FakeConnector
) -> None:
    """A pair that has not asked for creation gets none -- a brownfield tenant is the norm."""
    seed_product(source, "sales.orders")

    report = await make_loop().run_cycle(EntityType.DATA_PRODUCT)

    assert report.create_enabled is False
    assert target.call_count("create") == 0


async def test_a_created_object_is_bound_to_the_key_the_target_returned(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """The binding a create produces is not a name match: it is the target's own answer."""
    seed_product(source, "sales.orders")

    report = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    result = target.calls("create")[0].result
    neutral_id = report.records[0].neutral_id
    assert neutral_id is not None
    binding = await store.get_binding(neutral_id, target.name, EntityType.DATA_PRODUCT)
    assert binding is not None
    assert binding.identity.native_key == result.ref.native_key
    assert binding.confirmed is True
    # Nothing in the created object's key came from the source's name.
    assert binding.identity.native_key != "sales.orders"


async def test_a_created_product_lands_in_the_pairs_target_space(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, target: FakeConnector
) -> None:
    """``target_space`` is what the pair configured it for."""
    seed_product(source, "sales.orders")

    await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    created = target.calls("create")[0].args["entity"]
    assert created.placement == "Sales Space"
