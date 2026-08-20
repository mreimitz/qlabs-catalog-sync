"""Restart safety: one unit of work is one session and one transaction.

T2.4's sync loop persists envelopes and advances the watermark together so a crash
mid-cycle commits nothing. These tests prove the primitive that guarantee rests on:
:meth:`StateStore.unit_of_work` opens exactly one session for the whole block, and a
block that raises leaves the database exactly as it was before the block started.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.models import EntityType, FieldEnvelope, IdentityRef

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


class _SimulatedCrash(Exception):
    """Stands in for whatever kills the process mid-cycle."""


async def test_successful_unit_of_work_commits_every_write_together(store: StateStore) -> None:
    neutral_id = uuid4()
    identity = IdentityRef(
        endpoint="databricks",
        entity_type=EntityType.DATASET,
        native_key="main.sales.orders",
        tenant_id="tenant-a",
    )

    async with store.unit_of_work() as uow:
        await uow.bind_identity(neutral_id, identity, now=NOW)
        await uow.upsert_field_envelope(
            neutral_id,
            "databricks",
            EntityType.DATASET,
            "name",
            FieldEnvelope(value="orders", source_endpoint="databricks"),
            now=NOW,
        )
        await uow.advance_watermark(
            "databricks-to-qlik", "databricks", EntityType.DATASET, "next-token", run_at=NOW
        )

    assert await store.get_binding(neutral_id, "databricks", EntityType.DATASET) is not None
    envelopes = await store.fetch_envelopes(neutral_id, "databricks")
    assert envelopes["name"].value == "orders"
    watermark = await store.get_watermark("databricks-to-qlik", "databricks", EntityType.DATASET)
    assert watermark is not None
    assert watermark.watermark_token == "next-token"


async def test_a_unit_of_work_that_raises_midway_commits_nothing(store: StateStore) -> None:
    neutral_id = uuid4()

    with pytest.raises(_SimulatedCrash):
        async with store.unit_of_work() as uow:
            await uow.upsert_field_envelope(
                neutral_id,
                "databricks",
                EntityType.DATASET,
                "name",
                FieldEnvelope(value="orders", source_endpoint="databricks"),
                now=NOW,
            )
            await uow.advance_watermark(
                "databricks-to-qlik", "databricks", EntityType.DATASET, "next-token", run_at=NOW
            )
            raise _SimulatedCrash("process dies after the writes, before commit")

    # Nothing from the aborted cycle is visible: not the envelope, not the watermark.
    assert await store.fetch_envelopes(neutral_id, "databricks") == {}
    assert await store.get_watermark("databricks-to-qlik", "databricks", EntityType.DATASET) is None


async def test_a_retried_cycle_after_a_crash_succeeds_cleanly(store: StateStore) -> None:
    """The idempotent-retry story: after an aborted cycle, redoing the same work lands."""
    neutral_id = uuid4()

    with pytest.raises(_SimulatedCrash):
        async with store.unit_of_work() as uow:
            await uow.advance_watermark(
                "databricks-to-qlik", "databricks", EntityType.DATASET, "stale-token", run_at=NOW
            )
            raise _SimulatedCrash()

    later = datetime(2026, 8, 20, 12, 5, 0, tzinfo=UTC)
    async with store.unit_of_work() as uow:
        await uow.upsert_field_envelope(
            neutral_id,
            "databricks",
            EntityType.DATASET,
            "name",
            FieldEnvelope(value="orders", source_endpoint="databricks"),
            now=later,
        )
        await uow.advance_watermark(
            "databricks-to-qlik", "databricks", EntityType.DATASET, "fresh-token", run_at=later
        )

    watermark = await store.get_watermark("databricks-to-qlik", "databricks", EntityType.DATASET)
    assert watermark is not None
    assert watermark.watermark_token == "fresh-token"
    envelopes = await store.fetch_envelopes(neutral_id, "databricks")
    assert envelopes["name"].value == "orders"


async def test_unit_of_work_opens_exactly_one_session_for_the_whole_block(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Whitebox: reach into the private session factory to count how many sessions a
    # single unit_of_work block opens, proving it is one -- not one per write.
    original_factory = store._session_factory
    calls: list[object] = []

    def counting_factory() -> object:
        session = original_factory()
        calls.append(session)
        return session

    monkeypatch.setattr(store, "_session_factory", counting_factory)

    neutral_id = uuid4()
    identity = IdentityRef(
        endpoint="databricks",
        entity_type=EntityType.DATASET,
        native_key="main.sales.orders",
        tenant_id="tenant-a",
    )
    async with store.unit_of_work() as uow:
        await uow.bind_identity(neutral_id, identity, now=NOW)
        await uow.upsert_field_envelope(
            neutral_id,
            "databricks",
            EntityType.DATASET,
            "name",
            FieldEnvelope(value="orders", source_endpoint="databricks"),
            now=NOW,
        )
        await uow.advance_watermark(
            "databricks-to-qlik", "databricks", EntityType.DATASET, "token", run_at=NOW
        )

    assert len(calls) == 1, "unit_of_work must open exactly one session, not one per write"
