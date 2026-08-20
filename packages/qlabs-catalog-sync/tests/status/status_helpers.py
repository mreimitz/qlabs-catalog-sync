"""Builders shared across the T7.4 ``sync.status`` tests.

Self-contained on purpose, matching ``tests/policy/conftest.py``'s own stated convention:
``tests/status`` is a separate ownership boundary from ``tests/sync`` and ``tests/policy``,
so it builds its own small fixtures rather than importing theirs, even though the shapes
are deliberately similar.
"""

from __future__ import annotations

from typing import ClassVar

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync_sdk.contract import IdentityRef, WriteResult
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    DataProductStatus,
    EntityType,
    FieldChange,
    FieldDiff,
)
from qlabs_catalog_sync_sdk.testing import DEFAULT_TENANT_ID, FakeConnector

SOURCE = "fake-source"
TARGET = "fake-target"


def data_product(name: str, *, status: DataProductStatus | None = None) -> DataProduct:
    """A neutral data product with just enough shape for the activation decision."""
    return DataProduct(name=name, status=status)


def target_identity(native_key: str, endpoint: str = TARGET) -> IdentityRef:
    """A target-side :class:`IdentityRef` for ``native_key``."""
    return IdentityRef(
        endpoint=endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=native_key,
        tenant_id=DEFAULT_TENANT_ID,
    )


def make_pair(*, activation_opt_in: bool = False, name: str = "db-to-qlik") -> SyncPairConfig:
    """One minimal, valid sync pair, with ``activation_opt_in`` as the one dial tests turn."""
    return SyncPairConfig(
        name=name,
        source=SOURCE,
        target=TARGET,
        catalog_schema_patterns=["sales.*"],
        target_space="Sales Space",
        entity_types=[EntityType.DATA_PRODUCT],
        activation_opt_in=activation_opt_in,
    )


class ActivatingFakeConnector(FakeConnector):
    """A Qlik-shaped :class:`FakeConnector` that also implements :class:`ActivationCapable`.

    Nothing in the SDK's ``FakeConnector`` exposes an ``activate`` route today — that is
    exactly the gap ``status.py``'s module docstring describes (no connector in this
    repository implements :class:`~qlabs_catalog_sync.sync.status.ActivationCapable` yet,
    including the real Qlik one). This test-local subclass stands in for the *future*
    connector the wiring note describes: its ``activate`` translates the request into the
    one generic mechanism a plain connector actually has (``update``, per T3.7's own
    finding that this is "the only generic route from the engine"), so a call to it shows
    up on :attr:`FakeConnector.call_log` exactly like any other write — which is what lets
    a test assert "the connector's call log" the way the task requires. Production code
    never does this translation itself; a real connector's ``activate`` calls its own
    vendor API directly, as ``qlabs_connector_qlik.lifecycle.LifecycleActions.activate``
    already does.
    """

    name: ClassVar[str] = TARGET

    async def activate(
        self, ref: IdentityRef, *, name: str, managed_space_id: str
    ) -> WriteResult:
        diff = FieldDiff(
            entity_type=EntityType.DATA_PRODUCT,
            changes=[FieldChange(field="status", value=DataProductStatus.ACTIVE.value)],
        )
        return await self.update(ref, diff)
