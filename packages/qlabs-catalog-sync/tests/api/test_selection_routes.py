"""Selection-rule and override routes (WP12/T12.4): ``/pairs/{pair_id}/rules``,
``/pairs/{pair_id}/rules/reorder``, ``/pairs/{pair_id}/overrides``.

Drives real HTTP through a real ``create_app`` app, a real
:class:`~qlabs_catalog_sync.configstore.service.ConfigService` over a real migrated
SQLite database, and a minimal, never-instantiated stub connector registry
(``sync_pair_helpers.build_registry``) -- no mocks. Mirrors
``tests/api/test_endpoints.py``'s own fixture shape (kept file-local rather than shared,
matching that file's own convention -- see ``sync_pair_helpers.py``'s module docstring).

Three properties this suite is written to fail on a dishonest implementation of, per the
T12.4 brief:

* **Rule order is explicit, not just positional.** A list response that happened to come
  back in database-insertion order rather than true evaluation (ordinal) order would
  still "look" ordered to a test that only checks array position --
  ``test_listing_rules_returns_evaluation_order_not_insertion_order`` creates rules with
  explicit, out-of-insertion-order ordinals specifically to catch that.
* **A reorder naming an unknown or missing rule id changes nothing.**
  ``test_reorder_with_an_unknown_rule_id_is_refused_and_changes_nothing`` and
  ``test_reorder_omitting_a_rule_is_refused_and_changes_nothing`` each re-fetch the rule
  set after the refused request and assert it is byte-for-byte the original order.
* **An object-scope override cannot be pinned by an opaque id at all.** Per
  ``tests/selection/test_preview_sync_agreement.py``, that is the one case where the
  console preview and the sync loop would disagree --
  ``test_create_object_scope_override_pinned_by_opaque_id_is_refused`` proves the API
  makes that request impossible to submit in the first place, rather than merely
  discouraging it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from qlabs_catalog_sync.api.app import API_PREFIX, create_app
from qlabs_catalog_sync.api.auth import AdminCredential, ConsoleAuth
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.observability import HealthRegistry
from qlabs_catalog_sync.state.migrate import upgrade_to_head

from .sync_pair_helpers import (
    CSRF_HEADER,
    PASSWORD_HASH,
    USERNAME,
    build_registry,
    create_endpoint,
    create_pair,
    sign_in,
)

# --------------------------------------------------------------------------------------
# App-building fixtures (mirrors tests/api/test_endpoints.py's own conventions)
# --------------------------------------------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'config.db'}"
    upgrade_to_head(url)
    return url


@pytest.fixture
def registry() -> ConnectorRegistry:
    return build_registry()


@pytest.fixture
def config_service(db_url: str, registry: ConnectorRegistry) -> Iterator[ConfigService]:
    yield ConfigService.from_url(db_url, registry)


@pytest.fixture
def auth() -> ConsoleAuth:
    credential = AdminCredential.from_password_hash(PASSWORD_HASH, username=USERNAME)
    return ConsoleAuth(credential=credential)


@pytest.fixture
def app(config_service: ConfigService, registry: ConnectorRegistry, auth: ConsoleAuth) -> FastAPI:
    return create_app(
        health=HealthRegistry(),
        metrics_registry=CollectorRegistry(),
        auth=auth,
        config_service=config_service,
        registry=registry,
    )


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def signed_in_client(client: TestClient) -> tuple[TestClient, str]:
    return client, sign_in(client)


@pytest.fixture
def pair_id(signed_in_client: tuple[TestClient, str]) -> str:
    """A ready-to-use pair -- source/target endpoints plus the pair itself -- most tests
    in this file need before they can create a rule or an override."""
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")
    created = create_pair(client, csrf, name="pair-1", source="src", target="tgt")
    return str(created["id"])


# ========================================================================================
# Auth applies (a full property is tests/api/test_auth.py's job; this is a spot check)
# ========================================================================================


def test_listing_rules_requires_a_session(client: TestClient, registry: ConnectorRegistry) -> None:
    response = client.get(f"{API_PREFIX}/pairs/{uuid.uuid4()}/rules", params={"scope": "object"})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


# ========================================================================================
# Selection rules: full lifecycle, the C3 worked example, and explicit evaluation order
# ========================================================================================


def test_full_rule_lifecycle_create_read_update_delete(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    client, csrf = signed_in_client

    created = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/rules",
        json={
            "scope": "object",
            "decision": "include",
            "matcher_kind": "glob",
            "pattern": "analytics.*",
        },
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["pair_id"] == pair_id
    assert body["ordinal"] == 0
    assert body["scope"] == "object"
    assert body["decision"] == "include"
    assert body["matcher_kind"] == "glob"
    assert body["pattern"] == "analytics.*"
    rule_id = body["id"]

    fetched = client.get(f"{API_PREFIX}/pairs/{pair_id}/rules/{rule_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == rule_id

    updated = client.patch(
        f"{API_PREFIX}/pairs/{pair_id}/rules/{rule_id}",
        json={"decision": "exclude", "pattern": "analytics.staging"},
        headers={CSRF_HEADER: csrf},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["decision"] == "exclude"
    assert updated.json()["pattern"] == "analytics.staging"
    assert updated.json()["ordinal"] == 0  # unaffected by a field-only update

    deleted = client.delete(
        f"{API_PREFIX}/pairs/{pair_id}/rules/{rule_id}", headers={CSRF_HEADER: csrf}
    )
    assert deleted.status_code == 204

    gone = client.get(f"{API_PREFIX}/pairs/{pair_id}/rules/{rule_id}")
    assert gone.status_code == 404
    assert gone.json()["code"] == "selection_rule_not_found"


def _create_rule(
    client: TestClient,
    csrf: str,
    pair_id: str,
    *,
    decision: str,
    pattern: str,
    scope: str = "object",
    matcher_kind: str = "glob",
    ordinal: int | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "scope": scope,
        "decision": decision,
        "matcher_kind": matcher_kind,
        "pattern": pattern,
    }
    if ordinal is not None:
        payload["ordinal"] = ordinal
    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/rules", json=payload, headers={CSRF_HEADER: csrf}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def test_c3_worked_example_built_through_the_api_comes_back_in_evaluation_order(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    """C3's own example, built entirely over HTTP: include analytics.*, exclude
    analytics.staging*, keep analytics.prod_staging -- then read the rules back and
    assert they come out in the order that makes that mean what it says (last match
    wins: prod_staging must be *after* the staging exclusion, or it would never apply)."""
    client, csrf = signed_in_client

    all_analytics = _create_rule(client, csrf, pair_id, decision="include", pattern="analytics.*")
    no_staging = _create_rule(
        client, csrf, pair_id, decision="exclude", pattern="analytics.staging*"
    )
    keep_prod_staging = _create_rule(
        client, csrf, pair_id, decision="include", pattern="analytics.prod_staging"
    )

    listed = client.get(f"{API_PREFIX}/pairs/{pair_id}/rules", params={"scope": "object"})
    assert listed.status_code == 200
    rules = listed.json()

    assert [r["id"] for r in rules] == [
        all_analytics["id"],
        no_staging["id"],
        keep_prod_staging["id"],
    ]
    assert [r["ordinal"] for r in rules] == [0, 1, 2]
    assert [r["pattern"] for r in rules] == [
        "analytics.*",
        "analytics.staging*",
        "analytics.prod_staging",
    ]


def test_listing_rules_returns_evaluation_order_not_insertion_order(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    """The dishonest case: rules are *created* in one order but explicitly ordinalled
    into another. If the route ever returned rows in raw insertion order (or JSON array
    position were the only signal), this would either come back scrambled or silently
    agree with the wrong order -- asserting both the array order and each item's own
    ``ordinal`` field catches either failure mode."""
    client, csrf = signed_in_client

    third = _create_rule(client, csrf, pair_id, decision="include", pattern="c.*", ordinal=2)
    first = _create_rule(client, csrf, pair_id, decision="include", pattern="a.*", ordinal=0)
    second = _create_rule(client, csrf, pair_id, decision="include", pattern="b.*", ordinal=1)

    listed = client.get(f"{API_PREFIX}/pairs/{pair_id}/rules", params={"scope": "object"})
    rules = listed.json()

    assert [r["id"] for r in rules] == [first["id"], second["id"], third["id"]]
    assert [r["ordinal"] for r in rules] == [0, 1, 2]


def test_listing_rules_for_an_unknown_pair_is_a_404_not_an_empty_list(
    signed_in_client: tuple[TestClient, str],
) -> None:
    """A silent empty list for a pair that does not exist would be indistinguishable
    from "this pair genuinely has no rules yet"."""
    client, _csrf = signed_in_client
    response = client.get(f"{API_PREFIX}/pairs/{uuid.uuid4()}/rules", params={"scope": "object"})
    assert response.status_code == 404
    assert response.json()["code"] == "sync_pair_not_found"


def test_getting_a_rule_through_the_wrong_pair_is_not_found(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src2", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt2", connector="qlik", role="target")
    other_pair = create_pair(client, csrf, name="pair-2", source="src2", target="tgt2")

    rule = _create_rule(client, csrf, pair_id, decision="include", pattern="a.*")

    response = client.get(f"{API_PREFIX}/pairs/{other_pair['id']}/rules/{rule['id']}")
    assert response.status_code == 404
    assert response.json()["code"] == "selection_rule_not_found"


# ========================================================================================
# Reordering: atomic, and refused whole on an incoherent request
# ========================================================================================


def test_reorder_moves_last_rule_to_first(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    client, csrf = signed_in_client
    rules = [
        _create_rule(client, csrf, pair_id, decision="include", pattern=p)
        for p in ("a.*", "b.*", "c.*")
    ]

    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/rules/reorder",
        json={"scope": "object", "rule_ids": [rules[2]["id"], rules[0]["id"], rules[1]["id"]]},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 200, response.text
    reordered = response.json()
    assert [r["id"] for r in reordered] == [rules[2]["id"], rules[0]["id"], rules[1]["id"]]
    assert [r["ordinal"] for r in reordered] == [0, 1, 2]

    # And it is durable, not just the response of the reorder call itself.
    listed = client.get(f"{API_PREFIX}/pairs/{pair_id}/rules", params={"scope": "object"})
    assert [r["id"] for r in listed.json()] == [rules[2]["id"], rules[0]["id"], rules[1]["id"]]


def test_reorder_with_an_unknown_rule_id_is_refused_and_changes_nothing(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    client, csrf = signed_in_client
    rules = [
        _create_rule(client, csrf, pair_id, decision="include", pattern=p) for p in ("a.*", "b.*")
    ]
    original = client.get(f"{API_PREFIX}/pairs/{pair_id}/rules", params={"scope": "object"}).json()

    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/rules/reorder",
        json={"scope": "object", "rule_ids": [rules[0]["id"], str(uuid.uuid4())]},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "selection_rule_reorder_mismatch"

    unchanged = client.get(f"{API_PREFIX}/pairs/{pair_id}/rules", params={"scope": "object"}).json()
    assert unchanged == original


def test_reorder_omitting_a_rule_is_refused_and_changes_nothing(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    """A partial reorder -- naming only some of the rule set -- is exactly the
    half-applied-order failure T12.4 must avoid; the service refuses it whole rather
    than moving the named rules and leaving the rest wherever they were."""
    client, csrf = signed_in_client
    rules = [
        _create_rule(client, csrf, pair_id, decision="include", pattern=p)
        for p in ("a.*", "b.*", "c.*")
    ]
    original = client.get(f"{API_PREFIX}/pairs/{pair_id}/rules", params={"scope": "object"}).json()

    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/rules/reorder",
        json={"scope": "object", "rule_ids": [rules[1]["id"], rules[0]["id"]]},  # rules[2] missing
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "selection_rule_reorder_mismatch"

    unchanged = client.get(f"{API_PREFIX}/pairs/{pair_id}/rules", params={"scope": "object"}).json()
    assert unchanged == original


# ========================================================================================
# Pattern validation: reused from qlabs_catalog_sync.selection.rules, actionable message
# ========================================================================================


def test_create_rule_with_a_scope_mismatched_pattern_is_refused_with_an_actionable_message(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    """An object-scope pattern needs exactly two segments; three (a dataset-shaped
    pattern) is refused rather than silently never matching."""
    client, csrf = signed_in_client
    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/rules",
        json={
            "scope": "object",
            "decision": "include",
            "matcher_kind": "glob",
            "pattern": "analytics.sales.orders",
        },
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "selection_rule_pattern_invalid"
    message = response.json()["message"]
    assert "analytics.sales.orders" in message
    assert "2" in message  # names the expected segment count, not just "invalid"


def test_create_rule_with_a_dataset_scope_two_segment_pattern_is_refused(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    client, csrf = signed_in_client
    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/rules",
        json={
            "scope": "dataset",
            "decision": "include",
            "matcher_kind": "glob",
            "pattern": "analytics.sales",
        },
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "selection_rule_pattern_invalid"


def test_update_rule_pattern_is_revalidated_against_its_existing_scope(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    client, csrf = signed_in_client
    rule = _create_rule(client, csrf, pair_id, decision="include", pattern="analytics.*")

    response = client.patch(
        f"{API_PREFIX}/pairs/{pair_id}/rules/{rule['id']}",
        json={"pattern": "analytics.sales.orders"},  # three segments, but this rule is object-scope
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "selection_rule_pattern_invalid"

    # The rule was never actually changed.
    unchanged = client.get(f"{API_PREFIX}/pairs/{pair_id}/rules/{rule['id']}")
    assert unchanged.json()["pattern"] == "analytics.*"


# ========================================================================================
# Overrides: full lifecycle, and the constraint that matters most in T12.4
# ========================================================================================


def test_full_override_lifecycle_create_read_update_delete(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    client, csrf = signed_in_client

    created = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/overrides",
        json={
            "scope": "object",
            "object_id": "analytics.prod_staging",
            "decision": "include",
            "reason": "keep despite the exclude rule",
        },
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["pair_id"] == pair_id
    assert body["object_id"] == "analytics.prod_staging"
    assert body["decision"] == "include"
    assert body["reason"] == "keep despite the exclude rule"
    override_id = body["id"]

    fetched = client.get(f"{API_PREFIX}/pairs/{pair_id}/overrides/{override_id}")
    assert fetched.status_code == 200
    assert fetched.json()["object_id"] == "analytics.prod_staging"

    listed = client.get(f"{API_PREFIX}/pairs/{pair_id}/overrides", params={"scope": "object"})
    assert [o["object_id"] for o in listed.json()] == ["analytics.prod_staging"]

    updated = client.patch(
        f"{API_PREFIX}/pairs/{pair_id}/overrides/{override_id}",
        json={"decision": "exclude", "reason": None},
        headers={CSRF_HEADER: csrf},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["decision"] == "exclude"
    assert updated.json()["reason"] is None
    assert updated.json()["object_id"] == "analytics.prod_staging"  # identity unchanged

    deleted = client.delete(
        f"{API_PREFIX}/pairs/{pair_id}/overrides/{override_id}", headers={CSRF_HEADER: csrf}
    )
    assert deleted.status_code == 204

    gone = client.get(f"{API_PREFIX}/pairs/{pair_id}/overrides/{override_id}")
    assert gone.status_code == 404
    assert gone.json()["code"] == "selection_override_not_found"


def test_create_duplicate_override_is_a_clear_conflict(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    client, csrf = signed_in_client
    payload = {"scope": "object", "object_id": "analytics.prod_staging", "decision": "include"}
    first = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/overrides", json=payload, headers={CSRF_HEADER: csrf}
    )
    assert first.status_code == 201

    second = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/overrides", json=payload, headers={CSRF_HEADER: csrf}
    )
    assert second.status_code == 409
    assert second.json()["code"] == "selection_override_already_exists"


def test_create_object_scope_override_pinned_by_opaque_id_is_refused(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    """THE constraint this task exists to enforce. ``tests/selection/
    test_preview_sync_agreement.py`` proves that a schema pinned by its opaque stable id
    (rather than its ``catalog.schema`` qualified name) is honoured by the console
    preview and silently invisible to the sync loop -- the console would promise a sync
    that never happens. This route must make that request impossible to submit, not
    merely inadvisable."""
    client, csrf = signed_in_client
    opaque_schema_id = "8f14e45f-ea8d-4b1c-9f6a-2c3d4e5f6a7b"  # dot-free, exactly how a
    # real stable id (e.g. a Databricks schema_id) is shaped -- never a qualified name.

    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/overrides",
        json={"scope": "object", "object_id": opaque_schema_id, "decision": "include"},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "selection_override_object_id_invalid"
    message = response.json()["message"]
    assert "qualified name" in message
    assert opaque_schema_id in message

    # And it was genuinely never created.
    listed = client.get(f"{API_PREFIX}/pairs/{pair_id}/overrides", params={"scope": "object"})
    assert listed.json() == []


def test_create_object_scope_override_pinned_by_qualified_name_succeeds(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    """The documented, honoured-by-both-paths shape: exactly two segments."""
    client, csrf = signed_in_client
    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/overrides",
        json={"scope": "object", "object_id": "finance.reporting", "decision": "include"},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 201, response.text
    assert response.json()["object_id"] == "finance.reporting"


def test_create_dataset_scope_override_with_only_two_segments_is_refused(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    client, csrf = signed_in_client
    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/overrides",
        json={"scope": "dataset", "object_id": "analytics.sales", "decision": "include"},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "selection_override_object_id_invalid"


def test_create_dataset_scope_override_with_three_segments_succeeds(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    client, csrf = signed_in_client
    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/overrides",
        json={"scope": "dataset", "object_id": "analytics.sales.orders", "decision": "include"},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 201, response.text


def test_create_override_with_a_glob_pattern_instead_of_a_literal_name_is_refused(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    """An override pins one exact object; a wildcard would silently mean something
    different from what the field name (``object_id``) promises."""
    client, csrf = signed_in_client
    response = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/overrides",
        json={"scope": "object", "object_id": "analytics.*", "decision": "include"},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "selection_override_object_id_invalid"


def test_getting_an_override_through_the_wrong_pair_is_not_found(
    signed_in_client: tuple[TestClient, str], pair_id: str
) -> None:
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src2", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt2", connector="qlik", role="target")
    other_pair = create_pair(client, csrf, name="pair-2", source="src2", target="tgt2")

    override = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/overrides",
        json={"scope": "object", "object_id": "analytics.sales", "decision": "include"},
        headers={CSRF_HEADER: csrf},
    ).json()

    response = client.get(f"{API_PREFIX}/pairs/{other_pair['id']}/overrides/{override['id']}")
    assert response.status_code == 404
    assert response.json()["code"] == "selection_override_not_found"


def test_listing_overrides_for_an_unknown_pair_is_a_404_not_an_empty_list(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, _csrf = signed_in_client
    response = client.get(
        f"{API_PREFIX}/pairs/{uuid.uuid4()}/overrides", params={"scope": "object"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "sync_pair_not_found"


# ========================================================================================
# Every mutation appears in the change log (T12.4 DoD)
# ========================================================================================


async def test_rule_and_override_mutations_are_recorded_in_the_change_log(
    signed_in_client: tuple[TestClient, str], pair_id: str, config_service: ConfigService
) -> None:
    """Every route in this module calls straight into ``ConfigService``, which is what
    actually appends to ``config_changes`` and bumps the generation counter
    (``configstore/audit.py``) -- this proves the wiring is real for rules, reorder, and
    overrides alike, not just asserted by construction."""
    client, csrf = signed_in_client

    generation = await config_service.current_generation()
    rules = [
        _create_rule(client, csrf, pair_id, decision="include", pattern=p) for p in ("a.*", "b.*")
    ]
    assert await config_service.current_generation() > generation

    generation = await config_service.current_generation()
    reorder = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/rules/reorder",
        json={"scope": "object", "rule_ids": [rules[1]["id"], rules[0]["id"]]},
        headers={CSRF_HEADER: csrf},
    )
    assert reorder.status_code == 200, reorder.text
    assert await config_service.current_generation() > generation

    generation = await config_service.current_generation()
    delete_rule = client.delete(
        f"{API_PREFIX}/pairs/{pair_id}/rules/{rules[0]['id']}", headers={CSRF_HEADER: csrf}
    )
    assert delete_rule.status_code == 204
    assert await config_service.current_generation() > generation

    generation = await config_service.current_generation()
    override = client.post(
        f"{API_PREFIX}/pairs/{pair_id}/overrides",
        json={"scope": "object", "object_id": "analytics.sales", "decision": "include"},
        headers={CSRF_HEADER: csrf},
    )
    assert override.status_code == 201, override.text
    assert await config_service.current_generation() > generation

    generation = await config_service.current_generation()
    update_override = client.patch(
        f"{API_PREFIX}/pairs/{pair_id}/overrides/{override.json()['id']}",
        json={"decision": "exclude"},
        headers={CSRF_HEADER: csrf},
    )
    assert update_override.status_code == 200, update_override.text
    assert await config_service.current_generation() > generation

    generation = await config_service.current_generation()
    delete_override = client.delete(
        f"{API_PREFIX}/pairs/{pair_id}/overrides/{override.json()['id']}",
        headers={CSRF_HEADER: csrf},
    )
    assert delete_override.status_code == 204
    assert await config_service.current_generation() > generation
