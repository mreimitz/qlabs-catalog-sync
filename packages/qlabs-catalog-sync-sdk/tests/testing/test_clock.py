"""Deterministic everywhere: FakeConnector never advances the clock on its own, and
timestamps it produces come from the injected clock, not the wall clock.
"""

from __future__ import annotations

from datetime import UTC, datetime

from qlabs_catalog_sync_sdk.config import ManualClock
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType, FieldChange, FieldDiff
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def test_the_clock_never_advances_on_its_own(clock: ManualClock) -> None:
    before = clock.now()
    connector = FakeConnector.write_target(clock=clock)

    await connector.healthcheck()
    await connector.create(DataProduct(name="Retail Sales"))
    await connector.healthcheck()

    assert clock.now() == before
    assert clock.sleep_calls == []


async def test_a_default_connector_owns_its_own_manual_clock(target: FakeConnector) -> None:
    assert isinstance(target.clock, ManualClock)


async def test_stored_timestamps_come_from_the_injected_clock_not_the_wall_clock(
    clock: ManualClock,
) -> None:
    connector = FakeConnector.write_target(clock=clock)
    fixed_instant = clock.now()

    created = await connector.create(DataProduct(name="Retail Sales"))
    entity = await connector.read(created.ref)

    envelope = entity.field_envelopes["name"]
    assert envelope.last_modified_at == fixed_instant
    assert envelope.last_modified_at != datetime.now(UTC)  # not the real wall clock


async def test_advancing_the_clock_changes_subsequent_timestamps(clock: ManualClock) -> None:
    connector = FakeConnector.write_target(clock=clock)
    created = await connector.create(DataProduct(name="Retail Sales"))
    first_envelope = (await connector.read(created.ref)).field_envelopes["name"]

    clock.advance(60)
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="name", value="Retail Sales v2")],
    )
    await connector.update(created.ref, diff)
    second_envelope = (await connector.read(created.ref)).field_envelopes["name"]

    assert second_envelope.last_modified_at is not None
    assert first_envelope.last_modified_at is not None
    assert second_envelope.last_modified_at > first_envelope.last_modified_at
