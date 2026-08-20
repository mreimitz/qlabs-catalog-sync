"""Source-tree and preview routes (WP12/T12.5): ``/pairs/{pair_id}/source-tree``,
``/pairs/{pair_id}/preview``.

Drives real HTTP through a real ``create_app`` app (plus this task's own
``build_preview_router``, spliced in ahead of the SPA catch-all -- see
:func:`_mount_before_catch_all`), a real
:class:`~qlabs_catalog_sync.configstore.service.ConfigService` over a real migrated
SQLite database, and a real :class:`~qlabs_catalog_sync_sdk.testing.FakeConnector`
source -- no mocks. Mirrors ``tests/api/test_endpoints.py``'s own fixture shape
(``_wrap_as_class``) for wiring an already-configured connector *instance* into a
:class:`~qlabs_catalog_sync.discovery.ConnectorRegistry`.

Because ``routes/preview.py`` is not wired into ``api/app.py`` yet (three WP12 route
tasks are landing in parallel; the orchestrator wires all three routers into
``create_app`` in one pass), this suite builds its own app per test via :func:`_harness`
rather than depending on ``create_app`` mounting it -- see that function for exactly how.

Decision C4 in full: *"The sync loop and the console's preview call the same
implementation. A preview that can disagree with the run it predicts is worse than no
preview, so there is exactly one code path."* This suite is the API-layer half of that
certification -- ``tests/selection/test_preview_sync_agreement.py`` is the layer below
it, over the shared functions directly.
"""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from qlabs_catalog_sync.api.app import API_PREFIX, create_app
from qlabs_catalog_sync.api.auth import AdminCredential, ConsoleAuth
from qlabs_catalog_sync.api.routes import preview as preview_module
from qlabs_catalog_sync.api.routes.preview import build_preview_router
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.configstore.types import MatcherKind, RuleScope, SelectionDecision
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.observability import HealthRegistry
from qlabs_catalog_sync.selection import DecisionSource, SelectionRule, SelectionRuleSet
from qlabs_catalog_sync.selection.source_tree import SchemaNode, walk_source_tree
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.exceptions import ConnectorError
from qlabs_catalog_sync_sdk.models import DataProduct, Dataset, EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector

from .sync_pair_helpers import (
    CSRF_HEADER,
    PASSWORD_HASH,
    USERNAME,
    create_endpoint,
    create_pair,
    sign_in,
)

SOURCE_CONNECTOR: Final[str] = "source"
TARGET_CONNECTOR: Final[str] = "qlik"
ENDPOINT_SOURCE: Final[str] = "src"
ENDPOINT_TARGET: Final[str] = "tgt"

#: A metastore shaped like the C3 worked example, plus a second catalog that must stay
#: out -- the exact fixture ``tests/selection/test_preview_sync_agreement.py`` uses.
_SCHEMAS: Final = [
    "analytics.sales",
    "analytics.staging",
    "analytics.prod_staging",
    "finance.reporting",
]
_DATASETS: Final = [
    "analytics.sales.orders",
    "analytics.sales.returns",
    "analytics.staging.scratch",
    "analytics.prod_staging.snapshots",
    "finance.reporting.ledger",
]


def _seed_c3_example(source: FakeConnector) -> None:
    for name in _SCHEMAS:
        source.seed(DataProduct(name=name.rsplit(".", 1)[-1]), native_key=name)
    for name in _DATASETS:
        source.seed(Dataset(name=name.rsplit(".", 1)[-1]), native_key=name)


# --------------------------------------------------------------------------------------
# App wiring -- see the module docstring for why this is per-test rather than a shared
# fixture chain: every test needs a differently-configured FakeConnector (a different
# manifest, a queued failure, a paging size), which is simplest to set up before the app
# that wraps it is even built.
# --------------------------------------------------------------------------------------


def _wrap_as_class(instance: Connector) -> type[Connector]:
    """Wrap an already-built connector instance as a zero-argument-constructible class.

    A file-local copy of ``tests/api/test_endpoints.py``'s own ``_wrap_as_class`` (that
    file's own docstring explains why this is copied per suite rather than shared): the
    routes under test build a connector the production way
    (``registry.get_connector(name)()``), and this is what lets a test still hold a
    handle on the *exact* instance that call produces, to seed ``fail_next``/paging on
    before driving the route over HTTP.
    """
    base = type(instance)

    class _Wrapped(base):  # type: ignore[misc, valid-type]
        def __new__(cls) -> Connector:
            return instance

    return _Wrapped


def _mount_before_catch_all(app: FastAPI, router: object, *, prefix: str) -> None:
    """Include ``router`` on ``app``, then move its freshly-added routes to just before
    the SPA catch-all ``create_app``/``mount_static`` always registers last
    (``api/static.py``'s own module docstring: Starlette matches routes in registration
    order, and the catch-all matches every unmatched ``GET``). Generalizes
    ``tests/api/api_helpers.py``'s own ``add_raising_route`` splice from one route to a
    whole router's worth at once -- this is the "construct a FastAPI app and
    include_router your own router... rather than relying on it being mounted by the
    factory" alternative T12.5's brief offers, applied to a full ``create_app`` app
    instead of a bare one, so every other WP12 route (``/pairs``, ``/endpoints``, ...)
    stays reachable too.
    """
    before = len(app.router.routes)
    app.include_router(router, prefix=prefix)  # type: ignore[arg-type]
    added = app.router.routes[before:]
    del app.router.routes[before:]
    insert_at = len(app.router.routes) - 1  # just before the catch-all
    app.router.routes[insert_at:insert_at] = added


@dataclass
class Harness:
    client: TestClient
    csrf: str
    config_service: ConfigService


def _harness(
    tmp_path: Path, source: FakeConnector, *, qlik: FakeConnector | None = None
) -> Harness:
    db_url = f"sqlite:///{tmp_path / 'preview-config.db'}"
    upgrade_to_head(db_url)
    registry = ConnectorRegistry(
        {
            SOURCE_CONNECTOR: _wrap_as_class(source),
            TARGET_CONNECTOR: _wrap_as_class(
                qlik if qlik is not None else FakeConnector.write_target(name=TARGET_CONNECTOR)
            ),
        },
        {},
    )
    config_service = ConfigService.from_url(db_url, registry)
    credential = AdminCredential.from_password_hash(PASSWORD_HASH, username=USERNAME)
    auth = ConsoleAuth(credential=credential)
    app = create_app(
        health=HealthRegistry(),
        metrics_registry=CollectorRegistry(),
        auth=auth,
        config_service=config_service,
        registry=registry,
    )
    _mount_before_catch_all(app, build_preview_router(config_service, registry), prefix=API_PREFIX)
    client = TestClient(app, raise_server_exceptions=False)
    csrf = sign_in(client)
    return Harness(client=client, csrf=csrf, config_service=config_service)


def _setup_pair(h: Harness, *, source_enabled: bool = True) -> str:
    create_endpoint(
        h.client, h.csrf, name=ENDPOINT_SOURCE, connector=SOURCE_CONNECTOR, role="source",
        enabled=source_enabled,
    )
    create_endpoint(
        h.client, h.csrf, name=ENDPOINT_TARGET, connector=TARGET_CONNECTOR, role="target"
    )
    pair = create_pair(h.client, h.csrf, source=ENDPOINT_SOURCE, target=ENDPOINT_TARGET)
    return str(pair["id"])


def _create_rule(
    h: Harness,
    pair_id: str,
    *,
    scope: str,
    decision: str,
    pattern: str,
    matcher_kind: str = "glob",
    ordinal: int | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "scope": scope, "decision": decision, "matcher_kind": matcher_kind, "pattern": pattern,
    }
    if ordinal is not None:
        payload["ordinal"] = ordinal
    response = h.client.post(
        f"{API_PREFIX}/pairs/{pair_id}/rules", json=payload, headers={CSRF_HEADER: h.csrf}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# --------------------------------------------------------------------------------------
# Nothing is written -- structural and behavioral (mirrors
# tests/selection/test_source_tree.py's own two-pronged pin exactly)
# --------------------------------------------------------------------------------------


def test_preview_module_never_imports_the_state_store() -> None:
    """Structural guarantee: this route module cannot reach a StateStore/watermark
    because it never imports one -- there is no code path left to persist one from."""
    tree = ast.parse(Path(preview_module.__file__).read_text())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any("state" in name.split(".") for name in imported_modules), imported_modules


def test_preview_calls_no_config_service_write_method() -> None:
    """A second structural guarantee: this route module never names a
    ``ConfigService`` mutation. Every attribute access on ``config_service`` in the
    source is a plain read (``get_*``/``list_*``); nothing here calls
    ``create_*``/``update_*``/``delete_*``."""
    source = Path(preview_module.__file__).read_text()
    for verb in ("create_", "update_", "delete_"):
        assert f"config_service.{verb}" not in source, verb


def test_browsing_the_source_tree_twice_yields_identical_results(tmp_path: Path) -> None:
    """If a browse request had advanced any state, a second identical request would see
    something different. It must see exactly the same tree both times -- mirrors
    ``tests/selection/test_source_tree.py``'s own
    ``test_walking_the_tree_twice_yields_the_same_nodes_both_times``."""
    source = FakeConnector.read_only_source(name=SOURCE_CONNECTOR)
    _seed_c3_example(source)
    h = _harness(tmp_path, source)
    pair_id = _setup_pair(h)
    _create_rule(h, pair_id, scope="object", decision="include", pattern="analytics.*")

    first = h.client.get(f"{API_PREFIX}/pairs/{pair_id}/source-tree", params={"scope": "object"})
    second = h.client.get(f"{API_PREFIX}/pairs/{pair_id}/source-tree", params={"scope": "object"})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


# --------------------------------------------------------------------------------------
# The C3 worked example, previewed through the API
# --------------------------------------------------------------------------------------


def test_c3_worked_example_previewed_through_the_api(tmp_path: Path) -> None:
    source = FakeConnector.read_only_source(name=SOURCE_CONNECTOR)
    _seed_c3_example(source)
    h = _harness(tmp_path, source)
    pair_id = _setup_pair(h)

    all_analytics = _create_rule(
        h, pair_id, scope="object", decision="include", pattern="analytics.*", ordinal=0
    )
    no_staging = _create_rule(
        h, pair_id, scope="object", decision="exclude", pattern="analytics.staging*", ordinal=1
    )
    keep_prod_staging = _create_rule(
        h, pair_id, scope="object", decision="include", pattern="analytics.prod_staging", ordinal=2
    )

    response = h.client.post(
        f"{API_PREFIX}/pairs/{pair_id}/preview", json={}, headers={CSRF_HEADER: h.csrf}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["rule_set_source"] == "stored"
    assert body["truncated"] is False
    assert body["counts"]["object"] == {
        "total": 4, "included": 2, "excluded": 2, "undetermined": 0,
    }
    assert body["counts"]["dataset"] == {
        "total": 5, "included": 3, "excluded": 2, "undetermined": 0,
    }

    by_name = {item["qualified_name"]: item for item in body["sample"]}
    assert set(by_name) == set(_SCHEMAS) | set(_DATASETS)

    # Included, and naming the rule that included it.
    assert by_name["analytics.sales"]["included"] is True
    assert by_name["analytics.sales"]["rule_id"] == all_analytics["id"]
    # A dataset with no dataset-scope rule of its own inherits the parent's inclusion,
    # and the sample names the *parent's* rule as the deciding one (C5).
    assert by_name["analytics.sales.orders"]["included"] is True
    assert by_name["analytics.sales.orders"]["rule_id"] == all_analytics["id"]

    # Excluded by the later "no-staging" rule (last match wins, C3).
    assert by_name["analytics.staging"]["included"] is False
    assert by_name["analytics.staging"]["rule_id"] == no_staging["id"]
    assert by_name["analytics.staging.scratch"]["included"] is False
    assert by_name["analytics.staging.scratch"]["rule_id"] == no_staging["id"]

    # Carved back in by the third rule.
    assert by_name["analytics.prod_staging"]["included"] is True
    assert by_name["analytics.prod_staging"]["rule_id"] == keep_prod_staging["id"]
    assert by_name["analytics.prod_staging.snapshots"]["included"] is True
    assert by_name["analytics.prod_staging.snapshots"]["rule_id"] == keep_prod_staging["id"]

    # A different catalog entirely: no rule matched, the default (exclude) decided, and
    # there is no rule to name.
    assert by_name["finance.reporting"]["included"] is False
    assert by_name["finance.reporting"]["rule_id"] is None
    assert by_name["finance.reporting.ledger"]["included"] is False
    assert by_name["finance.reporting.ledger"]["rule_id"] is None


async def test_preview_agrees_with_the_evaluator_directly_c4_at_the_api_layer(
    tmp_path: Path,
) -> None:
    """The C4 test at the API layer: preview the C3 rule set through the route, then
    evaluate the same candidates through :func:`walk_source_tree` directly (the exact
    function T11.3's sync loop calls through), and assert identical decisions and
    identical deciding rules."""
    source = FakeConnector.read_only_source(name=SOURCE_CONNECTOR)
    _seed_c3_example(source)
    h = _harness(tmp_path, source)
    pair_id = _setup_pair(h)

    rule_rows = [
        _create_rule(
            h, pair_id, scope="object", decision="include", pattern="analytics.*", ordinal=0
        ),
        _create_rule(
            h,
            pair_id,
            scope="object",
            decision="exclude",
            pattern="analytics.staging*",
            ordinal=1,
        ),
        _create_rule(
            h,
            pair_id,
            scope="object",
            decision="include",
            pattern="analytics.prod_staging",
            ordinal=2,
        ),
    ]

    response = h.client.post(
        f"{API_PREFIX}/pairs/{pair_id}/preview", json={}, headers={CSRF_HEADER: h.csrf}
    )
    assert response.status_code == 200, response.text
    api_sample = {item["qualified_name"]: item for item in response.json()["sample"]}

    rules = [
        SelectionRule(
            rule_id=str(row["id"]),
            ordinal=int(row["ordinal"]),  # type: ignore[arg-type]
            scope=RuleScope(row["scope"]),
            decision=SelectionDecision(row["decision"]),
            matcher_kind=MatcherKind(row["matcher_kind"]),
            pattern=str(row["pattern"]),
        )
        for row in rule_rows
    ]
    rule_set = SelectionRuleSet.build(rules)
    direct = {
        node.candidate.qualified_name: node
        async for node in walk_source_tree(source, rule_set)
    }

    assert set(api_sample) == set(direct)
    for name, api_item in api_sample.items():
        node = direct[name]
        if isinstance(node, SchemaNode):
            assert api_item["included"] == node.result.included, name
            assert api_item["rule_id"] == node.result.rule_id, name
        else:
            assert api_item["included"] == node.selection.included, name
            if (
                not node.selection.parent.included
                or node.selection.dataset.source is DecisionSource.DEFAULT
            ):
                expected_rule_id = node.selection.parent.rule_id
            else:
                expected_rule_id = node.selection.dataset.rule_id
            assert api_item["rule_id"] == expected_rule_id, name


# --------------------------------------------------------------------------------------
# A draft preview leaves the stored configuration untouched
# --------------------------------------------------------------------------------------


def test_a_draft_preview_leaves_the_stored_configuration_untouched(tmp_path: Path) -> None:
    source = FakeConnector.read_only_source(name=SOURCE_CONNECTOR)
    source.seed(DataProduct(name="sales"), native_key="analytics.sales")
    source.seed(Dataset(name="orders"), native_key="analytics.sales.orders")
    h = _harness(tmp_path, source)
    pair_id = _setup_pair(h)
    _create_rule(h, pair_id, scope="object", decision="exclude", pattern="analytics.*")

    before = h.client.get(
        f"{API_PREFIX}/pairs/{pair_id}/rules", params={"scope": "object"}
    ).json()

    draft = h.client.post(
        f"{API_PREFIX}/pairs/{pair_id}/preview",
        json={
            "rules": [
                {
                    "scope": "object",
                    "decision": "include",
                    "matcher_kind": "glob",
                    "pattern": "analytics.*",
                }
            ]
        },
        headers={CSRF_HEADER: h.csrf},
    )
    assert draft.status_code == 200, draft.text
    draft_body = draft.json()
    assert draft_body["rule_set_source"] == "draft"
    assert draft_body["counts"]["object"]["included"] == 1

    after = h.client.get(
        f"{API_PREFIX}/pairs/{pair_id}/rules", params={"scope": "object"}
    ).json()
    assert after == before, "the draft preview must not have written the stored rule list"

    stored = h.client.post(
        f"{API_PREFIX}/pairs/{pair_id}/preview", json={}, headers={CSRF_HEADER: h.csrf}
    )
    stored_body = stored.json()
    assert stored_body["rule_set_source"] == "stored"
    # The ORIGINAL exclude rule still decides -- the draft never touched it.
    assert stored_body["counts"]["object"]["excluded"] == 1
    assert stored_body["counts"]["object"]["included"] == 0


def test_a_draft_rule_with_an_invalid_pattern_is_rejected_exactly_like_saving_one_would_be(
    tmp_path: Path,
) -> None:
    """T12.4's create-rule route and this route's draft both call
    ``validate_pattern`` -- the same function, not two copies of its grammar -- so a
    pattern one refuses, the other refuses too."""
    source = FakeConnector.read_only_source(name=SOURCE_CONNECTOR)
    h = _harness(tmp_path, source)
    pair_id = _setup_pair(h)

    bad_pattern = "analytics.sales"  # dataset scope needs three segments, not two

    draft = h.client.post(
        f"{API_PREFIX}/pairs/{pair_id}/preview",
        json={
            "rules": [
                {
                    "scope": "dataset",
                    "decision": "include",
                    "matcher_kind": "glob",
                    "pattern": bad_pattern,
                }
            ]
        },
        headers={CSRF_HEADER: h.csrf},
    )
    assert draft.status_code == 422, draft.text

    saved = h.client.post(
        f"{API_PREFIX}/pairs/{pair_id}/rules",
        json={
            "scope": "dataset",
            "decision": "include",
            "matcher_kind": "glob",
            "pattern": bad_pattern,
        },
        headers={CSRF_HEADER: h.csrf},
    )
    assert saved.status_code == 422, saved.text


# --------------------------------------------------------------------------------------
# Undetermined surfaces distinctly -- never folded into "excluded"
# --------------------------------------------------------------------------------------


def test_undetermined_rules_surface_as_undetermined_not_as_exclusions(tmp_path: Path) -> None:
    """RM-01 D6: a source with no SQL warehouse configured cannot report tags at all. A
    tag rule against it must be *undetermined*, not silently read as "no tag" -- and the
    resulting exclusion (DEFAULT_DECISION) must still be counted as excluded, with
    ``undetermined`` naming that the decision might have gone the other way."""
    from qlabs_catalog_sync_sdk.testing.manifests import databricks_shaped_manifest

    source = FakeConnector.read_only_source(
        name=SOURCE_CONNECTOR, manifest=databricks_shaped_manifest(has_sql_warehouse=False)
    )
    source.seed(DataProduct(name="sales"), native_key="analytics.sales")
    h = _harness(tmp_path, source)
    pair_id = _setup_pair(h)
    _create_rule(h, pair_id, scope="object", decision="exclude", matcher_kind="tag", pattern="pii")

    response = h.client.post(
        f"{API_PREFIX}/pairs/{pair_id}/preview", json={}, headers={CSRF_HEADER: h.csrf}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # Excluded (the honest default decision) AND flagged undetermined -- never diverted
    # into some third bucket that would make "excluded" read as 0.
    assert body["counts"]["object"] == {"total": 1, "included": 0, "excluded": 1, "undetermined": 1}

    [item] = body["sample"]
    assert item["included"] is False
    assert item["has_undetermined"] is True
    assert "tag" in item["explain"] and "unknown" in item["explain"]


# --------------------------------------------------------------------------------------
# A source that will not connect is an actionable error, never a 500 and never a hang
# --------------------------------------------------------------------------------------


def test_a_source_that_cannot_be_reached_is_an_actionable_error_not_a_500(tmp_path: Path) -> None:
    source = FakeConnector.read_only_source(name=SOURCE_CONNECTOR)
    h = _harness(tmp_path, source)
    pair_id = _setup_pair(h)
    _create_rule(h, pair_id, scope="object", decision="include", pattern="analytics.*")

    source.fail_next("setup", ConnectorError("could not authenticate", endpoint=SOURCE_CONNECTOR))

    response = h.client.post(
        f"{API_PREFIX}/pairs/{pair_id}/preview", json={}, headers={CSRF_HEADER: h.csrf}
    )
    assert response.status_code != 500, response.text
    assert response.status_code == 502, response.text
    body = response.json()
    assert body["code"] == "source_unreachable"
    assert "could not authenticate" in body["message"]


def test_an_unexpected_source_error_is_a_502_never_a_leaked_traceback_or_a_500(
    tmp_path: Path,
) -> None:
    source = FakeConnector.read_only_source(name=SOURCE_CONNECTOR)
    h = _harness(tmp_path, source)
    pair_id = _setup_pair(h)

    sentinel = "SUPER-SECRET-CONNECTION-STRING-do-not-leak"
    source.fail_next("setup", RuntimeError(f"boom {sentinel}"))

    response = h.client.get(f"{API_PREFIX}/pairs/{pair_id}/source-tree")
    assert response.status_code != 500, response.text
    assert response.status_code == 502, response.text
    assert sentinel not in response.text
    assert "Traceback" not in response.text


def test_previewing_a_disabled_source_endpoint_is_a_clean_422(tmp_path: Path) -> None:
    source = FakeConnector.read_only_source(name=SOURCE_CONNECTOR)
    h = _harness(tmp_path, source)
    pair_id = _setup_pair(h)

    disable = h.client.patch(
        f"{API_PREFIX}/endpoints/{ENDPOINT_SOURCE}",
        json={"enabled": False},
        headers={CSRF_HEADER: h.csrf},
    )
    assert disable.status_code == 200, disable.text

    response = h.client.post(
        f"{API_PREFIX}/pairs/{pair_id}/preview", json={}, headers={CSRF_HEADER: h.csrf}
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "source_endpoint_disabled"

    browse = h.client.get(f"{API_PREFIX}/pairs/{pair_id}/source-tree")
    assert browse.status_code == 422, browse.text
    assert browse.json()["code"] == "source_endpoint_disabled"


def test_previewing_an_unknown_pair_is_a_404(tmp_path: Path) -> None:
    source = FakeConnector.read_only_source(name=SOURCE_CONNECTOR)
    h = _harness(tmp_path, source)

    unknown_pair_id = "00000000-0000-0000-0000-000000000000"
    preview = h.client.post(
        f"{API_PREFIX}/pairs/{unknown_pair_id}/preview", json={}, headers={CSRF_HEADER: h.csrf}
    )
    assert preview.status_code == 404, preview.text

    browse = h.client.get(f"{API_PREFIX}/pairs/{unknown_pair_id}/source-tree")
    assert browse.status_code == 404, browse.text


# --------------------------------------------------------------------------------------
# Laziness: a large source pages rather than being materialized whole, and rather than
# timing out
# --------------------------------------------------------------------------------------


def test_browsing_a_large_source_pages_rather_than_fetching_everything(tmp_path: Path) -> None:
    """The dishonest case: a route that materialized the whole tree before answering
    would fetch every page of ``list_changed`` no matter how small ``limit`` is. This
    asserts directly on the connector's own call count, not just on the response shape.
    """
    source = FakeConnector.read_only_source(name=SOURCE_CONNECTOR, list_changed_page_size=3)
    for index in range(12):
        source.seed(DataProduct(name=f"schema{index:02d}"), native_key=f"cat.schema{index:02d}")
    h = _harness(tmp_path, source)
    pair_id = _setup_pair(h)
    _create_rule(h, pair_id, scope="object", decision="include", pattern="cat.*")

    response = h.client.get(
        f"{API_PREFIX}/pairs/{pair_id}/source-tree",
        params={"scope": "object", "offset": 2, "limit": 2},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["nodes"]) == 2
    assert body["has_more"] is True
    assert body["next_offset"] == 4

    # 12 schemas at page_size=3 need 4 pages to exhaust the whole source. This request
    # only ever needed items 2, 3 and 4 (offset 2, limit 2, plus one to detect
    # has_more) -- all within the first two pages.
    assert source.call_count("list_changed") <= 2, source.call_count("list_changed")


def test_preview_truncates_honestly_when_max_candidates_is_exceeded(tmp_path: Path) -> None:
    source = FakeConnector.read_only_source(name=SOURCE_CONNECTOR)
    for index in range(10):
        source.seed(DataProduct(name=f"s{index}"), native_key=f"cat.s{index}")
    h = _harness(tmp_path, source)
    pair_id = _setup_pair(h)
    _create_rule(h, pair_id, scope="object", decision="include", pattern="cat.*")

    response = h.client.post(
        f"{API_PREFIX}/pairs/{pair_id}/preview",
        json={"max_candidates": 4},
        headers={CSRF_HEADER: h.csrf},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["truncated"] is True
    assert body["candidates_examined"] == 4
    assert body["counts"]["object"]["total"] == 4
    # The cap lands mid schema-phase (walk_source_tree's own order): the dataset phase
    # never starts. That must be visible as truncation, never silently read as "there
    # are no datasets".
    assert body["counts"]["dataset"]["total"] == 0


def test_preview_times_out_cleanly_rather_than_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preview_module, "PREVIEW_TIMEOUT_SECONDS", 0.3)

    class _SlowConnector(FakeConnector):
        async def list_changed(self, entity_type: EntityType, since: object) -> object:  # type: ignore[override]
            await asyncio.sleep(3)
            return await super().list_changed(entity_type, since)  # type: ignore[arg-type]

    source = _SlowConnector.read_only_source(name=SOURCE_CONNECTOR)
    source.seed(DataProduct(name="sales"), native_key="analytics.sales")
    h = _harness(tmp_path, source)
    pair_id = _setup_pair(h)
    _create_rule(h, pair_id, scope="object", decision="include", pattern="analytics.*")

    response = h.client.post(
        f"{API_PREFIX}/pairs/{pair_id}/preview", json={}, headers={CSRF_HEADER: h.csrf}
    )
    assert response.status_code == 504, response.text
    assert response.json()["code"] == "preview_timeout"
