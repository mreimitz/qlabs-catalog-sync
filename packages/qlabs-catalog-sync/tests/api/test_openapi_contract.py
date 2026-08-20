"""The OpenAPI contract T12.8's generated TypeScript client depends on: the committed
document must describe every route ``create_app`` can mount, must never silently drop a
route group, and must give the console a typed failure path for every operation.

This suite exists because of one footgun in ``api/app.py``. ``create_app`` takes several
**optional** dependencies that default to ``None``, and each ``None`` silently removes a
whole route group from the application -- ``auth=None`` drops the sign-in routes,
``config_service=None`` or ``registry=None`` drops connectors/endpoints/pairs/selection/
preview, ``recorder=None`` drops history, ``store=None`` or ``resolver=None`` drops run
control. An exporter (``scripts/gen_openapi.py``) that builds its app the way T12.1's own
minimal test helper does -- ``health``/``metrics_registry`` only, everything else left at
its default -- produces a schema that is internally consistent, passes its own drift
check, and describes almost nothing: built, tested, and reachable by nothing.

The methodology below is built specifically to catch that class of regression, not just
schema drift in general:

* :func:`_build_reference_app` wires ``create_app`` a **second, independent** time,
  directly in this module, rather than reusing
  ``gen_openapi.build_fully_wired_app``. That duplication is deliberate: if the
  exporter's own wiring regressed (stopped passing ``recorder``, say), reusing its
  builder as this suite's source of "what should be mounted" would make both sides
  shrink together and every test below would stay green. Building the reference
  independently is what makes :func:`test_every_route_group_survives_export` fail when
  the exporter's wiring breaks -- see this task's own report for the mutation that
  proves it.
* Every comparison below is made through ``FastAPI.app.openapi()`` (a plain in-process
  call, no ASGI transport), because ``auth.py`` makes ``/openapi.json`` itself
  deliberately non-public -- see :func:`test_schema_and_docs_endpoints_are_not_publicly_reachable`.
  An exporter that tried to fetch the schema by starting a server and issuing a GET to
  ``/openapi.json`` would get a ``401`` the moment a real ``ConsoleAuth`` is wired in.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from qlabs_catalog_sync.api.app import create_app
from qlabs_catalog_sync.api.auth import (
    AdminCredential,
    ConsoleAuth,
    ScryptParams,
    hash_password,
    schema_and_docs_paths,
)
from qlabs_catalog_sync.api.errors import ErrorModel
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.observability import HealthRegistry
from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.config import NullMetrics

# scripts/ ships operator tooling, not a workspace package -- mypy covers it (see
# pyproject.toml's [tool.mypy] `files`) but pytest does not collect it and nothing on
# the normal import path can see it. Put it on sys.path the same way this package's own
# suites reach a sibling test directory (e.g. tests/api/test_run_control.py's
# `sys.path.insert(0, str(Path(__file__).resolve().parent))` for sync_pair_helpers), so
# this suite drives the real exporter instead of reimplementing it.
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import gen_openapi  # noqa: E402

_REFERENCE_PASSWORD = "reference-app-password-not-a-real-secret"

#: Every HTTP method OpenAPI's Path Item Object can carry an Operation Object under.
_OPERATION_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)


def _build_reference_app(store: StateStore) -> FastAPI:
    """An independently-wired, fully-mounted app -- built with its own direct calls to
    ``create_app``, never through :func:`gen_openapi.build_fully_wired_app`. See the
    module docstring for why the duplication is load-bearing rather than incidental.
    """
    registry = ConnectorRegistry({}, {})
    config_service = ConfigService(store.engine, registry)
    resolver = IdentityResolver(
        store,
        review_path=Path(tempfile.gettempdir()) / "test-openapi-contract-identity-review.json",
    )
    recorder = RunRecorder.from_store(store)
    credential = AdminCredential.from_password_hash(
        hash_password(_REFERENCE_PASSWORD, params=ScryptParams(log_n=14)),
        username="reference",
    )
    auth = ConsoleAuth(credential=credential)
    return create_app(
        health=HealthRegistry(),
        metrics_registry=CollectorRegistry(),
        auth=auth,
        config_service=config_service,
        registry=registry,
        store=store,
        resolver=resolver,
        recorder=recorder,
        metrics=NullMetrics(),
    )


@pytest.fixture
def state_store(tmp_path: Path) -> Iterator[StateStore]:
    """A freshly migrated, disposable SQLite state store -- backs :func:`_build_reference_app`."""
    db_url = f"sqlite:///{tmp_path / 'state.db'}"
    upgrade_to_head(db_url)
    store = StateStore.from_url(db_url)
    yield store
    store.engine.dispose()


def test_every_route_group_survives_export(state_store: StateStore) -> None:
    """The certification test for this task's whole reason to exist: every path an
    independently-wired reference app registers must appear in what
    ``gen_openapi.export_schema()`` produces, and vice versa.

    Fails if ``scripts/gen_openapi.py`` ever builds its app with a dependency missing
    (the ``create_app`` footgun the module docstring describes) -- the reference app
    still has the route, the export does not, and the set difference names exactly
    which paths went missing rather than reporting a vague count mismatch.
    """
    reference_app = _build_reference_app(state_store)
    reference_paths = set(reference_app.openapi()["paths"].keys())

    exported_paths = set(gen_openapi.export_schema()["paths"].keys())

    missing = reference_paths - exported_paths
    assert not missing, (
        "gen_openapi.py's exported document is missing routes present on an "
        f"independently-wired reference app -- a create_app dependency is probably "
        f"None where it should be a real object (see api/app.py's own docstring for "
        f"the footgun): {sorted(missing)}"
    )
    unexpected = exported_paths - reference_paths
    assert not unexpected, (
        "gen_openapi.py's exported document has routes the independently-wired "
        f"reference app does not -- the two builders have drifted apart: {sorted(unexpected)}"
    )


def test_healthz_and_metrics_are_in_the_document() -> None:
    """C8: the console, the REST API and ``/healthz``/``/metrics`` share one origin --
    the OpenAPI document is the contract for the whole surface, not only ``/api``."""
    schema = gen_openapi.export_schema()
    assert "/healthz" in schema["paths"], "the exported document is missing /healthz"
    assert "get" in schema["paths"]["/healthz"]
    assert "/metrics" in schema["paths"], "the exported document is missing /metrics"
    assert "get" in schema["paths"]["/metrics"]


def test_error_model_is_a_documented_component() -> None:
    """``errors.py``'s ``ErrorModel`` -- the one JSON shape every failure this API can
    produce returns -- must be a real, complete entry in ``components.schemas``, not
    just a runtime-only shape the generated TypeScript client never learns about."""
    schema = gen_openapi.export_schema()
    schemas = schema["components"]["schemas"]
    assert "ErrorModel" in schemas, "ErrorModel is not in components.schemas"
    documented_properties = set(schemas["ErrorModel"].get("properties", {}))
    assert set(ErrorModel.model_fields) <= documented_properties, (
        "the documented ErrorModel component does not carry every field the real "
        f"ErrorModel does: documented={sorted(documented_properties)}, "
        f"actual={sorted(ErrorModel.model_fields)}"
    )


def test_every_route_declares_the_typed_error_response() -> None:
    """``errors.py``'s module docstring: an error shape ``API_ERROR_RESPONSES``
    *declares in openapi.json*, not just produces at runtime, is what T12.8's generated
    TypeScript client needs to give the console a typed failure path rather than an
    untyped ``catch``. Every operation this suite finds must reference ``ErrorModel`` as
    its ``"default"`` response.

    If this test ever fails on a real route (rather than a deliberate mutation), the fix
    is in the route module that omitted ``responses=API_ERROR_RESPONSES`` -- outside this
    task's owned paths; see the task report for how that must be handled.
    """
    schema = gen_openapi.export_schema()
    undocumented: list[str] = []
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if method not in _OPERATION_METHODS:
                continue
            default_response = operation.get("responses", {}).get("default")
            ref = None
            if default_response is not None:
                ref = (
                    default_response.get("content", {})
                    .get("application/json", {})
                    .get("schema", {})
                    .get("$ref")
                )
            if ref != "#/components/schemas/ErrorModel":
                undocumented.append(f"{method.upper()} {path}")
    assert not undocumented, (
        "these operations do not declare API_ERROR_RESPONSES as their `default` "
        f"response, so the generated TypeScript client gives them no typed failure "
        f"path: {undocumented}"
    )


def test_committed_document_matches_a_fresh_export() -> None:
    """Definition of done: 'the committed OpenAPI document matches the running app.'
    This is the same byte-for-byte comparison ``scripts/gen_openapi.py --check`` makes,
    run here as a named pytest so this task's own verify command --
    ``pytest -q .../test_openapi_contract.py`` on its own -- fails on drift without
    shelling out to the script."""
    committed = gen_openapi.DEFAULT_OUTPUT.read_text(encoding="utf-8")
    fresh = gen_openapi.render(gen_openapi.export_schema())
    assert committed == fresh, (
        f"{gen_openapi.DEFAULT_OUTPUT} does not match a fresh export -- run "
        "`uv run python scripts/gen_openapi.py`, regenerate the TypeScript client with "
        "`scripts/gen_api_client.sh`, and commit both."
    )


def test_schema_and_docs_endpoints_are_not_publicly_reachable(state_store: StateStore) -> None:
    """Why ``gen_openapi.py`` never fetches the schema over HTTP: ``auth.py``
    deliberately carves ``/openapi.json``, ``/docs`` and ``/redoc`` back **out** of the
    "any safe-method request outside the API prefix reaches the console shell"
    allowance (see :func:`~qlabs_catalog_sync.api.auth.schema_and_docs_paths` and
    :func:`~qlabs_catalog_sync.api.auth.install_auth`'s own docstrings) -- an
    unauthenticated request for any of them gets a ``401``, exactly like any other
    unauthenticated request under the API prefix. An exporter that started a server and
    issued a GET to ``/openapi.json`` against a real deployment would therefore fail
    closed, which is exactly what this test proves and why
    :func:`gen_openapi.export_schema` calls ``FastAPI.app.openapi()`` in-process instead.
    """
    reference_app = _build_reference_app(state_store)
    protected = schema_and_docs_paths(reference_app)
    assert "/openapi.json" in protected
    assert "/docs" in protected
    assert "/redoc" in protected

    client = TestClient(reference_app, raise_server_exceptions=False)
    for path in sorted(protected):
        response = client.get(path)
        assert response.status_code == 401, (
            f"{path} should require a session (auth.py carves it out of the console-shell "
            f"allowance), got {response.status_code}"
        )
