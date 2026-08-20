"""Shared fixtures for T4.2's capability-manifest tests.

Self-contained within ``tests/manifest`` (T4.2 owns exactly this directory): a minimal
``DatabricksConfig`` builder, the two manifest shapes (with and without a configured SQL
warehouse), and a real ``Connector`` subclass wired to this manifest so the
capability-honesty guards can be exercised end to end, exactly as the SDK's own
``tests/manifest/conftest.py`` does for its Qlik/Databricks-shaped reference fixtures.
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
from qlabs_connector_databricks.config import DatabricksConfig
from qlabs_connector_databricks.manifest import build_manifest

ENDPOINT = "databricks"
TENANT = "tenant-a"


def build_config(**overrides: Any) -> DatabricksConfig:
    """A minimally valid ``DatabricksConfig``, with any field overridden."""
    values: dict[str, Any] = {
        "host": "https://acme.cloud.databricks.com",
        "client_id": "sp-client-abc",
        "client_secret": "sp-secret-value",
    }
    values.update(overrides)
    return DatabricksConfig(**values)


class _ManifestOnlyConnector(ConnectorABC):
    """A real ``Connector`` subclass that exists only to prove the manifest wires into
    the contract's own capability guards (``ensure_supported``/``ensure_writable``) and
    that the inherited write-path defaults refuse with ``CapabilityError``.

    Nothing here touches an API: ``setup``/``healthcheck``/``list_changed``/``read`` are
    trivial stand-ins, and ``create``/``update``/``delete`` are deliberately left
    un-overridden, exactly as the real Databricks connector leaves them (T4.1's
    ``__init__.py`` docstring: "The write path ... is deliberately left untouched").
    """

    name = ENDPOINT
    ConfigModel = DatabricksConfig

    def __init__(self, *, has_sql_warehouse: bool) -> None:
        super().__init__()
        self._manifest = build_manifest(has_sql_warehouse=has_sql_warehouse)

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
def manifest_with_warehouse() -> CapabilityManifest:
    return build_manifest(has_sql_warehouse=True)


@pytest.fixture
def manifest_without_warehouse() -> CapabilityManifest:
    return build_manifest(has_sql_warehouse=False)


@pytest.fixture
def config_with_warehouse() -> DatabricksConfig:
    return build_config(sql_warehouse_id="warehouse-123")


@pytest.fixture
def config_without_warehouse() -> DatabricksConfig:
    return build_config()


@pytest.fixture
def connector_with_warehouse() -> _ManifestOnlyConnector:
    return _ManifestOnlyConnector(has_sql_warehouse=True)


@pytest.fixture
def connector_without_warehouse() -> _ManifestOnlyConnector:
    return _ManifestOnlyConnector(has_sql_warehouse=False)


@pytest.fixture
def schema_ref() -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key="main.sales",
        tenant_id=TENANT,
    )


@pytest.fixture
def table_ref() -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key="main.sales.orders",
        tenant_id=TENANT,
    )
