"""Dry-run and run-control routes (WP12/T12.6): ``/pairs/{id}/dry-run``, ``/run-now``,
``/pause``, ``/resume``, ``/run-status``.

Drives real HTTP through a real ``create_app`` app, a real
:class:`~qlabs_catalog_sync.configstore.service.ConfigService` and a real
:class:`~qlabs_catalog_sync.state.store.StateStore` sharing one migrated SQLite
database (mirrors ``cli/serve_command.py``'s own ``config_service = ConfigService(
store.engine, ...)``), and real :class:`~qlabs_catalog_sync_sdk.testing.FakeConnector`
instances playing the source and target -- no mocks. ``run_control.py``'s own
``build_run_control_router`` is not mounted by ``create_app`` (three WP12 route tasks
land in parallel and would collide on ``api/app.py``; see that module's own docstring),
so :func:`_mount_run_control` below builds the app the same way ``create_app`` will
once the orchestrator wires it in, then mounts this task's router itself -- including
re-ordering routes so this router's one ``GET`` (``run-status``) is not shadowed by
``static.py``'s catch-all SPA fallback, which ``create_app`` already registered last
(mirrors ``tests/api/api_helpers.py``'s own ``add_raising_route`` trick, for the same
reason).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from qlabs_catalog_sync.api.app import API_PREFIX, create_app
from qlabs_catalog_sync.api.auth import (
    AUTH_SESSION_ROUTE,
    CSRF_HEADER,
    AdminCredential,
    ConsoleAuth,
)
from qlabs_catalog_sync.api.routes.run_control import build_run_control_router
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.configstore.types import MatcherKind, RuleScope, SelectionDecision
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.observability import HealthRegistry
from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.models import DataProduct, DataProductStatus, TextField
from qlabs_catalog_sync_sdk.testing import (
    FakeConnector,
    databricks_shaped_manifest,
    qlik_shaped_manifest,
)

# pytest runs with --import-mode=importlib, which does not put a test directory on
# sys.path -- put this one on it so sync_pair_helpers (owned by T12.4, not this task,
# but its create_endpoint/create_pair/sign_in builders are exactly this suite's own
# idiom -- see that module's docstring) is importable the same way test_pairs.py uses it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sync_pair_helpers import (  # noqa: E402
    PASSWORD,
    PASSWORD_HASH,
    USERNAME,
    create_endpoint,
    create_pair,
    sign_in,
)

SOURCE_ENDPOINT = "databricks"
TARGET_ENDPOINT = "qlik"
SESSION_PATH = f"{API_PREFIX}{AUTH_SESSION_ROUTE}"

#: A fixed clock so ``generated_at``/run-history timestamps are exact, not "close enough".
NOW: datetime = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


def _wrap_as_class(instance: Connector) -> type[Connector]:
    """Wrap an already-built connector instance as a zero-argument-constructible class.

    A file-local copy of ``tests/cli/cli_helpers.py``'s ``wrap_as_class`` (not owned by
    this file; this suite lives in a different test directory) -- see that module's
    docstring for the full reasoning. This is what lets ``run_control.py`` build a
    connector the production way (``registry.get_connector(name)()``, zero arguments)
    while this suite still holds a handle on the exact instance to seed and assert on.
    """
    base = type(instance)

    class _Wrapped(base):  # type: ignore[misc, valid-type]
        def __new__(cls) -> Connector:
            return instance

    return _Wrapped


def _mount_run_control(app: FastAPI, router: APIRouter) -> None:
    """Mount ``router`` under :data:`API_PREFIX`, ahead of ``static.py``'s SPA catch-all.

    ``create_app`` always registers ``GET /{full_path:path}`` last (``static.py``'s own
    module docstring: Starlette matches routes in insertion order, not by specificity).
    ``app.include_router`` appends, which would land this router's one ``GET``
    (``run-status``) *after* that catch-all and make it permanently unreachable. Mirrors
    ``tests/api/api_helpers.py``'s ``add_raising_route`` fix for the identical problem.
    """
    before = len(app.router.routes)
    app.include_router(router, prefix=API_PREFIX)
    routes = app.router.routes
    added = routes[before:]
    del routes[before:]
    insert_at = len(routes) - 1  # just before the catch-all
    routes[insert_at:insert_at] = added


# --------------------------------------------------------------------------------------
# Seed data
# --------------------------------------------------------------------------------------


def _seed_one_product(source: FakeConnector, *, native_key: str = "sales.orders") -> None:
    source.seed(
        DataProduct(
            name="orders",
            description=TextField.plain("Orders data product"),
            status=DataProductStatus.ACTIVE,
        ),
        native_key=native_key,
    )


# --------------------------------------------------------------------------------------
# App-building fixtures (mirrors tests/api/test_pairs.py's own conventions, extended
# with the store/resolver/recorder this router needs and does not own creating)
# --------------------------------------------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'state.db'}"
    upgrade_to_head(url)
    return url


@pytest.fixture
def source_connector() -> FakeConnector:
    return FakeConnector.read_only_source(
        name=SOURCE_ENDPOINT, manifest=databricks_shaped_manifest()
    )


@pytest.fixture
def target_connector() -> FakeConnector:
    return FakeConnector.write_target(name=TARGET_ENDPOINT, manifest=qlik_shaped_manifest())


@pytest.fixture
def registry(source_connector: FakeConnector, target_connector: FakeConnector) -> ConnectorRegistry:
    return ConnectorRegistry(
        {
            SOURCE_ENDPOINT: _wrap_as_class(source_connector),
            TARGET_ENDPOINT: _wrap_as_class(target_connector),
        },
        {},
    )


@pytest.fixture
def store(db_url: str) -> Iterator[StateStore]:
    built = StateStore.from_url(db_url)
    yield built
    asyncio.run(built.aclose())


@pytest.fixture
def config_service(store: StateStore, registry: ConnectorRegistry) -> ConfigService:
    # Shares store's engine -- one database, one connection pool -- exactly how
    # cli/serve_command.py builds the two together in production.
    return ConfigService(store.engine, registry)


@pytest.fixture
def resolver(store: StateStore, tmp_path: Path) -> IdentityResolver:
    return IdentityResolver(store, review_path=tmp_path / "identity-review.json")


@pytest.fixture
def recorder(store: StateStore) -> RunRecorder:
    return RunRecorder.from_store(store)


@pytest.fixture
def auth() -> ConsoleAuth:
    credential = AdminCredential.from_password_hash(PASSWORD_HASH, username=USERNAME)
    return ConsoleAuth(credential=credential)


@pytest.fixture
def app(
    config_service: ConfigService,
    registry: ConnectorRegistry,
    store: StateStore,
    resolver: IdentityResolver,
    recorder: RunRecorder,
    auth: ConsoleAuth,
) -> FastAPI:
    built = create_app(
        health=HealthRegistry(),
        metrics_registry=CollectorRegistry(),
        auth=auth,
        config_service=config_service,
        registry=registry,
    )
    router = build_run_control_router(
        config_service=config_service,
        registry=registry,
        store=store,
        resolver=resolver,
        recorder=recorder,
        clock=lambda: NOW,
    )
    _mount_run_control(built, router)
    return built


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def signed_in_client(client: TestClient) -> tuple[TestClient, str]:
    return client, sign_in(client)


@pytest.fixture
async def async_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _sign_in_async(client: httpx.AsyncClient) -> str:
    """``sync_pair_helpers.sign_in``, for an ``httpx.AsyncClient`` instead of a
    ``TestClient`` -- needed only by the concurrency test below, which must issue two
    requests genuinely concurrently (a synchronous ``TestClient`` cannot)."""
    response = await client.post(SESSION_PATH, json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()["csrf_token"]
    assert isinstance(token, str) and token
    return token


async def _include_everything(config_service: ConfigService, pair_id: uuid.UUID) -> None:
    """One object-scope, include-everything rule (C3) -- without it, C3's own fail-closed
    default (``DEFAULT_DECISION``, see ``selection/rules.py``) excludes every candidate,
    since a freshly created pair starts with no selection rules at all. This suite is not
    about selection (that is T11.x's/T12.4's own test surface); it just needs the seeded
    object to be in scope so a cycle has something to plan against."""
    await config_service.create_selection_rule(
        pair_id=pair_id,
        scope=RuleScope.OBJECT,
        decision=SelectionDecision.INCLUDE,
        matcher_kind=MatcherKind.GLOB,
        pattern="*.*",
        actor="test-setup",
        now=NOW,
    )


def _setup_pair(
    client: TestClient, csrf: str, config_service: ConfigService, *, enabled: bool = True
) -> str:
    """Register the source/target endpoints and one enabled, fully-selected sync pair;
    return its id."""
    create_endpoint(client, csrf, name="src", connector=SOURCE_ENDPOINT, role="source")
    create_endpoint(client, csrf, name="tgt", connector=TARGET_ENDPOINT, role="target")
    pair = create_pair(
        client,
        csrf,
        name="db-to-qlik",
        source="src",
        target="tgt",
        target_space="Sales Space",
        entity_types=["data_product"],
        enabled=enabled,
    )
    pair_id = uuid.UUID(pair["id"])
    asyncio.run(_include_everything(config_service, pair_id))
    return str(pair_id)


# ========================================================================================
# Dry run: the plan, and zero mutations -- proved against the connector's own call log
# ========================================================================================


def test_dry_run_returns_the_plan_and_the_target_records_zero_writes(
    signed_in_client: tuple[TestClient, str],
    config_service: ConfigService,
    source_connector: FakeConnector,
    target_connector: FakeConnector,
) -> None:
    client, csrf = signed_in_client
    pair_id = _setup_pair(client, csrf, config_service)
    _seed_one_product(source_connector)

    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/dry-run",
        json={"create_missing": True},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pair_id"] == pair_id
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["dry_run"] is True
    assert run["committed"] is False
    assert run["counts"]["created"] == 1

    records = run["records"]
    assert len(records) == 1
    assert records[0]["native_key"] == "sales.orders"
    assert records[0]["outcome"] == "created"
    assert "name" in records[0]["changed_fields"]
    # D7: activation is withheld by default -- an "unresolved reference" of a kind, in
    # the sense that the plan names exactly what it would NOT carry across and why.
    withheld = {item["field"] for item in records[0]["withheld"]}
    assert "status" in withheld

    # The whole safety story, proved against the connector's own recorded calls -- not
    # against the "dry_run" flag in the response body, which the route could get wrong
    # independently of whether it actually wrote anything.
    assert target_connector.call_count("create") == 0
    assert target_connector.call_count("update") == 0
    assert target_connector.call_count("delete") == 0
    # The source WAS read: this is a real plan, not a stub.
    assert source_connector.call_count("read") == 1


def test_dry_run_without_create_missing_shows_the_honest_skip_not_a_fabricated_create(
    signed_in_client: tuple[TestClient, str],
    config_service: ConfigService,
    source_connector: FakeConnector,
    target_connector: FakeConnector,
) -> None:
    client, csrf = signed_in_client
    pair_id = _setup_pair(client, csrf, config_service)
    _seed_one_product(source_connector)

    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/dry-run", json={}, headers={CSRF_HEADER: csrf}
    )
    assert response.status_code == 200, response.text
    run = response.json()["runs"][0]
    assert run["counts"]["created"] == 0
    assert run["counts"]["skipped"] == 1
    assert run["records"][0]["reason"] == "no_target_binding"
    assert target_connector.call_count("create") == 0


def test_dry_run_on_unknown_pair_is_a_clear_404(signed_in_client: tuple[TestClient, str]) -> None:
    client, csrf = signed_in_client
    missing = str(uuid.uuid4())
    response = client.post(
        f"{API_PREFIX}/pairs/{missing}/dry-run", json={}, headers={CSRF_HEADER: csrf}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "sync_pair_not_found"


# ========================================================================================
# Run now: a real cycle, the target sees it, and it is recorded like a scheduled one
# ========================================================================================


def test_run_now_actually_runs_a_cycle_and_the_target_sees_the_writes(
    signed_in_client: tuple[TestClient, str],
    config_service: ConfigService,
    source_connector: FakeConnector,
    target_connector: FakeConnector,
    recorder: RunRecorder,
) -> None:
    client, csrf = signed_in_client
    pair_id = _setup_pair(client, csrf, config_service)
    _seed_one_product(source_connector)

    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/run-now",
        json={"create_missing": True},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 200, response.text
    run = response.json()["runs"][0]
    assert run["dry_run"] is False
    assert run["committed"] is True
    assert run["counts"]["created"] == 1

    assert target_connector.call_count("create") == 1

    # Recorded like a scheduled cycle would be (runs/recorder.py, T11.4) -- an operator
    # triggering a cycle by hand must not leave a gap in run history.
    history = asyncio.run(recorder.list_runs(pair="db-to-qlik"))
    assert len(history) == 1
    assert history[0].dry_run is False
    assert history[0].created_count == 1


def test_dry_run_is_not_recorded_in_run_history(
    signed_in_client: tuple[TestClient, str],
    config_service: ConfigService,
    source_connector: FakeConnector,
    recorder: RunRecorder,
) -> None:
    client, csrf = signed_in_client
    pair_id = _setup_pair(client, csrf, config_service)
    _seed_one_product(source_connector)

    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/dry-run",
        json={"create_missing": True},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 200, response.text

    history = asyncio.run(recorder.list_runs(pair="db-to-qlik"))
    assert history == []


def test_run_now_on_unknown_pair_is_a_clear_404(signed_in_client: tuple[TestClient, str]) -> None:
    client, csrf = signed_in_client
    missing = str(uuid.uuid4())
    response = client.post(
        f"{API_PREFIX}/pairs/{missing}/run-now", json={}, headers={CSRF_HEADER: csrf}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "sync_pair_not_found"


# ========================================================================================
# Run now on a pair already running: refused, never queued -- max_instances=1 (C1)
# ========================================================================================


async def test_run_now_on_a_pair_already_running_is_refused_not_queued(
    async_client: httpx.AsyncClient,
    config_service: ConfigService,
    source_connector: FakeConnector,
    target_connector: FakeConnector,
) -> None:
    """FAILS if run-now could ever run two cycles for one pair concurrently: the second
    call is made to arrive *while the first is provably still inside its cycle* (a
    patched, ``asyncio.Event``-gated ``list_changed``, mirroring ``tests/scheduler/
    scheduler_helpers.py``'s own started/release pattern for the identical claim), and
    this asserts the second is refused with 409 before it ever gets a chance to run --
    not silently queued behind the first.
    """
    csrf = await _sign_in_async(async_client)
    create_endpoint_response = await async_client.post(
        f"{API_PREFIX}/endpoints",
        json={"name": "src", "connector": SOURCE_ENDPOINT, "role": "source", "enabled": True},
        headers={CSRF_HEADER: csrf},
    )
    assert create_endpoint_response.status_code == 201, create_endpoint_response.text
    create_target_response = await async_client.post(
        f"{API_PREFIX}/endpoints",
        json={"name": "tgt", "connector": TARGET_ENDPOINT, "role": "target", "enabled": True},
        headers={CSRF_HEADER: csrf},
    )
    assert create_target_response.status_code == 201, create_target_response.text
    pair_response = await async_client.post(
        f"{API_PREFIX}/pairs",
        json={
            "name": "db-to-qlik",
            "source": "src",
            "target": "tgt",
            "target_space": "Sales Space",
            "entity_types": ["data_product"],
            "enabled": True,
        },
        headers={CSRF_HEADER: csrf},
    )
    assert pair_response.status_code == 201, pair_response.text
    pair_id = pair_response.json()["id"]
    await _include_everything(config_service, uuid.UUID(pair_id))

    _seed_one_product(source_connector)

    entered = asyncio.Event()
    release = asyncio.Event()
    original_list_changed = source_connector.list_changed

    async def _hanging_list_changed(entity_type: object, since: object) -> object:
        entered.set()
        await release.wait()
        return await original_list_changed(entity_type, since)  # type: ignore[arg-type]

    source_connector.list_changed = _hanging_list_changed  # type: ignore[method-assign]

    first_call = asyncio.create_task(
        async_client.post(
            f"{API_PREFIX}/pairs/{pair_id}/run-now",
            json={"create_missing": True},
            headers={CSRF_HEADER: csrf},
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)

    # While the first cycle is provably in flight, the API must say so honestly --
    # FAILS if it ever reports a pair as idle while a cycle is actually running.
    status_while_running = await async_client.get(f"{API_PREFIX}/pairs/{pair_id}/run-status")
    assert status_while_running.status_code == 200
    assert status_while_running.json()["running"] is True

    second = await async_client.post(
        f"{API_PREFIX}/pairs/{pair_id}/run-now",
        json={"create_missing": True},
        headers={CSRF_HEADER: csrf},
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "sync_cycle_already_running"

    release.set()
    first = await asyncio.wait_for(first_call, timeout=5)
    assert first.status_code == 200, first.text

    # Only the first call's cycle ever actually wrote -- the refused second one queued
    # nothing and ran nothing once the first released.
    assert target_connector.call_count("create") == 1

    status_after = await async_client.get(f"{API_PREFIX}/pairs/{pair_id}/run-status")
    assert status_after.json()["running"] is False


# ========================================================================================
# Pause / resume: real, persisted state -- honest about what it does and does not do
# ========================================================================================


def test_pause_is_reported_by_run_status_and_refuses_run_now(
    signed_in_client: tuple[TestClient, str],
    config_service: ConfigService,
    source_connector: FakeConnector,
    target_connector: FakeConnector,
) -> None:
    client, csrf = signed_in_client
    pair_id = _setup_pair(client, csrf, config_service, enabled=True)
    _seed_one_product(source_connector)

    before = client.get(f"{API_PREFIX}/pairs/{pair_id}/run-status")
    assert before.status_code == 200
    # FAILS if the API ever conflates "paused" with "running" -- an idle, unpaused pair
    # reports both independently false/true as appropriate.
    assert before.json() == {
        "pair_id": pair_id,
        "pair_name": "db-to-qlik",
        "enabled": True,
        "paused": False,
        "running": False,
    }

    paused = client.post(f"{API_PREFIX}/pairs/{pair_id}/pause", headers={CSRF_HEADER: csrf})
    assert paused.status_code == 200, paused.text
    assert paused.json()["enabled"] is False
    assert paused.json()["paused"] is True
    assert paused.json()["running"] is False

    status = client.get(f"{API_PREFIX}/pairs/{pair_id}/run-status")
    assert status.json()["paused"] is True

    # FAILS if a paused pair could still be run by hand through this API.
    refused = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/run-now",
        json={"create_missing": True},
        headers={CSRF_HEADER: csrf},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "sync_pair_paused"
    assert target_connector.call_count("create") == 0

    # Dry-run stays available on a paused pair -- an operator previewing before
    # resuming must not be blocked by the same guard that protects real writes.
    preview = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/dry-run",
        json={"create_missing": True},
        headers={CSRF_HEADER: csrf},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["runs"][0]["counts"]["created"] == 1
    assert target_connector.call_count("create") == 0


def test_resume_after_pause_allows_run_now_again(
    signed_in_client: tuple[TestClient, str],
    config_service: ConfigService,
    source_connector: FakeConnector,
    target_connector: FakeConnector,
) -> None:
    client, csrf = signed_in_client
    pair_id = _setup_pair(client, csrf, config_service, enabled=True)
    _seed_one_product(source_connector)

    client.post(f"{API_PREFIX}/pairs/{pair_id}/pause", headers={CSRF_HEADER: csrf})

    resumed = client.post(f"{API_PREFIX}/pairs/{pair_id}/resume", headers={CSRF_HEADER: csrf})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["enabled"] is True
    assert resumed.json()["paused"] is False

    ran = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/run-now",
        json={"create_missing": True},
        headers={CSRF_HEADER: csrf},
    )
    assert ran.status_code == 200, ran.text
    assert target_connector.call_count("create") == 1


def test_pause_on_unknown_pair_is_a_clear_404(
    signed_in_client: tuple[TestClient, str],
    config_service: ConfigService,
) -> None:
    client, csrf = signed_in_client
    pair_id = _setup_pair(client, csrf, config_service, enabled=True)
    missing = str(uuid.uuid4())

    response = client.post(f"{API_PREFIX}/pairs/{missing}/pause", headers={CSRF_HEADER: csrf})
    assert response.status_code == 404
    assert response.json()["code"] == "sync_pair_not_found"

    # The real pair is unaffected by a pause attempt against a different id.
    status = client.get(f"{API_PREFIX}/pairs/{pair_id}/run-status")
    assert status.json()["enabled"] is True


# ========================================================================================
# Auth: every route lives under the prefix, mutations need a session and a CSRF token
# ========================================================================================


def test_dry_run_and_run_now_require_a_session(client: TestClient) -> None:
    pair_id = str(uuid.uuid4())
    dry_run_response = client.post(f"{API_PREFIX}/pairs/{pair_id}/dry-run", json={})
    assert dry_run_response.status_code == 401
    run_now_response = client.post(f"{API_PREFIX}/pairs/{pair_id}/run-now", json={})
    assert run_now_response.status_code == 401
    status_response = client.get(f"{API_PREFIX}/pairs/{pair_id}/run-status")
    assert status_response.status_code == 401


def test_run_now_and_pause_without_a_csrf_token_are_refused(
    signed_in_client: tuple[TestClient, str],
    config_service: ConfigService,
) -> None:
    client, _csrf = signed_in_client
    pair_id = _setup_pair(client, _csrf, config_service, enabled=True)

    run_now_response = client.post(f"{API_PREFIX}/pairs/{pair_id}/run-now", json={})
    assert run_now_response.status_code == 403
    assert run_now_response.json()["code"] == "csrf_token_invalid"

    pause_response = client.post(f"{API_PREFIX}/pairs/{pair_id}/pause")
    assert pause_response.status_code == 403
    assert pause_response.json()["code"] == "csrf_token_invalid"
