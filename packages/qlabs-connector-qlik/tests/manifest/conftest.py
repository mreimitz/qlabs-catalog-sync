"""Shared fixtures for T3.2's manifest tests.

Everything here is built from the real ``qlik_capability_manifest()`` (not a
hand-rolled stub) and a real ``Connector`` subclass, mirroring the pattern the SDK's
own T1.3 test suite uses (``qlabs_catalog_sync_sdk/tests/manifest/conftest.py``) so the
manifest is proven end to end through the contract's own guards
(``ensure_supported``/``ensure_writable``), not just in isolation.
"""

from __future__ import annotations

import pytest
from pydantic_settings import BaseSettings

from qlabs_catalog_sync_sdk.contract import (
    Connector,
    ConnectorContext,
    FieldDiff,
    HealthStatus,
    IdentityRef,
    ListChangedResult,
    Watermark,
    WriteResult,
)
from qlabs_catalog_sync_sdk.manifest import CapabilityManifest
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType
from qlabs_connector_qlik.manifest import qlik_capability_manifest

ENDPOINT = "qlik"
TENANT = "tenant-a"


class _StubConfig(BaseSettings):
    """An empty connector config — these fixtures need no real settings."""


class FakeManifestConnector(Connector):
    """A real ``Connector`` subclass wired to the real Qlik manifest, write path
    included, so ``ensure_supported``/``ensure_writable`` are exercised end to end
    exactly as the engine would use them — not just against the manifest in isolation.

    This is a test-only stand-in for ``qlabs_connector_qlik.Connector``: it exists so
    this suite can prove the manifest without depending on T3.3's read path or
    T3.4-T3.7's write path, none of which have landed yet.
    """

    name = ENDPOINT
    ConfigModel = _StubConfig

    def capabilities(self) -> CapabilityManifest:
        return qlik_capability_manifest()

    async def setup(self, ctx: ConnectorContext[_StubConfig]) -> None:
        return None

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus.healthy(self.name)

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        return ListChangedResult.empty(since)

    async def read(self, ref: IdentityRef) -> DataProduct:
        return DataProduct(identities=[ref], name="stub")

    async def update(self, ref: IdentityRef, diff: FieldDiff) -> WriteResult:
        self.ensure_writable(diff)
        return WriteResult.updated(ref, written_fields=diff.field_names)


@pytest.fixture
def qlik_manifest() -> CapabilityManifest:
    return qlik_capability_manifest()


@pytest.fixture
def qlik_connector() -> FakeManifestConnector:
    return FakeManifestConnector()


@pytest.fixture
def qlik_data_product_ref() -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key="dp-1",
        tenant_id=TENANT,
    )
