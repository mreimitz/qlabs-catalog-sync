"""identity_map: round-trip write/read, the explicit confirmation step, uniqueness."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.models import EntityType, IdentityRef

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


async def test_bind_identity_round_trips_by_native_key(store: StateStore) -> None:
    neutral_id = uuid4()
    identity = IdentityRef(
        endpoint="databricks",
        entity_type=EntityType.DATASET,
        native_key="main.sales.orders",
        tenant_id="tenant-a",
        secondary_keys={"legacy_id": "abc123"},
    )

    async with store.unit_of_work() as uow:
        await uow.bind_identity(neutral_id, identity, now=NOW)

    found = await store.find_binding(
        endpoint="databricks",
        entity_type=EntityType.DATASET,
        tenant_id="tenant-a",
        native_key="main.sales.orders",
    )
    assert found is not None
    assert found.neutral_id == neutral_id
    assert found.identity == identity
    assert found.confirmed is False
    assert found.confirmed_at is None
    assert found.created_at == NOW
    assert found.updated_at == NOW


async def test_get_binding_round_trips_by_neutral_id(store: StateStore) -> None:
    neutral_id = uuid4()
    identity = IdentityRef(
        endpoint="qlik",
        entity_type=EntityType.DATA_PRODUCT,
        native_key="dp-123",
        tenant_id="tenant-b",
    )

    async with store.unit_of_work() as uow:
        await uow.bind_identity(neutral_id, identity, now=NOW)

    found = await store.get_binding(neutral_id, "qlik", EntityType.DATA_PRODUCT)
    assert found is not None
    assert found.identity == identity


async def test_unbound_lookup_returns_none(store: StateStore) -> None:
    assert (
        await store.find_binding(
            endpoint="databricks",
            entity_type=EntityType.DATASET,
            tenant_id="tenant-a",
            native_key="does.not.exist",
        )
        is None
    )
    assert await store.get_binding(uuid4(), "databricks", EntityType.DATASET) is None


async def test_confirm_identity_sets_confirmed_and_timestamp(store: StateStore) -> None:
    neutral_id = uuid4()
    identity = IdentityRef(
        endpoint="databricks",
        entity_type=EntityType.DATASET,
        native_key="main.sales.customers",
        tenant_id="tenant-a",
    )
    confirmed_at = datetime(2026, 8, 20, 13, 0, 0, tzinfo=UTC)

    async with store.unit_of_work() as uow:
        await uow.bind_identity(neutral_id, identity, now=NOW)
    async with store.unit_of_work() as uow:
        await uow.confirm_identity(neutral_id, "databricks", EntityType.DATASET, now=confirmed_at)

    found = await store.get_binding(neutral_id, "databricks", EntityType.DATASET)
    assert found is not None
    assert found.confirmed is True
    assert found.confirmed_at == confirmed_at


async def test_rebinding_same_identity_upserts(store: StateStore) -> None:
    neutral_id = uuid4()
    identity = IdentityRef(
        endpoint="databricks",
        entity_type=EntityType.DATASET,
        native_key="main.sales.orders",
        tenant_id="tenant-a",
    )
    updated = identity.model_copy(update={"secondary_keys": {"alt": "xyz"}})

    async with store.unit_of_work() as uow:
        await uow.bind_identity(neutral_id, identity, now=NOW)
    later = datetime(2026, 8, 21, 0, 0, 0, tzinfo=UTC)
    async with store.unit_of_work() as uow:
        await uow.bind_identity(neutral_id, updated, confirmed=True, now=later)

    found = await store.get_binding(neutral_id, "databricks", EntityType.DATASET)
    assert found is not None
    assert found.identity.secondary_keys == {"alt": "xyz"}
    assert found.confirmed is True
    assert found.created_at == NOW  # created_at is not touched by an update
    assert found.updated_at == later


async def test_same_native_key_twice_at_same_endpoint_and_tenant_is_rejected(
    store: StateStore,
) -> None:
    first = IdentityRef(
        endpoint="databricks",
        entity_type=EntityType.DATASET,
        native_key="main.sales.orders",
        tenant_id="tenant-a",
    )
    second = IdentityRef(
        endpoint="databricks",
        entity_type=EntityType.DATASET,
        native_key="main.sales.orders",  # same native key, same endpoint, same tenant
        tenant_id="tenant-a",
    )

    async with store.unit_of_work() as uow:
        await uow.bind_identity(uuid4(), first, now=NOW)

    with pytest.raises(IntegrityError):
        async with store.unit_of_work() as uow:
            await uow.bind_identity(uuid4(), second, now=NOW)


async def test_same_native_key_is_allowed_across_different_tenants(store: StateStore) -> None:
    """Native keys are only unique *within* a tenant (RS-03 section 4)."""
    tenant_a = IdentityRef(
        endpoint="databricks",
        entity_type=EntityType.DATASET,
        native_key="main.sales.orders",
        tenant_id="tenant-a",
    )
    tenant_b = tenant_a.model_copy(update={"tenant_id": "tenant-b"})

    async with store.unit_of_work() as uow:
        await uow.bind_identity(uuid4(), tenant_a, now=NOW)
    async with store.unit_of_work() as uow:
        await uow.bind_identity(uuid4(), tenant_b, now=NOW)

    assert await store.find_binding(
        endpoint="databricks",
        entity_type=EntityType.DATASET,
        tenant_id="tenant-a",
        native_key="main.sales.orders",
    )
    assert await store.find_binding(
        endpoint="databricks",
        entity_type=EntityType.DATASET,
        tenant_id="tenant-b",
        native_key="main.sales.orders",
    )
