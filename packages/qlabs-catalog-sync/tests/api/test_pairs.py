"""Sync-pair routes (WP12/T12.4): ``/pairs``.

Drives real HTTP through a real ``create_app`` app, a real
:class:`~qlabs_catalog_sync.configstore.service.ConfigService` over a real migrated
SQLite database, and a minimal, never-instantiated stub connector registry
(``sync_pair_helpers.build_registry``) -- no mocks. Mirrors
``tests/api/test_endpoints.py``'s own fixture shape (kept file-local rather than shared,
matching that file's own convention -- see ``sync_pair_helpers.py``'s module docstring).

Covers the full pair lifecycle, the v1 upstream-only direction guardrail surfacing as a
clear 4xx (``ConfigService`` already enforces it; this suite proves the route does not
swallow or reword it), and the T12.4 DoD's "a pair referencing a disabled or missing
endpoint is rejected" -- the "missing" half is ``ConfigService``'s own
``SyncPairEndpointError``; the "disabled" half is ``routes/pairs.py``'s own pre-write
check (see that module's docstring for why it lives there and not in
``ConfigService``).
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


# ========================================================================================
# Auth applies (a full property is tests/api/test_auth.py's job; this is a spot check)
# ========================================================================================


def test_listing_pairs_requires_a_session(client: TestClient) -> None:
    response = client.get(f"{API_PREFIX}/pairs")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_creating_a_pair_without_a_csrf_token_is_refused(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, _csrf = signed_in_client
    response = client.post(
        f"{API_PREFIX}/pairs",
        json={"name": "p", "source": "s", "target": "t", "target_space": "personal"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_token_invalid"


# ========================================================================================
# Full lifecycle
# ========================================================================================


def test_full_pair_lifecycle_create_read_list_update_delete(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")

    created = create_pair(
        client,
        csrf,
        name="pair-1",
        source="src",
        target="tgt",
        target_space="personal",
        entity_types=["data_product", "dataset"],
        cadence_seconds=600,
        jitter_seconds=30.0,
        manual_edit_policy={"default": "preserve_local", "per_entity": {}, "per_field": {}},
        activation_opt_in=True,
        enabled=True,
    )
    assert created["name"] == "pair-1"
    assert created["source"] == "src"
    assert created["target"] == "tgt"
    assert created["target_space"] == "personal"
    assert created["entity_types"] == ["data_product", "dataset"]
    assert created["cadence_seconds"] == 600
    assert created["jitter_seconds"] == 30.0
    assert created["manual_edit_policy"]["default"] == "preserve_local"
    assert created["activation_opt_in"] is True
    assert created["enabled"] is True
    pair_id = created["id"]
    uuid.UUID(pair_id)  # a real, opaque id -- never guessed at by the client

    fetched = client.get(f"{API_PREFIX}/pairs/{pair_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == pair_id

    listed = client.get(f"{API_PREFIX}/pairs")
    assert [p["name"] for p in listed.json()] == ["pair-1"]

    updated = client.patch(
        f"{API_PREFIX}/pairs/{pair_id}",
        json={"cadence_seconds": 1200, "enabled": False},
        headers={CSRF_HEADER: csrf},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["cadence_seconds"] == 1200
    assert updated.json()["enabled"] is False
    # Untouched fields survive a partial update.
    assert updated.json()["source"] == "src"
    assert updated.json()["target_space"] == "personal"

    # jitter_seconds is nullable: explicit null clears it, other fields stay put.
    cleared = client.patch(
        f"{API_PREFIX}/pairs/{pair_id}",
        json={"jitter_seconds": None},
        headers={CSRF_HEADER: csrf},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["jitter_seconds"] is None
    assert cleared.json()["cadence_seconds"] == 1200  # unaffected

    deleted = client.delete(f"{API_PREFIX}/pairs/{pair_id}", headers={CSRF_HEADER: csrf})
    assert deleted.status_code == 204

    gone = client.get(f"{API_PREFIX}/pairs/{pair_id}")
    assert gone.status_code == 404
    assert gone.json()["code"] == "sync_pair_not_found"


def test_get_update_delete_unknown_pair_is_a_clear_404(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    missing = str(uuid.uuid4())

    get_response = client.get(f"{API_PREFIX}/pairs/{missing}")
    assert get_response.status_code == 404
    assert get_response.json()["code"] == "sync_pair_not_found"

    patch_response = client.patch(
        f"{API_PREFIX}/pairs/{missing}",
        json={"enabled": True},
        headers={CSRF_HEADER: csrf},
    )
    assert patch_response.status_code == 404
    assert patch_response.json()["code"] == "sync_pair_not_found"

    delete_response = client.delete(f"{API_PREFIX}/pairs/{missing}", headers={CSRF_HEADER: csrf})
    assert delete_response.status_code == 404
    assert delete_response.json()["code"] == "sync_pair_not_found"


def test_create_pair_duplicate_name_is_a_clear_conflict(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")
    create_pair(client, csrf, name="dupe", source="src", target="tgt")

    response = client.post(
        f"{API_PREFIX}/pairs",
        json={"name": "dupe", "source": "src", "target": "tgt", "target_space": "personal"},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "sync_pair_already_exists"


# ========================================================================================
# Endpoint reference guardrails: missing, backwards direction, disabled
# ========================================================================================


def test_create_pair_missing_endpoint_is_refused(signed_in_client: tuple[TestClient, str]) -> None:
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")

    response = client.post(
        f"{API_PREFIX}/pairs",
        json={
            "name": "pair-1",
            "source": "does-not-exist",
            "target": "tgt",
            "target_space": "personal",
        },
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "sync_pair_endpoint_invalid"
    assert "does-not-exist" in response.json()["message"]


def test_create_pair_with_qlik_as_source_is_refused(
    signed_in_client: tuple[TestClient, str],
) -> None:
    """The v1 upstream-only guardrail: Qlik can never be a sync source."""
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="backwards-src", connector="qlik", role="source")
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")

    response = client.post(
        f"{API_PREFIX}/pairs",
        json={
            "name": "pair-1",
            "source": "backwards-src",
            "target": "tgt",
            "target_space": "personal",
        },
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "sync_pair_endpoint_invalid"
    message = response.json()["message"]
    assert "never be a sync source" in message  # an operator can act on this


def test_create_pair_with_a_non_qlik_target_is_refused(
    signed_in_client: tuple[TestClient, str],
) -> None:
    """The v1 upstream-only guardrail: the only write target is the Qlik connector."""
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source")
    create_endpoint(client, csrf, name="wrong-tgt", connector="source", role="target")

    response = client.post(
        f"{API_PREFIX}/pairs",
        json={
            "name": "pair-1",
            "source": "src",
            "target": "wrong-tgt",
            "target_space": "personal",
        },
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "sync_pair_endpoint_invalid"
    message = response.json()["message"]
    assert "qlik" in message.lower()


def test_create_pair_referencing_a_disabled_source_endpoint_is_refused(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source", enabled=False)
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")

    response = client.post(
        f"{API_PREFIX}/pairs",
        json={"name": "pair-1", "source": "src", "target": "tgt", "target_space": "personal"},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "sync_pair_endpoint_invalid"
    message = response.json()["message"]
    assert "src" in message and "disabled" in message  # actionable, not "invalid pair"

    # And the pair was genuinely never created.
    assert client.get(f"{API_PREFIX}/pairs").json() == []


def test_create_pair_referencing_a_disabled_target_endpoint_is_refused(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target", enabled=False)

    response = client.post(
        f"{API_PREFIX}/pairs",
        json={"name": "pair-1", "source": "src", "target": "tgt", "target_space": "personal"},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "sync_pair_endpoint_invalid"
    assert "tgt" in response.json()["message"]
    assert "disabled" in response.json()["message"]


def test_update_pair_to_point_at_a_disabled_endpoint_is_refused_and_changes_nothing(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")
    create_endpoint(
        client, csrf, name="other-src", connector="source", role="source", enabled=False
    )
    created = create_pair(client, csrf, name="pair-1", source="src", target="tgt")
    pair_id = created["id"]

    response = client.patch(
        f"{API_PREFIX}/pairs/{pair_id}",
        json={"source": "other-src"},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "sync_pair_endpoint_invalid"

    # The pair's source is untouched by the refused update.
    unchanged = client.get(f"{API_PREFIX}/pairs/{pair_id}")
    assert unchanged.json()["source"] == "src"


def test_update_pair_leaving_source_and_target_alone_does_not_re_check_them(
    signed_in_client: tuple[TestClient, str],
) -> None:
    """A field-only update (no source/target in the request) must not be blocked by an
    endpoint that became disabled after the pair was created -- only a write that
    actually touches source/target re-runs the endpoint checks (mirrors
    ``ConfigService.update_sync_pair``'s own "only re-check what changed" behavior)."""
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")
    created = create_pair(client, csrf, name="pair-1", source="src", target="tgt")
    pair_id = created["id"]

    # Disable the source endpoint after the pair already references it.
    disable = client.patch(
        f"{API_PREFIX}/endpoints/src", json={"enabled": False}, headers={CSRF_HEADER: csrf}
    )
    assert disable.status_code == 200, disable.text

    response = client.patch(
        f"{API_PREFIX}/pairs/{pair_id}",
        json={"cadence_seconds": 1800},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 200, response.text
    assert response.json()["cadence_seconds"] == 1800


# ========================================================================================
# Request-level validation: never a raw 500 for an obviously-bad value
# ========================================================================================


async def test_pair_mutations_are_recorded_in_the_change_log(
    signed_in_client: tuple[TestClient, str], config_service: ConfigService
) -> None:
    """T12.4's DoD: "every mutation appears in the change log." Every route in this
    module calls straight into ``ConfigService``, which is what actually appends to
    ``config_changes`` and bumps the generation counter (``configstore/audit.py``) --
    this proves the wiring is real, not just asserted by construction."""
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")

    generation_before_create = await config_service.current_generation()
    created = create_pair(client, csrf, name="pair-1", source="src", target="tgt")
    assert await config_service.current_generation() > generation_before_create

    generation_before_update = await config_service.current_generation()
    update = client.patch(
        f"{API_PREFIX}/pairs/{created['id']}",
        json={"enabled": True},
        headers={CSRF_HEADER: csrf},
    )
    assert update.status_code == 200, update.text
    assert await config_service.current_generation() > generation_before_update

    generation_before_delete = await config_service.current_generation()
    delete = client.delete(f"{API_PREFIX}/pairs/{created['id']}", headers={CSRF_HEADER: csrf})
    assert delete.status_code == 204
    assert await config_service.current_generation() > generation_before_delete


def test_create_pair_non_positive_cadence_is_a_clean_422_not_a_500(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    create_endpoint(client, csrf, name="src", connector="source", role="source")
    create_endpoint(client, csrf, name="tgt", connector="qlik", role="target")

    response = client.post(
        f"{API_PREFIX}/pairs",
        json={
            "name": "pair-1",
            "source": "src",
            "target": "tgt",
            "target_space": "personal",
            "cadence_seconds": 0,
        },
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_error"
