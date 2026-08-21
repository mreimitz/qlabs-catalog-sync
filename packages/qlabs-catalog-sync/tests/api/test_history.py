"""Run history, run-issue and configuration change-log routes (WP12/T12.7): the module
under test is ``qlabs_catalog_sync.api.routes.history``, which is NOT wired into
``api.app.create_app`` by this task (three WP12 route tasks landed in parallel and would
have collided on that file -- see this task's own report for the exact lines the
orchestrator adds there instead). So every test here builds its own app, in
:func:`_build_app` below, out of the same pieces ``create_app`` itself uses --
``install_error_handlers``, ``install_auth`` and every route factory this suite needs,
``build_history_router`` included -- exactly the shape ``create_app`` will have once this
router is registered, proven independently of that registration ever landing.

Drives real HTTP through a real, migrated SQLite database (``state.migrate.upgrade_to_head``
runs all three migrations -- state store, config store, run history -- against one file,
because ``runs``/``configstore``/``state`` share one ``Base.metadata`` by design, C1), a
real :class:`~qlabs_catalog_sync.configstore.service.ConfigService`, a real
:class:`~qlabs_catalog_sync.state.store.StateStore` and a real
:class:`~qlabs_catalog_sync.runs.recorder.RunRecorder` all sharing one engine, and a
never-instantiated stub connector registry (``sync_pair_helpers.build_registry``,
already used by ``test_pairs.py``/``test_selection_routes.py``) for the config-store half.
No mocks.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from qlabs_catalog_sync.api.app import API_PREFIX
from qlabs_catalog_sync.api.auth import AdminCredential, ConsoleAuth, install_auth
from qlabs_catalog_sync.api.errors import install_error_handlers
from qlabs_catalog_sync.api.routes import (
    build_connectors_router,
    build_endpoints_router,
    build_pairs_router,
    build_selection_router,
)
from qlabs_catalog_sync.api.routes.history import build_history_router
from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.runs.recorder import STALE_RUN_MESSAGE, RunRecorder
from qlabs_catalog_sync.state.db import create_state_engine
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import (
    ErrorReport,
    RecordOutcome,
    RecordReport,
    RunStatus,
    SyncLoop,
    SyncRunReport,
)
from qlabs_catalog_sync_sdk.contract import WriteResult
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector, qlik_shaped_manifest

from .sync_pair_helpers import (
    CSRF_HEADER,
    PASSWORD_HASH,
    USERNAME,
    build_registry,
    create_endpoint,
    create_pair,
    sign_in,
)

RUNS_PATH = f"{API_PREFIX}/runs"
CHANGES_PATH = f"{API_PREFIX}/config-changes"

STARTED_AT = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)

#: A sentinel that would only ever appear in a response if a credential value leaked
#: through it. Never asserted as *present* anywhere -- only ever asserted absent.
SECRET_SENTINEL = "t12-7-never-leaves-this-process-3f9c1a"


# ========================================================================================
# App-building: mirrors tests/api/test_pairs.py's own fixture shape, plus the one router
# this task owns, mounted the way api.app.create_app will mount it once registered there.
# ========================================================================================


def _build_app(
    *,
    config_service: ConfigService,
    registry: ConnectorRegistry,
    recorder: RunRecorder,
    auth: ConsoleAuth,
) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    install_auth(app, auth=auth, api_prefix=API_PREFIX)
    app.include_router(build_connectors_router(registry), prefix=API_PREFIX)
    app.include_router(build_endpoints_router(config_service, registry), prefix=API_PREFIX)
    app.include_router(build_pairs_router(config_service), prefix=API_PREFIX)
    app.include_router(build_selection_router(config_service), prefix=API_PREFIX)
    app.include_router(build_history_router(recorder, config_service), prefix=API_PREFIX)
    return app


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'state.db'}"
    upgrade_to_head(url)
    return url


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    eng = create_state_engine(db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def registry() -> ConnectorRegistry:
    return build_registry()


@pytest.fixture
def config_service(engine: Engine, registry: ConnectorRegistry) -> ConfigService:
    return ConfigService(engine, registry)


@pytest.fixture
def state_store(engine: Engine) -> StateStore:
    return StateStore(engine)


@pytest.fixture
def recorder(engine: Engine) -> RunRecorder:
    return RunRecorder(engine)


@pytest.fixture
def auth() -> ConsoleAuth:
    credential = AdminCredential.from_password_hash(PASSWORD_HASH, username=USERNAME)
    return ConsoleAuth(credential=credential)


@pytest.fixture
def app(
    config_service: ConfigService,
    registry: ConnectorRegistry,
    recorder: RunRecorder,
    auth: ConsoleAuth,
) -> FastAPI:
    return _build_app(
        config_service=config_service, registry=registry, recorder=recorder, auth=auth
    )


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def signed_in_client(client: TestClient) -> tuple[TestClient, str]:
    return client, sign_in(client)


# ========================================================================================
# Run-history fixture helpers: hand-constructed rows for edge cases, a real SyncLoop cycle
# for the one test that has to prove the routes against what the engine really emits.
# ========================================================================================


async def _finished_run(
    recorder: RunRecorder,
    *,
    pair: str = "db-to-qlik",
    source_endpoint: str = "dbx",
    target_endpoint: str = "qlik_tenant",
    entity_type: EntityType = EntityType.DATA_PRODUCT,
    started_at: datetime = STARTED_AT,
    records: tuple[RecordReport, ...] = (),
    errors: tuple[ErrorReport, ...] = (),
    run_status: RunStatus = RunStatus.OK,
    committed: bool = True,
) -> uuid.UUID:
    """Start and finish one run from a hand-built :class:`SyncRunReport` -- the shape
    every edge-case test in this file needs, without a real connector or a real cycle."""
    run_id = await recorder.start(
        pair=pair,
        source_endpoint=source_endpoint,
        target_endpoint=target_endpoint,
        entity_type=entity_type,
        dry_run=False,
        started_at=started_at,
    )
    finished_at = started_at + timedelta(seconds=1)
    report = SyncRunReport(
        pair=pair,
        source_endpoint=source_endpoint,
        target_endpoint=target_endpoint,
        entity_type=entity_type,
        status=run_status,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=1.0,
        committed=committed,
        records=records,
        errors=errors,
    )
    await recorder.finish(run_id, report)
    return run_id


class _UnresolvedFieldTarget(FakeConnector):
    """A Qlik-shaped write target whose every ``create`` also reports one field it could
    not resolve, through ``WriteResult.skipped_fields`` -- the SDK's documented D2/D3
    channel (``qlabs_catalog_sync_sdk.contract.WriteResult``'s own docstring). Small and
    local rather than imported from ``tests/runs/run_history_helpers.py``: this package
    does not own that file, and it documents itself as not a stable cross-package API.
    """

    name: ClassVar[str] = "qlik-target"

    def __init__(self, *, unresolved_field: str = "owners") -> None:
        super().__init__(manifest=qlik_shaped_manifest())
        self.unresolved_field = unresolved_field

    async def create(self, entity: Any) -> WriteResult:
        result = await super().create(entity)
        return result.model_copy(
            update={
                "skipped_fields": [*result.skipped_fields, self.unresolved_field],
                "detail": f"{self.unresolved_field}: no matching Qlik reference found",
            }
        )


def _make_loop(
    *,
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    resolver: IdentityResolver,
    **overrides: Any,
) -> SyncLoop:
    pair = SyncPairConfig(
        name="db-to-qlik",
        source=source.name,
        target=target.name,
        catalog_schema_patterns=["sales.*"],
        target_space="Sales Space",
        entity_types=[EntityType.DATA_PRODUCT],
    )
    kwargs: dict[str, Any] = {
        "pair": pair,
        "source": source,
        "target": target,
        "store": store,
        "resolver": resolver,
        "sleep": _no_sleep,
    }
    kwargs.update(overrides)
    return SyncLoop(**kwargs)


async def _no_sleep(seconds: float) -> None:
    return None


# ========================================================================================
# Auth applies (a full property is tests/api/test_auth.py's job; this is a spot check)
# ========================================================================================


def test_listing_runs_requires_a_session(client: TestClient) -> None:
    response = client.get(RUNS_PATH)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_reading_config_changes_requires_a_session(client: TestClient) -> None:
    response = client.get(CHANGES_PATH)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


# ========================================================================================
# Run list: explicit pagination contract, newest first
# ========================================================================================


async def test_run_list_pages_newest_first_with_an_explicit_contract(
    signed_in_client: tuple[TestClient, str], recorder: RunRecorder
) -> None:
    client, _csrf = signed_in_client
    ids = [
        await _finished_run(recorder, started_at=STARTED_AT + timedelta(seconds=i))
        for i in range(5)
    ]

    first_page = client.get(RUNS_PATH, params={"limit": 2})
    assert first_page.status_code == 200, first_page.text
    body = first_page.json()
    assert body["limit"] == 2
    assert body["has_more"] is True
    assert body["next_cursor"] is not None
    # Newest first: the two most recently started runs, in descending order.
    assert [item["id"] for item in body["items"]] == [str(ids[4]), str(ids[3])]

    second_page = client.get(RUNS_PATH, params={"limit": 2, "cursor": body["next_cursor"]})
    assert second_page.status_code == 200, second_page.text
    second_body = second_page.json()
    assert [item["id"] for item in second_body["items"]] == [str(ids[2]), str(ids[1])]
    assert second_body["has_more"] is True

    third_page = client.get(RUNS_PATH, params={"limit": 2, "cursor": second_body["next_cursor"]})
    third_body = third_page.json()
    assert [item["id"] for item in third_body["items"]] == [str(ids[0])]
    assert third_body["has_more"] is False
    assert third_body["next_cursor"] is None


async def test_run_list_filters_by_pair_entity_type_and_status(
    signed_in_client: tuple[TestClient, str], recorder: RunRecorder
) -> None:
    client, _csrf = signed_in_client
    wanted = await _finished_run(recorder, pair="wanted-pair", started_at=STARTED_AT)
    await _finished_run(recorder, pair="other-pair", started_at=STARTED_AT + timedelta(seconds=1))
    await _finished_run(
        recorder,
        pair="wanted-pair",
        started_at=STARTED_AT + timedelta(seconds=2),
        run_status=RunStatus.FAILED,
        committed=False,
    )

    response = client.get(RUNS_PATH, params={"pair": "wanted-pair", "status": "ok"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(wanted)]


async def test_run_list_pages_deterministically_under_concurrent_writes(
    signed_in_client: tuple[TestClient, str], recorder: RunRecorder
) -> None:
    """The DoD's own words: "run history pages deterministically under concurrent
    writes." Several runs starting at the *same instant* -- exactly what concurrent
    writers produce -- must still page without a duplicate or a dropped row, because the
    keyset cursor breaks every tie with the row id, not with a wall-clock value that
    concurrent writers can share."""
    client, _csrf = signed_in_client

    async def _start_one(n: int) -> uuid.UUID:
        return await recorder.start(
            pair="concurrent-pair",
            source_endpoint="dbx",
            target_endpoint="qlik_tenant",
            entity_type=EntityType.DATA_PRODUCT,
            dry_run=False,
            started_at=STARTED_AT,  # identical for every one of them
        )

    created_ids = await asyncio.gather(*(_start_one(n) for n in range(7)))

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(20):  # generous upper bound on pages; the real loop always breaks
        params: dict[str, Any] = {"pair": "concurrent-pair", "limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get(RUNS_PATH, params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
        assert cursor is not None
    else:
        pytest.fail("pagination never terminated -- has_more never went false")

    assert sorted(seen) == sorted(str(i) for i in created_ids)
    assert len(seen) == len(set(seen)), "a row was paged twice"


def test_invalid_cursor_is_a_clean_422_not_a_500(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, _csrf = signed_in_client
    response = client.get(RUNS_PATH, params={"cursor": "not-a-real-cursor"})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_cursor"


def test_run_list_limit_is_bounded(signed_in_client: tuple[TestClient, str]) -> None:
    client, _csrf = signed_in_client
    too_big = client.get(RUNS_PATH, params={"limit": 100000})
    assert too_big.status_code == 422
    too_small = client.get(RUNS_PATH, params={"limit": 0})
    assert too_small.status_code == 422


# ========================================================================================
# Run detail: counts, items, errors -- and the "not found" case
# ========================================================================================


async def test_run_detail_includes_counts_items_and_errors(
    signed_in_client: tuple[TestClient, str], recorder: RunRecorder
) -> None:
    client, _csrf = signed_in_client
    neutral_id = uuid.uuid4()
    run_id = await _finished_run(
        recorder,
        records=(
            RecordReport(
                native_key="sales.orders",
                entity_type=EntityType.DATA_PRODUCT,
                outcome=RecordOutcome.CREATED,
                neutral_id=neutral_id,
                was_read=True,
            ),
        ),
        errors=(ErrorReport(kind="TransientError", message="tenant timed out", retryable=True),),
    )

    response = client.get(f"{RUNS_PATH}/{run_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(run_id)
    assert body["status"] == "ok"
    assert body["in_progress"] is False
    assert body["counts"]["created"] == 1
    assert body["counts"]["write"] == 1
    assert body["counts"]["error"] == 1
    assert len(body["items"]) == 0  # a clean CREATED record is not "reportable"
    assert len(body["errors"]) == 1
    assert body["errors"][0]["message"] == "tenant timed out"
    assert body["errors"][0]["is_stale_sweep"] is False


def test_run_detail_unknown_id_is_a_clear_404(signed_in_client: tuple[TestClient, str]) -> None:
    client, _csrf = signed_in_client
    missing = uuid.uuid4()
    response = client.get(f"{RUNS_PATH}/{missing}")
    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"

    issues_response = client.get(f"{RUNS_PATH}/{missing}/issues")
    assert issues_response.status_code == 404
    assert issues_response.json()["code"] == "run_not_found"


# ========================================================================================
# The dishonest cases: a run in progress must never look failed, and "no issues" must
# never look the same as "issues were never recorded".
# ========================================================================================


async def test_a_run_in_progress_is_rendered_as_running_never_as_failed(
    signed_in_client: tuple[TestClient, str], recorder: RunRecorder
) -> None:
    client, _csrf = signed_in_client
    run_id = await recorder.start(
        pair="in-flight-pair",
        source_endpoint="dbx",
        target_endpoint="qlik_tenant",
        entity_type=EntityType.DATA_PRODUCT,
        dry_run=False,
        started_at=STARTED_AT,
    )

    response = client.get(f"{RUNS_PATH}/{run_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "running"
    assert body["status"] != "failed"
    assert body["in_progress"] is True
    assert body["finished_at"] is None

    listed = client.get(RUNS_PATH, params={"pair": "in-flight-pair"})
    assert listed.json()["items"][0]["status"] == "running"


async def test_no_issues_is_distinguishable_from_issues_not_yet_recorded(
    signed_in_client: tuple[TestClient, str], recorder: RunRecorder
) -> None:
    client, _csrf = signed_in_client

    still_running = await recorder.start(
        pair="p",
        source_endpoint="dbx",
        target_endpoint="qlik_tenant",
        entity_type=EntityType.DATA_PRODUCT,
        dry_run=False,
        started_at=STARTED_AT,
    )
    clean_finish = await _finished_run(
        recorder, pair="p", started_at=STARTED_AT + timedelta(seconds=5)
    )

    running_issues = client.get(f"{RUNS_PATH}/{still_running}/issues").json()
    assert running_issues["issues_recorded"] is False
    assert running_issues["has_issues"] is False  # nothing to show yet -- not "clean"

    clean_issues = client.get(f"{RUNS_PATH}/{clean_finish}/issues").json()
    assert clean_issues["issues_recorded"] is True
    assert clean_issues["has_issues"] is False  # genuinely nothing wrong
    assert clean_issues["unresolved_dataset_members"] == []
    assert clean_issues["unresolvable_owners"] == []
    assert clean_issues["orphans"] == []
    assert clean_issues["other_outstanding"] == []
    assert clean_issues["errors"] == []

    # The two must not collapse to the same shape: one is a promise ("nothing wrong"),
    # the other is silence ("nothing recorded yet").
    assert running_issues["issues_recorded"] != clean_issues["issues_recorded"]


async def test_swept_stale_run_is_distinguishable_from_a_genuine_failure(
    signed_in_client: tuple[TestClient, str], recorder: RunRecorder
) -> None:
    client, _csrf = signed_in_client

    genuinely_failed = await recorder.start(
        pair="p",
        source_endpoint="dbx",
        target_endpoint="qlik_tenant",
        entity_type=EntityType.DATA_PRODUCT,
        dry_run=False,
        started_at=STARTED_AT,
    )
    await recorder.fail(
        genuinely_failed,
        message="tenant credential rejected",
        finished_at=STARTED_AT + timedelta(seconds=1),
        kind="AuthError",
    )

    reaped = await recorder.start(
        pair="p",
        source_endpoint="dbx",
        target_endpoint="qlik_tenant",
        entity_type=EntityType.DATA_PRODUCT,
        dry_run=False,
        started_at=STARTED_AT,
    )
    reaped_ids = await recorder.reap_stale(now=STARTED_AT + timedelta(minutes=10))
    assert reaped in reaped_ids

    failed_body = client.get(f"{RUNS_PATH}/{genuinely_failed}").json()
    assert failed_body["status"] == "failed"
    assert failed_body["swept_stale"] is False
    assert "credential rejected" in failed_body["errors"][0]["message"]

    reaped_body = client.get(f"{RUNS_PATH}/{reaped}").json()
    assert reaped_body["status"] == "failed"
    assert reaped_body["swept_stale"] is True
    assert reaped_body["errors"][0]["message"] == STALE_RUN_MESSAGE

    # Both status "failed", but not the same failure -- an operator must be able to tell.
    assert failed_body["swept_stale"] != reaped_body["swept_stale"]


# ========================================================================================
# Run issues: D2 (unresolved dataset members), D3 (unresolvable owners), D4 (orphans) and
# errors, as one coherent answer -- attributable to a specific run and object.
# ========================================================================================


async def test_run_issues_surfaces_dataset_members_owners_and_orphans_as_one_answer(
    signed_in_client: tuple[TestClient, str], recorder: RunRecorder, state_store: StateStore
) -> None:
    client, _csrf = signed_in_client
    orphan_neutral_id = uuid.uuid4()

    async with state_store.unit_of_work() as uow:
        await uow.record_orphan(
            orphan_neutral_id,
            "dbx",
            EntityType.DATA_PRODUCT,
            native_key="sales.legacy",
            last_seen_at=STARTED_AT - timedelta(days=1),
            observed_at=STARTED_AT,
        )

    run_id = await _finished_run(
        recorder,
        records=(
            RecordReport(
                native_key="sales.orders",
                entity_type=EntityType.DATA_PRODUCT,
                outcome=RecordOutcome.CREATED,
                neutral_id=uuid.uuid4(),
                target_skipped_fields=("dataset_refs",),
                detail="dataset_refs: no matching Qlik dataset found",
            ),
            RecordReport(
                native_key="sales.customers",
                entity_type=EntityType.DATA_PRODUCT,
                outcome=RecordOutcome.WRITTEN,
                neutral_id=uuid.uuid4(),
                target_skipped_fields=("owners",),
                detail="owners: no matching Qlik user found",
            ),
            RecordReport(
                native_key="sales.legacy",
                entity_type=EntityType.DATA_PRODUCT,
                outcome=RecordOutcome.ORPHANED,
                neutral_id=orphan_neutral_id,
            ),
            RecordReport(
                native_key="sales.broken",
                entity_type=EntityType.DATA_PRODUCT,
                outcome=RecordOutcome.FAILED,
                detail="target rejected the write",
            ),
        ),
        run_status=RunStatus.PARTIAL,
    )

    response = client.get(f"{RUNS_PATH}/{run_id}/issues")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["issues_recorded"] is True
    assert body["has_issues"] is True

    assert [item["native_key"] for item in body["unresolved_dataset_members"]] == ["sales.orders"]
    assert [item["native_key"] for item in body["unresolvable_owners"]] == ["sales.customers"]
    assert [item["native_key"] for item in body["other_outstanding"]] == ["sales.broken"]

    assert len(body["orphans"]) == 1
    orphan = body["orphans"][0]
    assert orphan["native_key"] == "sales.legacy"
    assert orphan["neutral_id"] == str(orphan_neutral_id)
    # Attributable to a specific run AND a specific object -- not just "3 orphans".
    assert orphan["run_item_id"]
    # Enriched from orphan_log, not invented: the two sources cannot disagree because
    # this value is read from orphan_log, never copied from the run_items row.
    assert orphan["orphan_log_found"] is True
    assert orphan["still_open"] is True
    assert orphan["last_seen_at"] is not None


# ========================================================================================
# The one test that reads back history a real SyncLoop cycle actually produced.
# ========================================================================================


async def test_a_real_sync_loop_cycle_is_reachable_through_the_routes(
    signed_in_client: tuple[TestClient, str],
    recorder: RunRecorder,
    state_store: StateStore,
    tmp_path: Path,
) -> None:
    client, _csrf = signed_in_client
    source = FakeConnector.read_only_source(name="dbx")
    source.seed(DataProduct(name="Orders"), native_key="sales.orders")
    target = _UnresolvedFieldTarget(unresolved_field="owners")
    resolver = IdentityResolver(state_store, review_path=tmp_path / "identity-review.json")

    loop = _make_loop(
        source=source, target=target, store=state_store, resolver=resolver, create_missing=True
    )

    run_id = await recorder.start(
        pair=loop.pair.name,
        source_endpoint=loop.source_endpoint,
        target_endpoint=loop.target_endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        dry_run=False,
        started_at=STARTED_AT,
    )
    report = await loop.run_cycle(EntityType.DATA_PRODUCT)
    await recorder.finish(run_id, report)

    # The routes agree with what the loop itself reported, not a re-derived summary.
    listed = client.get(RUNS_PATH, params={"pair": loop.pair.name})
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [str(run_id)]

    detail = client.get(f"{RUNS_PATH}/{run_id}").json()
    # The route's counts agree with the report the loop itself produced -- not a
    # re-derived summary this test could accidentally get right by coincidence.
    assert detail["counts"]["created"] == report.count(RecordOutcome.CREATED)
    assert detail["source_endpoint"] == "dbx"
    assert detail["target_endpoint"] == loop.target_endpoint

    issues = client.get(f"{RUNS_PATH}/{run_id}/issues").json()
    assert [item["native_key"] for item in issues["unresolvable_owners"]] == ["sales.orders"]


# ========================================================================================
# The configuration change log: raw field-level rows, "when did this schema start
# syncing", and the deliberate choice not to group into synthetic operations.
# ========================================================================================


async def test_change_log_answers_when_a_schema_started_syncing(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")
    pair = create_pair(client, csrf, name="pair-1", source="src", target="tgt")

    rule_response = client.post(
        f"{API_PREFIX}/pairs/{pair['id']}/rules",
        json={
            "scope": "object",
            "decision": "include",
            "matcher_kind": "glob",
            "pattern": "analytics.prod",
        },
        headers={CSRF_HEADER: csrf},
    )
    assert rule_response.status_code == 201, rule_response.text
    rule = rule_response.json()

    response = client.get(
        CHANGES_PATH, params={"entity_kind": "selection_rule", "entity_id": rule["id"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    entry = body["items"][0]
    assert entry["action"] == "create"
    assert entry["entity_id"] == rule["id"]
    # This is the answer to "when did this schema start syncing": the timestamp the
    # include rule for it was created.
    assert entry["changed_at"]
    assert entry["new_value"]["pattern"] == "analytics.prod"
    assert entry["new_value"]["decision"] == "include"


async def test_change_log_keeps_field_level_detail_of_a_multi_field_update(
    signed_in_client: tuple[TestClient, str],
) -> None:
    """T12.7's own decision: raw rows, one per changed field, never grouped into a
    synthetic "operation". A change log that instead collapsed a multi-field update into
    one row would fail this exact assertion -- one row per field, not one row total."""
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")
    pair = create_pair(client, csrf, name="pair-1", source="src", target="tgt", enabled=False)

    update = client.patch(
        f"{API_PREFIX}/pairs/{pair['id']}",
        json={"cadence_seconds": 1800, "enabled": True},
        headers={CSRF_HEADER: csrf},
    )
    assert update.status_code == 200, update.text

    response = client.get(
        CHANGES_PATH,
        params={"entity_kind": "sync_pair", "entity_id": pair["id"], "action": "update"},
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]

    # One row per changed field -- never one row for the whole update.
    fields_changed = {item["field"] for item in items}
    assert fields_changed == {"cadence_seconds", "enabled"}
    assert len(items) == 2

    # Every row shares one generation: they describe one write, even though it is
    # reported as more than one row -- a client can recover "one operation" losslessly
    # by grouping on this value, without the server ever having discarded field detail.
    generations = {item["generation"] for item in items}
    assert len(generations) == 1

    cadence_row = next(item for item in items if item["field"] == "cadence_seconds")
    assert cadence_row["old_value"] == 900
    assert cadence_row["new_value"] == 1800
    enabled_row = next(item for item in items if item["field"] == "enabled")
    assert enabled_row["old_value"] is False
    assert enabled_row["new_value"] is True


async def test_change_log_filters_and_paginates_like_run_history(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")

    response = client.get(CHANGES_PATH, params={"limit": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["limit"] == 1
    assert len(body["items"]) == 1
    if body["has_more"]:
        assert body["next_cursor"] is not None


def test_change_log_invalid_cursor_is_a_clean_422(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, _csrf = signed_in_client
    response = client.get(CHANGES_PATH, params={"cursor": "garbage"})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_cursor"


# ========================================================================================
# No credential, ever, in any response this module produces.
# ========================================================================================


async def test_no_credential_reaches_any_history_or_change_log_response(
    signed_in_client: tuple[TestClient, str],
    recorder: RunRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, csrf = signed_in_client
    # A real credential value, resolvable through the endpoint's secret_ref (C2). Nothing
    # in this router ever resolves a secret_ref -- these routes only ever read run history
    # and the change log, neither of which touches ConfigService.get_endpoint's secret
    # resolution path -- so this value must never appear in any response no matter what
    # else the response legitimately echoes (record details, endpoint names, ...).
    monkeypatch.setenv("QLABS_SRC__SOME_SECRET", SECRET_SENTINEL)
    create_endpoint(
        client,
        csrf,
        name="src",
        connector="source",
        role="source",
        secret_ref="env:QLABS_SRC",
    )
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")
    pair = create_pair(client, csrf, name="pair-1", source="src", target="tgt")
    run_id = await _finished_run(
        recorder,
        records=(
            RecordReport(
                native_key="sales.orders",
                entity_type=EntityType.DATA_PRODUCT,
                outcome=RecordOutcome.CREATED,
                neutral_id=uuid.uuid4(),
                target_skipped_fields=("owners",),
                detail="owners: no matching Qlik user found",
            ),
        ),
    )

    bodies = [
        client.get(RUNS_PATH).text,
        client.get(f"{RUNS_PATH}/{run_id}").text,
        client.get(f"{RUNS_PATH}/{run_id}/issues").text,
        client.get(CHANGES_PATH, params={"entity_id": pair["id"]}).text,
        client.get(CHANGES_PATH, params={"entity_id": "src"}).text,
        client.get(CHANGES_PATH, params={"entity_kind": "endpoint", "entity_id": "src"}).text,
    ]
    for body in bodies:
        assert SECRET_SENTINEL not in body


# ========================================================================================
# Every response model reaches the OpenAPI schema (T12.8 generates the TS client from it).
# ========================================================================================


def test_response_models_reach_the_openapi_schema(app: FastAPI) -> None:
    schema = app.openapi()
    schemas = schema["components"]["schemas"]
    for name in (
        "RunSummaryOut",
        "RunDetailOut",
        "RunListPage",
        "RunIssuesOut",
        "RunItemOut",
        "RunErrorOut",
        "RunOrphanIssueOut",
        "ConfigChangeOut",
        "ConfigChangeListPage",
    ):
        assert name in schemas, f"{name} is missing from the OpenAPI schema"


def test_cursor_encoding_round_trips_without_padding_characters_breaking_query_strings() -> None:
    """Sanity check on the module's own cursor helpers -- urlsafe base64 avoids '+'/'/'
    which would otherwise need percent-encoding in a query string."""
    from qlabs_catalog_sync.api.routes.history import _decode_cursor, _encode_cursor

    row_id = uuid.uuid4()
    cursor = _encode_cursor(STARTED_AT, row_id)
    assert "+" not in cursor and "/" not in cursor
    decoded_at, decoded_id = _decode_cursor(cursor)
    assert decoded_at == STARTED_AT
    assert decoded_id == row_id
