"""Property 3 -- restart resumes from the persisted watermark.

Every other test in this package reuses the *same* ``SyncLoop`` object across
``run_cycle`` calls, which proves the loop is stateless within a Python process but
proves nothing about whether the resume position is durable across a real restart --
the loop could, in principle, be caching something in memory that a second cycle on the
same object happens to see. These tests instead build a genuinely *second*,
independent :class:`~qlabs_catalog_sync.state.store.StateStore` (a fresh SQLAlchemy
engine and session factory) against the same on-disk file, and a fresh
:class:`~qlabs_catalog_sync.identity.IdentityResolver` and :class:`SyncLoop` over it,
while deliberately keeping the *same* connector instances -- Databricks and Qlik do not
restart just because the sync engine's process does. Only what the state store
persisted is available to the second loop.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from restart_helpers import Clock, cursor_position, no_sleep, seed_product

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import RecordOutcome, RunStatus, SyncLoop
from qlabs_catalog_sync_sdk.models import EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def test_a_freshly_built_engine_resumes_from_the_persisted_watermark(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
    clock: Clock,
    db_url: str,
    tmp_path: Path,
) -> None:
    seed_product(source, "sales.orders", description="Order facts")
    first_loop = make_loop(create_missing=True)
    first = await first_loop.run_cycle(EntityType.DATA_PRODUCT)
    assert first.status is RunStatus.OK
    watermark_after_first = await store.get_watermark(
        pair.name, source.name, EntityType.DATA_PRODUCT
    )
    assert watermark_after_first is not None

    # Simulate the engine process restarting: nothing here reuses the first loop, the
    # first store, or the first resolver.
    restarted_store = StateStore.from_url(db_url)
    try:
        restarted_resolver = IdentityResolver(
            restarted_store, review_path=tmp_path / "identity-review-restart.json", clock=clock
        )
        restarted_loop = SyncLoop(
            pair=pair,
            source=source,
            target=target,
            store=restarted_store,
            resolver=restarted_resolver,
            clock=clock,
            sleep=no_sleep,
            create_missing=True,
        )

        # A change appears at the source while the engine was "down".
        seed_product(source, "sales.returns", description="Return facts")
        source.reset_call_log()

        second = await restarted_loop.run_cycle(EntityType.DATA_PRODUCT)

        # Resumed, not rescanned: list_changed was called with exactly the persisted
        # cursor, not a fresh start.
        listed_since = source.calls("list_changed")[0].args["since"]
        assert not listed_since.is_initial
        assert listed_since.model_dump_json() == watermark_after_first.watermark_token

        # And only the new record was processed -- the pre-restart object was never
        # re-listed, let alone re-created.
        assert [record.native_key for record in second.records] == ["sales.returns"]
        assert second.records[0].outcome is RecordOutcome.CREATED
        assert source.call_count("read") == 1
    finally:
        await restarted_store.aclose()


async def test_a_held_watermark_survives_a_restart_and_relists_the_same_page(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
    clock: Clock,
    db_url: str,
    tmp_path: Path,
) -> None:
    """The watermark-hold rule (a record with work outstanding pins the watermark) is
    not something the in-memory loop remembers to re-apply -- it is read back off the
    persisted row by a completely different engine instance.
    """
    seed_product(source, "sales.orders")  # no target binding, and creation is off
    first_loop = make_loop()  # create_missing left at its default: off
    first = await first_loop.run_cycle(EntityType.DATA_PRODUCT)
    assert first.status is RunStatus.PARTIAL
    assert first.watermark_held_by == ("sales.orders",)
    row = await store.get_watermark(pair.name, source.name, EntityType.DATA_PRODUCT)
    assert row is not None
    assert cursor_position(row.watermark_token) is None  # held at the very start

    restarted_store = StateStore.from_url(db_url)
    try:
        restarted_resolver = IdentityResolver(
            restarted_store, review_path=tmp_path / "identity-review-restart.json", clock=clock
        )
        restarted_loop = SyncLoop(
            pair=pair,
            source=source,
            target=target,
            store=restarted_store,
            resolver=restarted_resolver,
            clock=clock,
            sleep=no_sleep,
        )
        source.reset_call_log()

        second = await restarted_loop.run_cycle(EntityType.DATA_PRODUCT)

        # The held-back record is relisted, not lost, by an engine that never saw the
        # first cycle run.
        assert [record.native_key for record in second.records] == ["sales.orders"]
        assert second.status is RunStatus.PARTIAL
        assert second.watermark_held_by == ("sales.orders",)
    finally:
        await restarted_store.aclose()


async def test_a_paged_cycle_resumed_by_a_fresh_engine_continues_from_the_right_page(
    make_loop: Callable[..., SyncLoop],
    target: FakeConnector,
    store: StateStore,
    pair: SyncPairConfig,
    clock: Clock,
    db_url: str,
    tmp_path: Path,
) -> None:
    """``max_pages`` stopping a cycle early commits the last fully-processed page; a
    restarted engine resumes exactly there, not from page one.
    """
    paged = FakeConnector.read_only_source(name="fake-source", list_changed_page_size=1)
    for key in ("sales.orders", "sales.returns", "sales.refunds"):
        seed_product(paged, key)

    first_loop = make_loop(source=paged, create_missing=True, max_pages=1)
    first = await first_loop.run_cycle(EntityType.DATA_PRODUCT)
    assert first.pages == 1
    assert first.has_more is True
    assert [record.native_key for record in first.records] == ["sales.orders"]

    restarted_store = StateStore.from_url(db_url)
    try:
        restarted_resolver = IdentityResolver(
            restarted_store, review_path=tmp_path / "identity-review-restart.json", clock=clock
        )
        restarted_loop = SyncLoop(
            pair=pair,
            source=paged,
            target=target,
            store=restarted_store,
            resolver=restarted_resolver,
            clock=clock,
            sleep=no_sleep,
            create_missing=True,
        )
        paged.reset_call_log()

        second = await restarted_loop.run_cycle(EntityType.DATA_PRODUCT)

        # Resumes at page two, not page one: "sales.orders" (page one, already
        # committed before the restart) is never seen again.
        assert [record.native_key for record in second.records] == [
            "sales.returns",
            "sales.refunds",
        ]
    finally:
        await restarted_store.aclose()
