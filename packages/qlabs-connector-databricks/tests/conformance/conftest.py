"""Shared fixtures and hand-authored Unity Catalog payload builders for T4.6's
conformance suite: the SDK's ``ConnectorConformanceSuite`` run against a real
``Connector`` instance in both decision-D6 manifest shapes, plus the connector-specific
supplementary tests this task adds beside it (``test_write_refusal.py``,
``test_manifest_read_honesty.py``, ``test_read_cassettes.py``).

Self-contained within ``tests/conformance`` -- T4.6 owns exactly this directory and
``tests/cassettes/`` and nothing else in this package -- matching every other per-task
conftest here (``tests/manifest/conftest.py``, ``tests/auth/conftest.py``, ...): its own
minimal ``DatabricksConfig`` builder and fake ``databricks-sdk`` ``WorkspaceClient``
double, so this suite never touches a real workspace, another task's owned test module,
or the live network.

Every payload builder below (:func:`make_schema`, :func:`make_table`) produces fields
drawn from RS-01 (``planning/Research/RS-01-databricks-catalog-api/outputs/
databricks-catalog-api-reference.md``, sections 1.2 and 4.1) -- comment/owner/
properties/audit-stamp shapes, epoch-millisecond timestamps, ``full_name`` composition
-- never from a live tenant (agent-guide.md / decision-databricks-to-qlik-mvp.md: "The
MVP is also built without live tenants"). The same builders back both the respx-mocked
tests in this directory and the hand-authored cassette bodies under ``tests/cassettes/``,
so the two stay textually consistent instead of drifting apart.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import vcr

from qlabs_catalog_sync_sdk.config import ConnectorContext
from qlabs_catalog_sync_sdk.conformance.harness import vcr_config
from qlabs_connector_databricks import Connector
from qlabs_connector_databricks.config import DatabricksConfig

ENDPOINT = "databricks"
HOST = "https://acme.cloud.databricks.com"
TOKEN_URL = f"{HOST}/oidc/v1/token"
SCHEMAS_URL = f"{HOST}/api/2.1/unity-catalog/schemas"
TABLES_URL = f"{HOST}/api/2.1/unity-catalog/tables"
STATEMENTS_URL = f"{HOST}/api/2.0/sql/statements"

#: A plausible Unity Catalog metastore id (UUID-shaped, per RS-01 section 4.1 -- ids are
#: opaque and stable, never a derived name). Hand-chosen, not read off any real tenant.
METASTORE_ID = "11111111-2222-3333-4444-555555555555"

#: Where the hand-authored cassettes live -- the sibling directory T4.6 also owns.
CASSETTE_DIR = Path(__file__).resolve().parent.parent / "cassettes"

DEFAULT_TS = 1_700_000_000_000  # 2023-11-14T22:13:20Z, epoch milliseconds (RS-01 4.1/1.2)


# --------------------------------------------------------------------------------------
# DatabricksConfig / ConnectorContext builders
# --------------------------------------------------------------------------------------


def build_config(**overrides: Any) -> DatabricksConfig:
    """A minimally valid :class:`DatabricksConfig`, with any field overridden."""
    values: dict[str, Any] = {
        "host": HOST,
        "client_id": "sp-client-conformance",
        "client_secret": "sp-secret-conformance",
    }
    values.update(overrides)
    return DatabricksConfig(**values)


def build_ctx(config: DatabricksConfig | None = None) -> ConnectorContext[DatabricksConfig]:
    return ConnectorContext.build(config=config or build_config(), endpoint=ENDPOINT)


# --------------------------------------------------------------------------------------
# Fake databricks-sdk WorkspaceClient -- healthcheck()'s probe, never a live workspace
# --------------------------------------------------------------------------------------


@dataclass
class _FakeCatalogInfo:
    name: str = "prod"


@dataclass
class _FakeCatalogsAPI:
    """Stands in for ``databricks.sdk.service.catalog.CatalogsAPI`` -- just enough
    surface for :func:`qlabs_connector_databricks.auth.probe_unity_catalog` (a
    ``.catalogs.list(...)``) to run without a real vendor client or any network access,
    mirroring ``tests/auth/conftest.py``'s identically-shaped (but not imported, per this
    module's own docstring) double.
    """

    items: list[_FakeCatalogInfo] = field(default_factory=lambda: [_FakeCatalogInfo()])

    def list(self, *, max_results: int | None = None, **_: Any) -> Iterator[_FakeCatalogInfo]:
        return iter(self.items)


@dataclass
class _FakeWorkspaceClient:
    """Stands in for ``databricks.sdk.WorkspaceClient``. Construction does no I/O (see
    ``auth.py``'s own docstring), so this fake never needs to fake a network call
    either -- only the one attribute access ``probe_unity_catalog`` performs."""

    host: str
    token: str
    catalogs: _FakeCatalogsAPI = field(default_factory=_FakeCatalogsAPI)


def _fake_workspace_client_factory(*, host: str, token: str) -> _FakeWorkspaceClient:
    return _FakeWorkspaceClient(host=host, token=token)


# --------------------------------------------------------------------------------------
# connector fixture builder -- respx-mocked OAuth only for the duration of setup()
# --------------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def setup_connector(
    *, sql_warehouse_id: str | None = None, mock_oauth: bool = True
) -> AsyncIterator[Connector]:
    """Build, ``setup()`` and yield a real :class:`Connector` -- the exact shape every
    real connector's conformance fixture takes (see the SDK's own
    ``tests/conformance/test_suite_against_fake_connector.py`` module docstring: "build
    it, setup() it, yield it, close() it").

    When ``mock_oauth`` is true (the default), respx is active for the duration of
    ``setup()``'s OAuth token fetch only, in a router this function opens and closes
    itself -- not for the whole test via the ``respx_mock`` pytest fixture. That
    matters: the conformance suite's own ``assert_no_http_calls``/``capture_requests``
    (``qlabs_catalog_sync_sdk.conformance.harness``) each open their own fresh
    ``respx.mock()`` router inside a test method that uses this fixture, and confining
    this router to ``setup()`` alone means those never nest inside it.

    Pass ``mock_oauth=False`` when the caller has already arranged something else to
    answer the token POST -- concretely, ``test_read_cassettes.py``'s end-to-end test,
    where a ``vcrpy`` cassette is the active httpx interceptor: nesting this function's
    own ``respx.mock()`` *inside* an active ``vcr.use_cassette()`` does not compose --
    both patch httpx's transport, and vcr (already active, entered by the caller first)
    wins, answering the request from the cassette before respx's own route ever sees it,
    which then fails respx's ``assert_all_called`` for a route the cassette silently
    satisfied instead. With ``mock_oauth=False`` this function does not touch httpx at
    all; the cassette (or whatever else is active) must itself carry an interaction for
    ``POST /oidc/v1/token``.

    The OAuth token minted here (``expires_in=3600``) stays cached and valid for the
    lifetime of one (fast, sub-second) test, so ``healthcheck()`` -- which rebuilds the
    ``WorkspaceClient`` from a freshly-*fetched-or-cached* token on every call -- reuses
    it without a second HTTP round trip; the fake ``WorkspaceClient`` factory means the
    probe itself never touches a network either.
    """
    factory = _fake_workspace_client_factory
    async with httpx.AsyncClient() as transport:
        connector = Connector(workspace_client_factory=factory, transport=transport)
        config = build_config(sql_warehouse_id=sql_warehouse_id)
        if mock_oauth:
            with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
                router.post(TOKEN_URL).mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "access_token": "conformance-kit-token",
                            "token_type": "Bearer",
                            "expires_in": 3600,
                        },
                    )
                )
                await connector.setup(build_ctx(config))
        else:
            await connector.setup(build_ctx(config))
        try:
            yield connector
        finally:
            await connector.close()


# --------------------------------------------------------------------------------------
# Realistic Unity Catalog payload builders (RS-01 sections 1.2 / 4.1)
# --------------------------------------------------------------------------------------


def make_schema(
    catalog_name: str,
    name: str,
    *,
    comment: str = "Sales domain schema, owned by data engineering.",
    owner: str = "data-eng@acme.com",
    properties: dict[str, str] | None = None,
    schema_id: str = "b4b7c3d2-4e9a-4b1b-9a3f-2e6d9c8a7f10",
    metastore_id: str = METASTORE_ID,
    updated_at: int = DEFAULT_TS,
) -> dict[str, Any]:
    """Shaped like ``databricks.sdk.service.catalog.SchemaInfo.as_dict()`` /
    ``GET /api/2.1/unity-catalog/schemas`` (RS-01 section 1.2), hand-derived from the
    documented field list -- never captured from a real tenant."""
    return {
        "catalog_name": catalog_name,
        "name": name,
        "full_name": f"{catalog_name}.{name}",
        "comment": comment,
        "owner": owner,
        "properties": properties if properties is not None else {"team": "sales"},
        "schema_id": schema_id,
        "metastore_id": metastore_id,
        "created_at": updated_at,
        "created_by": "admin@acme.com",
        "updated_at": updated_at,
        "updated_by": owner,
    }


def make_table(
    catalog_name: str,
    schema_name: str,
    name: str,
    *,
    comment: str = "One row per customer order.",
    owner: str = "data-eng@acme.com",
    table_type: str = "MANAGED",
    data_source_format: str | None = "DELTA",
    table_id: str = "c5d8e4f3-5f0b-4c2c-8b4a-3f7e0d9b8a21",
    metastore_id: str = METASTORE_ID,
    columns: list[dict[str, Any]] | None = None,
    properties: dict[str, str] | None = None,
    updated_at: int = DEFAULT_TS,
) -> dict[str, Any]:
    """Shaped like ``databricks.sdk.service.catalog.TableInfo.as_dict()`` /
    ``GET /api/2.1/unity-catalog/tables`` (RS-01 sections 1.2, 4.3), hand-derived from
    the documented field list -- never captured from a real tenant."""
    payload: dict[str, Any] = {
        "catalog_name": catalog_name,
        "schema_name": schema_name,
        "name": name,
        "full_name": f"{catalog_name}.{schema_name}.{name}",
        "table_type": table_type,
        "columns": columns
        if columns is not None
        else [
            {
                "name": "order_id",
                "type_name": "BIGINT",
                "nullable": False,
                "position": 0,
                "comment": "Primary key",
            },
            {"name": "customer_id", "type_name": "BIGINT", "nullable": False, "position": 1},
            {"name": "order_total", "type_name": "DECIMAL", "nullable": True, "position": 2},
        ],
        "comment": comment,
        "owner": owner,
        "properties": properties if properties is not None else {"delta.minReaderVersion": "1"},
        "table_id": table_id,
        "metastore_id": metastore_id,
        "created_at": updated_at,
        "created_by": "admin@acme.com",
        "updated_at": updated_at,
        "updated_by": owner,
    }
    if data_source_format is not None:
        payload["data_source_format"] = data_source_format
    return payload


# --------------------------------------------------------------------------------------
# respx route helpers speaking the exact params read.py sends
# --------------------------------------------------------------------------------------


def mock_schema_list(
    router: respx.MockRouter, *, catalog_name: str, schemas: list[dict[str, Any]]
) -> respx.Route:
    """One-page ``GET /schemas?catalog_name=...&max_results=100`` response -- matches
    ``read.py``'s ``_iter_schemas_in_catalog`` call shape exactly (see that module)."""
    return router.get(
        SCHEMAS_URL, params={"catalog_name": catalog_name, "max_results": "100"}
    ).mock(return_value=httpx.Response(200, json={"schemas": schemas}))


def mock_table_list(
    router: respx.MockRouter,
    *,
    catalog_name: str,
    schema_name: str,
    tables: list[dict[str, Any]],
) -> respx.Route:
    """One-page ``GET /tables?catalog_name=...&schema_name=...&max_results=100``
    response -- matches ``read.py``'s ``_iter_tables_in_schema`` call shape."""
    return router.get(
        TABLES_URL,
        params={"catalog_name": catalog_name, "schema_name": schema_name, "max_results": "100"},
    ).mock(return_value=httpx.Response(200, json={"tables": tables}))


def mock_get_table(
    router: respx.MockRouter, *, full_name: str, table: dict[str, Any]
) -> respx.Route:
    """``GET /tables/{full_name}`` -- matches ``read.py``'s ``_get_table`` single fetch."""
    return router.get(f"{TABLES_URL}/{full_name}").mock(
        return_value=httpx.Response(200, json=table)
    )


# --------------------------------------------------------------------------------------
# vcrpy: hand-authored cassette playback (test_read_cassettes.py)
# --------------------------------------------------------------------------------------


@pytest.fixture
def databricks_vcr() -> vcr.VCR:
    """The SDK's own pre-configured ``vcr.VCR`` (``qlabs_catalog_sync_sdk.conformance
    .harness.vcr_config``), pointed at this task's owned ``tests/cassettes/`` directory.
    ``record_mode="once"`` (the harness helper's default) means: play back the
    hand-authored cassette that is already on disk, never silently re-hit a network."""
    return vcr_config(CASSETTE_DIR)
