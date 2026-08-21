"""Shared fixtures for T6.2's capability-manifest tests.

Self-contained within ``tests/manifest`` (T6.2 owns exactly this directory): the built
manifest, and a real ``Connector`` subclass wired to it so the capability-honesty guards
can be exercised end to end, exactly as the SDK's own ``tests/manifest/conftest.py`` and
the Databricks connector's ``tests/manifest/conftest.py`` do for their reference
fixtures. Unlike Databricks, there is only one manifest shape here — ``build_manifest()``
takes no arguments (manifest.py's own module docstring: "config-independence").
"""

from __future__ import annotations

from typing import Any

import pytest

from qlabs_catalog_sync_sdk.contract import Connector as ConnectorABC
from qlabs_catalog_sync_sdk.contract import (
    ConnectorContext,
    HealthStatus,
    ListChangedResult,
    Watermark,
)
from qlabs_catalog_sync_sdk.manifest import CapabilityManifest
from qlabs_catalog_sync_sdk.models import EntityType, IdentityRef, NeutralEntity
from qlabs_connector_snowflake.auth import SnowflakeConfig
from qlabs_connector_snowflake.manifest import build_manifest

ENDPOINT = "snowflake"
TENANT = "tenant-a"


class _ManifestOnlyConnector(ConnectorABC):
    """A real ``Connector`` subclass that exists only to prove the manifest wires into
    the contract's own capability guards (``ensure_supported``/``ensure_writable``) and
    that the inherited write-path defaults refuse with ``CapabilityError``.

    Nothing here touches an API: ``setup``/``healthcheck``/``list_changed``/``read`` are
    trivial stand-ins, and ``create``/``update``/``delete`` are deliberately left
    un-overridden, exactly as the real Snowflake connector leaves them.
    """

    name = ENDPOINT
    ConfigModel = SnowflakeConfig

    def __init__(self) -> None:
        super().__init__()
        self._manifest = build_manifest()

    def capabilities(self) -> CapabilityManifest:
        return self._manifest

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        return None

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus.healthy(self.name)

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        return ListChangedResult.empty(since)

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        raise NotImplementedError("not exercised by these tests")


@pytest.fixture
def manifest() -> CapabilityManifest:
    return build_manifest()


@pytest.fixture
def connector() -> _ManifestOnlyConnector:
    return _ManifestOnlyConnector()


@pytest.fixture
def data_product_ref() -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key="SALES_DB.SALES_SCHEMA",
        tenant_id=TENANT,
    )


@pytest.fixture
def dataset_ref() -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key="SALES_DB.SALES_SCHEMA.ORDERS",
        tenant_id=TENANT,
    )
