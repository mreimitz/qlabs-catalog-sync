"""Builders and shared assertions for the sync-loop tests."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.contract import Watermark
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    DataProductStatus,
    EntityType,
    FieldDiff,
    IdentityRef,
    NeutralEntity,
    Party,
    PartyRole,
    Tag,
    TextField,
)
from qlabs_catalog_sync_sdk.testing import (
    DEFAULT_TENANT_ID,
    FakeConnector,
    qlik_shaped_manifest,
)

WRITE_METHODS = ("create", "update", "delete")


class FailingTarget(FakeConnector):
    """A Qlik-shaped write target whose write path starts failing from the Nth call.

    ``fail_next`` on :class:`FakeConnector` is FIFO from the *next* call, which cannot
    express "let the first write really land, then fail". That is exactly the shape a
    mid-cycle failure has, so this subclass adds it: calls before ``fail_from`` go through
    the real in-memory write path and genuinely mutate the target, and calls from
    ``fail_from`` on raise. Set ``fail_from`` back to ``None`` to let the retry succeed.
    """

    name: ClassVar[str] = "fake-target"

    def __init__(self, *, error: BaseException, method: str = "update", **kwargs: Any) -> None:
        super().__init__(manifest=qlik_shaped_manifest(), **kwargs)
        self.fail_from: int | None = None
        self.failing_method = method
        self.error = error
        self._writes = 0

    def _maybe_fail(self, method: str) -> None:
        if method != self.failing_method:
            return
        self._writes += 1
        if self.fail_from is not None and self._writes >= self.fail_from:
            raise self.error

    async def update(self, ref: IdentityRef, diff: FieldDiff) -> Any:
        self._maybe_fail("update")
        return await super().update(ref, diff)

    async def create(self, entity: NeutralEntity) -> Any:
        self._maybe_fail("create")
        return await super().create(entity)


def data_product(
    name: str,
    *,
    description: str | None = None,
    tags: Sequence[tuple[str, str | None]] = (),
    owners: Sequence[str] = (),
    status: DataProductStatus | None = None,
) -> DataProduct:
    """A neutral data product with just enough shape to produce a real, multi-field diff."""
    return DataProduct(
        name=name,
        description=None if description is None else TextField.plain(description),
        tags=[Tag(key=key, value=value) for key, value in tags],
        owners=[Party(email=email, role=PartyRole.OWNER) for email in owners],
        status=status,
    )


def seed_product(connector: FakeConnector, native_key: str, **kwargs: object) -> IdentityRef:
    """Seed one data product into ``connector`` under an explicit native key.

    Native keys are Unity Catalog paths (``catalog.schema``) because that is what decision
    D1's ``catalog.schema`` selector is applied to.
    """
    name = str(kwargs.pop("name", native_key.split(".")[-1]))
    return connector.seed(data_product(name, **kwargs), native_key=native_key)  # type: ignore[arg-type]


def write_calls(connector: FakeConnector) -> list[str]:
    """Every recorded write-path call on ``connector``, in order.

    The direction guard and decision D4 are both assertions about this list: it must stay
    empty for a source connector, and must never contain ``delete`` for any connector.
    """
    return [entry.method for entry in connector.call_log if entry.method in WRITE_METHODS]


async def bind(
    store: StateStore,
    *identities: IdentityRef,
    neutral_id: uuid.UUID | None = None,
    confirmed: bool = True,
) -> uuid.UUID:
    """Bind one neutral id to each ``identity``, standing in for T7.1's confirmation step.

    Written straight through the state store because the *loop* is what is under test here:
    arranging a confirmed binding by hand is the setup, not the behavior.
    """
    identifier = neutral_id if neutral_id is not None else uuid.uuid4()
    now = datetime.now(UTC)
    async with store.unit_of_work() as uow:
        for identity in identities:
            await uow.bind_identity(identifier, identity, confirmed=confirmed, now=now)
    return identifier


def target_identity(native_key: str, endpoint: str = "fake-target") -> IdentityRef:
    """A target-side :class:`IdentityRef` for ``native_key``."""
    return IdentityRef(
        endpoint=endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=native_key,
        tenant_id=DEFAULT_TENANT_ID,
    )


def cursor_position(token: str | None) -> str | None:
    """The cursor inside a serialized watermark token, or ``None`` for an unset watermark."""
    if token is None:
        return None
    return Watermark.model_validate_json(token).cursor
