"""Shared fixtures for T8.5's Databricks VCR contract suite.

**Purpose, stated once so every test file in this directory can point back here.** The
conformance suite (``tests/conformance``, T4.6) and the pilot (T8.1) already prove the
connector *works* against friendly mocks. This directory exists for a narrower and
sharper job: freeze the exact request/response *shapes* this connector depends on as
``vcrpy`` cassettes, so that if Databricks ever renames a field, moves it to a different
nesting level, or changes its type, a test **fails with a clear message** instead of the
connector silently mapping less data than it used to (or, for pagination, silently
truncating a listing). ``test_databricks_contract_vcr_altered_cassettes.py`` is the
proof that this actually happens.

**Every cassette under ``cassettes/`` is hand-authored, not captured from a live
Databricks workspace.** RM-01 is explicitly built without live tenants
(``planning/Roadmap/RM-01-one-way-sync-mvp/decision-databricks-to-qlik-mvp.md``, D8).
Every field, value and URL below is typed by hand from the shapes RS-01 documents --
``databricks-catalog-api-reference.md`` sections 1.2 (schema/table field shapes), 1.3
(UC tags, ``INFORMATION_SCHEMA.*_TAGS``, read-back only over the Statement Execution
API), 3.1 (Statement Execution API), 3.2 (OAuth M2M, form-encoded, HTTP Basic,
``scope=all-apis``) and 3.6 (pagination, ``next_page_token``) -- never observed from a
real workspace. This mirrors exactly what T4.6's own
``tests/conformance/test_read_cassettes.py`` states and does; this directory is
self-contained (its own ``cassettes/`` beside it, nothing imported from another task's
test directory) rather than reusing T4.6's, so this task's guarantee does not depend on
a directory a different task owns staying unchanged.

``vcr_config``'s ``match_on`` is ``(method, scheme, host, port, path, query)`` -- it
deliberately excludes the request body (see
``qlabs_catalog_sync_sdk.conformance.harness.vcr_config``'s own docstring), so the
``body``/``headers`` values recorded in each cassette are realistic documentation of what
the connector actually sends, not a byte-exact replay requirement.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import vcr
import yaml

from qlabs_catalog_sync_sdk.config import ConnectorContext
from qlabs_catalog_sync_sdk.conformance.harness import vcr_config
from qlabs_catalog_sync_sdk.models import EntityType, IdentityRef
from qlabs_connector_databricks.config import DatabricksConfig

ENDPOINT = "databricks"
HOST = "https://contract-vcr.cloud.databricks.com"
METASTORE_ID = "99999999-8888-7777-6666-555555555555"

#: Where this task's hand-authored cassettes live -- owned by T8.5, distinct from
#: T4.6's ``tests/cassettes/``.
CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes"


def build_config(**overrides: Any) -> DatabricksConfig:
    """A minimally valid :class:`DatabricksConfig` for this suite, with any field
    overridden."""
    values: dict[str, Any] = {
        "host": HOST,
        "client_id": "contract-vcr-sp",
        "client_secret": "contract-vcr-secret",
        "catalog_schema_patterns": ["prod.*"],
    }
    values.update(overrides)
    return DatabricksConfig(**values)


def build_ctx(config: DatabricksConfig | None = None) -> ConnectorContext[DatabricksConfig]:
    return ConnectorContext.build(config=config or build_config(), endpoint=ENDPOINT)


def schema_ref(*, native_key: str, full_name: str, metastore_id: str = METASTORE_ID) -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=native_key,
        tenant_id=metastore_id,
        secondary_keys={"full_name": full_name},
    )


def table_ref(*, native_key: str, full_name: str, metastore_id: str = METASTORE_ID) -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key=native_key,
        tenant_id=metastore_id,
        secondary_keys={"full_name": full_name},
    )


@dataclass
class _FakeCatalogInfo:
    name: str = "prod"


@dataclass
class _FakeCatalogsAPI:
    """Stands in for ``databricks.sdk.service.catalog.CatalogsAPI`` -- this suite never
    calls ``healthcheck()``, so its ``.list()`` is never actually invoked, but
    ``Connector.setup()`` always builds a ``WorkspaceClient`` via the injected factory,
    so a minimal double is still needed to avoid constructing a real ``databricks-sdk``
    client (mirrors T4.6's identically-shaped double in
    ``tests/conformance/conftest.py``)."""

    items: list[_FakeCatalogInfo] = field(default_factory=lambda: [_FakeCatalogInfo()])

    def list(self, *, max_results: int | None = None, **_: Any) -> Iterator[_FakeCatalogInfo]:
        return iter(self.items)


@dataclass
class _FakeWorkspaceClient:
    host: str
    token: str
    catalogs: _FakeCatalogsAPI = field(default_factory=_FakeCatalogsAPI)


def _fake_workspace_client_factory(*, host: str, token: str) -> _FakeWorkspaceClient:
    return _FakeWorkspaceClient(host=host, token=token)


@pytest.fixture
def databricks_contract_vcr() -> vcr.VCR:
    """The SDK's pre-configured ``vcr.VCR``, pointed at this task's own ``cassettes/``
    directory. ``record_mode="once"`` means: play back what is already on disk, never
    silently hit a real network -- there is nothing to hit anyway; every cassette here
    is hand-authored."""
    return vcr_config(CASSETTE_DIR)


@pytest.fixture
def fake_workspace_client_factory() -> Callable[..., _FakeWorkspaceClient]:
    """The factory injected into :class:`Connector` so ``setup()`` never constructs a
    real ``databricks-sdk`` client (which does I/O-free construction, per ``auth.py``'s
    own docstring, but there is no reason to depend on that detail here)."""
    return _fake_workspace_client_factory


def write_mutated_cassette(
    *,
    source_name: str,
    dest_path: Path,
    interaction_index: int,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Copy a golden cassette from :data:`CASSETTE_DIR` to ``dest_path``, replacing one
    interaction's JSON response body with ``mutate(original_body)``.

    This is the mechanism every "altered cassette" proof in
    ``test_databricks_contract_vcr_altered_cassettes.py`` uses instead of a hand-edited,
    committed "broken" cassette file: the mutation runs fresh, from the *current* golden
    cassette, on every test invocation -- what makes the DoD's second half a standing
    guarantee rather than a one-off demonstration.
    """
    raw = yaml.safe_load((CASSETTE_DIR / source_name).read_text())
    interaction = raw["interactions"][interaction_index]
    body = json.loads(interaction["response"]["body"]["string"])
    interaction["response"]["body"]["string"] = json.dumps(mutate(body))
    dest_path.write_text(yaml.safe_dump(raw, sort_keys=False))


def vcr_for(directory: Path) -> vcr.VCR:
    """A fresh :class:`vcr.VCR` pointed at an arbitrary directory -- used to replay a
    mutated cassette written to ``tmp_path`` rather than :data:`CASSETTE_DIR`."""
    return vcr_config(directory)
