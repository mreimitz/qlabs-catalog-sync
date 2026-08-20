"""Shared fixtures for the Databricks connector's auth/config/setup/healthcheck tests.

Mirrors the SDK's own ``tests/auth/conftest.py`` (an ``httpx.AsyncClient`` transport for
``respx`` to intercept) and adds the two pieces specific to this connector: a valid
``DatabricksConfig`` factory and a fake, injectable ``databricks-sdk`` ``WorkspaceClient``
double so ``healthcheck()`` is testable without a live workspace or a real vendor SDK
client (there is no live Databricks workspace for this build; see
``planning/Roadmap/RM-01-one-way-sync-mvp/decision-databricks-to-qlik-mvp.md``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from qlabs_catalog_sync_sdk.config import ConnectorContext, ManualClock
from qlabs_connector_databricks.config import DatabricksConfig

TOKEN_URL = "https://acme.cloud.databricks.com/oidc/v1/token"
CATALOGS_URL = "https://acme.cloud.databricks.com/api/2.1/unity-catalog/catalogs"


@pytest.fixture
async def transport() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def build_config(**overrides: Any) -> DatabricksConfig:
    """A minimally valid :class:`DatabricksConfig`, with any field overridden."""
    values: dict[str, Any] = {
        "host": "https://acme.cloud.databricks.com",
        "client_id": "sp-client-abc",
        "client_secret": "sp-secret-value",
    }
    values.update(overrides)
    return DatabricksConfig(**values)


def build_ctx(config: DatabricksConfig | None = None) -> ConnectorContext[DatabricksConfig]:
    return ConnectorContext.build(
        config=config or build_config(), endpoint="databricks", clock=ManualClock()
    )


@dataclass
class FakeCatalogInfo:
    name: str = "sales"


@dataclass
class FakeCatalogsAPI:
    """Stands in for ``databricks.sdk.service.catalog.CatalogsAPI``.

    ``list()`` is what :func:`qlabs_connector_databricks.auth.probe_unity_catalog`
    calls. Configure ``error`` to make it raise (a ``databricks.sdk.errors.*``
    instance, or a transport-level ``OSError``) instead of returning items, exactly
    the shape a real failed call would take.
    """

    items: list[FakeCatalogInfo] = field(default_factory=lambda: [FakeCatalogInfo()])
    error: Exception | None = None
    calls: list[int | None] = field(default_factory=list)

    def list(self, *, max_results: int | None = None, **_: Any) -> Iterator[FakeCatalogInfo]:
        self.calls.append(max_results)
        if self.error is not None:
            raise self.error
        return iter(self.items)


@dataclass
class FakeWorkspaceClient:
    """Stands in for ``databricks.sdk.WorkspaceClient`` — just enough surface for
    ``probe_unity_catalog`` (a ``.catalogs.list(...)``) to run against."""

    host: str
    token: str
    catalogs: FakeCatalogsAPI = field(default_factory=FakeCatalogsAPI)


@dataclass
class RecordingWorkspaceClientFactory:
    """A ``WorkspaceClientFactory`` double that records every ``(host, token)`` it was
    built with, so a test can assert the token obtained from the (respx-mocked) OAuth
    flow is the one that actually reached client construction."""

    error: Exception | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    last_client: FakeWorkspaceClient | None = None

    def __call__(self, *, host: str, token: str) -> FakeWorkspaceClient:
        self.calls.append((host, token))
        catalogs = FakeCatalogsAPI(error=self.error)
        client = FakeWorkspaceClient(host=host, token=token, catalogs=catalogs)
        self.last_client = client
        return client
