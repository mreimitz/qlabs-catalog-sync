"""Builders shared by the T2.5 manual-edit-policy tests.

A deliberate near-duplicate of ``tests/sync/sync_helpers.py`` rather than an import of
it: ``tests/policy`` and ``tests/sync`` are separate ownership boundaries (T2.5 owns
only this directory), so this module stays self-contained instead of reaching into a
sibling test package it does not own.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.diff import compute_field_diff
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import WriteReview
from qlabs_catalog_sync_sdk.envelope import build_field_envelopes
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    EntityType,
    FieldEnvelope,
    IdentityRef,
    Party,
    PartyRole,
    Tag,
    TextField,
)
from qlabs_catalog_sync_sdk.testing import DEFAULT_TENANT_ID, FakeConnector


def data_product(
    name: str,
    *,
    description: str | None = None,
    tags: Sequence[tuple[str, str | None]] = (),
    owners: Sequence[str] = (),
) -> DataProduct:
    """A neutral data product with just enough shape to produce a real field diff."""
    return DataProduct(
        name=name,
        description=None if description is None else TextField.plain(description),
        tags=[Tag(key=key, value=value) for key, value in tags],
        owners=[Party(email=email, role=PartyRole.OWNER) for email in owners],
    )


def seed_product(connector: FakeConnector, native_key: str, **kwargs: object) -> IdentityRef:
    """Seed one data product into ``connector`` under an explicit native key.

    Native keys are Unity Catalog paths (``catalog.schema``), matching decision D1's
    ``catalog.schema`` selector.
    """
    name = str(kwargs.pop("name", native_key.split(".")[-1]))
    return connector.seed(data_product(name, **kwargs), native_key=native_key)  # type: ignore[arg-type]


async def bind(
    store: StateStore,
    *identities: IdentityRef,
    neutral_id: uuid.UUID | None = None,
    confirmed: bool = True,
) -> uuid.UUID:
    """Bind one neutral id to each ``identity``, standing in for T7.1's confirmation step."""
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


def build_review(
    *,
    pair: SyncPairConfig,
    target: FakeConnector,
    target_ref: IdentityRef,
    source_values: Mapping[str, Any],
    stored_values: Mapping[str, Any] | None,
    entity_type: EntityType = EntityType.DATA_PRODUCT,
    neutral_id: uuid.UUID | None = None,
    source_endpoint: str = "fake-source",
) -> WriteReview:
    """A genuine :class:`WriteReview`, built exactly the way ``sync/loop.py`` builds one.

    ``plan`` comes from the real :func:`~qlabs_catalog_sync.diff.compute_field_diff`
    against ``target``'s real capability manifest -- not a hand-rolled ``DiffPlan`` --
    so a policy-level test exercises the same seam the loop itself does, just without
    running a whole cycle around it. ``stored_values=None`` means the target has never
    held this entity at all (an empty ``stored_target_envelopes``, exactly what
    ``sync/loop.py`` passes on a first sync).
    """
    source_envelopes = build_field_envelopes(dict(source_values), source_endpoint=source_endpoint)
    stored_envelopes: dict[str, FieldEnvelope[Any]] = (
        {}
        if stored_values is None
        else build_field_envelopes(dict(stored_values), source_endpoint=target.name)
    )
    plan = compute_field_diff(
        entity_type=entity_type,
        source_envelopes=source_envelopes,
        target_envelopes=stored_envelopes,
        manifest=target.capabilities(),
        endpoint=target.name,
    )
    return WriteReview(
        pair=pair,
        entity_type=entity_type,
        neutral_id=neutral_id if neutral_id is not None else uuid.uuid4(),
        target_ref=target_ref,
        plan=plan,
        source_envelopes=source_envelopes,
        stored_target_envelopes=stored_envelopes,
        target=target,
    )
