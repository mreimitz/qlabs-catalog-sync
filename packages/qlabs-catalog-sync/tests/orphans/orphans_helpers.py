"""Builders and shared assertions for the orphan-lifecycle tests."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    DataProductStatus,
    IdentityRef,
    Party,
    PartyRole,
    Tag,
    TextField,
)
from qlabs_catalog_sync_sdk.testing import FakeConnector

#: Connector methods that mutate something. Never empty for a target in the ordinary
#: create/update path; must stay free of ``delete`` for any connector this task touches.
WRITE_METHODS = ("create", "update", "delete")


def data_product(
    name: str,
    *,
    description: str | None = None,
    tags: Sequence[tuple[str, str | None]] = (),
    owners: Sequence[str] = (),
    status: DataProductStatus | None = None,
) -> DataProduct:
    """A neutral data product with just enough shape to round-trip through a
    :class:`FakeConnector`."""
    return DataProduct(
        name=name,
        description=None if description is None else TextField.plain(description),
        tags=[Tag(key=key, value=value) for key, value in tags],
        owners=[Party(email=email, role=PartyRole.OWNER) for email in owners],
        status=status,
    )


def seed_product(connector: FakeConnector, native_key: str, **kwargs: object) -> IdentityRef:
    """Seed one data product into ``connector`` under an explicit native key.

    Native keys are Unity Catalog paths (``catalog.schema``) so a bound identity looks
    like a real Databricks-sourced object.
    """
    name = str(kwargs.pop("name", native_key.split(".")[-1]))
    return connector.seed(data_product(name, **kwargs), native_key=native_key)  # type: ignore[arg-type]


def write_calls(connector: FakeConnector) -> list[str]:
    """Every recorded write-path call on ``connector``, in order.

    Decision D4's assertion: this must never contain ``delete``, for any connector,
    anywhere this task's code runs.
    """
    return [entry.method for entry in connector.call_log if entry.method in WRITE_METHODS]


async def bind(
    store: StateStore,
    *identities: IdentityRef,
    neutral_id: uuid.UUID | None = None,
    confirmed: bool = True,
) -> uuid.UUID:
    """Bind one neutral id to each ``identity``, standing in for T7.1's confirmation step.

    Written straight through the state store because the module under test reacts to
    an already-bound identity; arranging the binding by hand is setup, not behavior.
    """
    identifier = neutral_id if neutral_id is not None else uuid.uuid4()
    now = datetime.now(UTC)
    async with store.unit_of_work() as uow:
        for identity in identities:
            await uow.bind_identity(identifier, identity, confirmed=confirmed, now=now)
    return identifier
