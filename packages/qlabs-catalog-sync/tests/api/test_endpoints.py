"""Connector and endpoint routes (C6, WP12/T12.3): ``/connectors``, ``/endpoints``.

Drives real HTTP through a real ``create_app`` app, a real
:class:`~qlabs_catalog_sync.configstore.service.ConfigService` over a real migrated
SQLite database, and a real :class:`~qlabs_catalog_sync.discovery.ConnectorRegistry` of
:class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` instances -- no mocks. This suite
owns no shared helper module of its own (a single test file did not need one); every
fixture below is file-local, mirroring the pattern ``tests/api/test_auth.py`` and
``tests/configstore/conftest.py`` each already use.

Every "must not leak" assertion below looks for a distinctive sentinel string in a raw
HTTP response body, mirroring ``tests/api/test_auth.py``'s own sentinel convention for
the same kind of claim.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pydantic import SecretStr

from qlabs_catalog_sync.api.app import API_PREFIX, create_app
from qlabs_catalog_sync.api.auth import (
    AUTH_SESSION_ROUTE,
    CSRF_HEADER,
    AdminCredential,
    ConsoleAuth,
    ScryptParams,
    hash_password,
)
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.discovery import BrokenConnector, ConnectorRegistry
from qlabs_catalog_sync.observability import HealthRegistry
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync_sdk.config import ConnectorConfig, ConnectorContext
from qlabs_catalog_sync_sdk.contract import (
    CapabilityManifestBase,
    Connector,
    HealthStatus,
    IdentityRef,
    ListChangedResult,
    Watermark,
)
from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_catalog_sync_sdk.models import EntityType, NeutralEntity
from qlabs_catalog_sync_sdk.testing import FakeConnector
from qlabs_catalog_sync_sdk.testing.manifests import databricks_shaped_manifest

# --------------------------------------------------------------------------------------
# Auth fixtures (mirrors tests/api/test_auth.py's own conventions, at the cheap cost
# floor -- this suite does not test auth itself, only that it applies)
# --------------------------------------------------------------------------------------

USERNAME = "console-operator"
PASSWORD = "SENTINEL-t12-3-endpoint-routes-admin-password"
_TEST_SCRYPT_PARAMS = ScryptParams(log_n=14, r=8, p=1)
PASSWORD_HASH = hash_password(PASSWORD, params=_TEST_SCRYPT_PARAMS)

SESSION_PATH = f"{API_PREFIX}{AUTH_SESSION_ROUTE}"


# --------------------------------------------------------------------------------------
# A connector with a real secret-typed field -- FakeConnector's own ConfigModel is
# deliberately empty (it needs no settings or secrets, see its own docstring), so
# exercising inline-secret-rejection and secret-resolve needs a connector that actually
# declares one. Never called beyond capabilities()/ConfigModel in this suite: every
# healthcheck test below runs against "fake" (a real FakeConnector), not this stub.
# --------------------------------------------------------------------------------------


class _SecretConfig(ConnectorConfig):
    """One required ``SecretStr`` field -- enough to exercise inline-secret-rejection
    and a real resolve-status query."""

    api_key: SecretStr


class _SecretConnector(Connector):
    name = "secrety"
    ConfigModel = _SecretConfig

    def capabilities(self) -> CapabilityManifestBase:
        return databricks_shaped_manifest()

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        raise NotImplementedError

    async def healthcheck(self) -> HealthStatus:
        raise NotImplementedError

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        raise NotImplementedError

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        raise NotImplementedError


def _wrap_as_class(instance: Connector) -> type[Connector]:
    """Wrap an already-built connector instance as a zero-argument-constructible class.

    A file-local copy of ``tests/cli/cli_helpers.py``'s ``wrap_as_class`` (not owned by
    this file, and this suite lives in a different test directory) -- same mechanism,
    same reason: the routes under test build a connector the production way
    (``registry.get_connector(name)()``, zero arguments, mirroring
    ``cli/wiring.py``'s ``build_connector_pool``), and this is what lets a test still
    hold a handle on the *exact* instance that call produces, to seed ``fail_next`` on
    before driving the route over HTTP.
    """
    base = type(instance)

    class _Wrapped(base):  # type: ignore[misc, valid-type]
        def __new__(cls) -> Connector:
            return instance

    return _Wrapped


# --------------------------------------------------------------------------------------
# App-building fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """A migrated temp-file SQLite database -- same helper T2.2/T10.1 tests use."""
    url = f"sqlite:///{tmp_path / 'config.db'}"
    upgrade_to_head(url)
    return url


@pytest.fixture
def fake_connector() -> FakeConnector:
    """A real, working connector -- what the DoD means by "against FakeConnector"."""
    return FakeConnector.named("fake", manifest=databricks_shaped_manifest())


@pytest.fixture
def qlik_connector() -> FakeConnector:
    """A Qlik-shaped write target named ``"qlik"`` -- the only connector name the v1
    direction guardrail accepts as a sync pair's target (``WRITE_CONNECTOR_NAME``),
    needed for the "endpoint still used by a pair" delete-refusal test."""
    return FakeConnector.write_target(name="qlik")


@pytest.fixture
def broken_connector() -> BrokenConnector:
    return BrokenConnector(
        name="broken-conn",
        distribution="acme-broken-connector-dist",
        stage="load",
        reason="ImportError: no module named 'acme_broken_connector'",
    )


@pytest.fixture
def registry(
    fake_connector: FakeConnector,
    qlik_connector: FakeConnector,
    broken_connector: BrokenConnector,
) -> ConnectorRegistry:
    return ConnectorRegistry(
        {
            "fake": _wrap_as_class(fake_connector),
            "qlik": _wrap_as_class(qlik_connector),
            "secrety": _SecretConnector,
        },
        {"broken-conn": broken_connector},
    )


@pytest.fixture
def config_service(db_url: str, registry: ConnectorRegistry) -> Iterator[ConfigService]:
    svc = ConfigService.from_url(db_url, registry)
    yield svc


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


def _sign_in(client: TestClient) -> str:
    response = client.post(SESSION_PATH, json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()["csrf_token"]
    assert isinstance(token, str) and token
    return token


@pytest.fixture
def signed_in_client(client: TestClient) -> tuple[TestClient, str]:
    """A client with a live session, plus the CSRF token every mutating call needs."""
    return client, _sign_in(client)


# ========================================================================================
# GET /connectors (C6: list what discovery found)
# ========================================================================================


def test_list_connectors_shows_available_connectors_with_their_manifest(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, _ = signed_in_client
    response = client.get(f"{API_PREFIX}/connectors")
    assert response.status_code == 200, response.text

    by_name = {item["name"]: item for item in response.json()}
    assert set(by_name) == {"fake", "qlik", "secrety", "broken-conn"}

    fake = by_name["fake"]
    assert fake["available"] is True
    assert fake["broken_reason"] is None
    assert fake["manifest"]["concurrency"] in {"etag", "revision", "none"}
    assert "data_product" in fake["manifest"]["entities"]
    data_product = fake["manifest"]["entities"]["data_product"]
    assert data_product["supported"] is True
    assert data_product["identity_keys"]
    # Read-only source guardrail: every field mode is "ro" or "na", never "rw".
    assert {f["mode"] for f in data_product["fields"].values()} <= {"ro", "na"}


def test_a_broken_connector_appears_in_the_list_with_its_reason_rather_than_vanishing(
    signed_in_client: tuple[TestClient, str],
) -> None:
    """The dishonest case: a broken entry point must be listed, never silently omitted."""
    client, _ = signed_in_client
    response = client.get(f"{API_PREFIX}/connectors")
    by_name = {item["name"]: item for item in response.json()}

    broken = by_name["broken-conn"]
    assert broken["available"] is False
    assert broken["manifest"] is None
    assert broken["distribution"] == "acme-broken-connector-dist"
    assert broken["broken_stage"] == "load"
    assert "ImportError" in broken["broken_reason"]


def test_listing_connectors_requires_a_session(client: TestClient) -> None:
    response = client.get(f"{API_PREFIX}/connectors")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


# ========================================================================================
# Full endpoint lifecycle (C6): register, read, update, delete
# ========================================================================================


def test_full_endpoint_lifecycle_register_read_update_delete(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client

    created = client.post(
        f"{API_PREFIX}/endpoints",
        json={
            "name": "fake_prod",
            "connector": "fake",
            "role": "source",
            "settings": {},
        },
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "fake_prod"
    assert body["connector"] == "fake"
    assert body["role"] == "source"
    assert body["secret_ref"] is None
    assert body["enabled"] is False

    fetched = client.get(f"{API_PREFIX}/endpoints/fake_prod")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "fake_prod"

    listed = client.get(f"{API_PREFIX}/endpoints")
    assert [e["name"] for e in listed.json()] == ["fake_prod"]

    updated = client.patch(
        f"{API_PREFIX}/endpoints/fake_prod",
        json={"enabled": True, "secret_ref": "env:FAKE_PROD"},
        headers={CSRF_HEADER: csrf},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["enabled"] is True
    assert updated.json()["secret_ref"] == "env:FAKE_PROD"
    # Untouched fields survive a partial update.
    assert updated.json()["connector"] == "fake"

    # secret_ref=null explicitly clears the reference; other fields stay put.
    cleared = client.patch(
        f"{API_PREFIX}/endpoints/fake_prod",
        json={"secret_ref": None},
        headers={CSRF_HEADER: csrf},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["secret_ref"] is None
    assert cleared.json()["enabled"] is True  # unaffected by the secret_ref-only update

    deleted = client.delete(f"{API_PREFIX}/endpoints/fake_prod", headers={CSRF_HEADER: csrf})
    assert deleted.status_code == 204

    gone = client.get(f"{API_PREFIX}/endpoints/fake_prod")
    assert gone.status_code == 404
    assert gone.json()["code"] == "endpoint_not_found"


def test_create_endpoint_duplicate_name_is_a_clear_conflict(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    payload = {"name": "dupe", "connector": "fake", "role": "source", "settings": {}}
    first = client.post(f"{API_PREFIX}/endpoints", json=payload, headers={CSRF_HEADER: csrf})
    assert first.status_code == 201

    second = client.post(f"{API_PREFIX}/endpoints", json=payload, headers={CSRF_HEADER: csrf})
    assert second.status_code == 409
    assert second.json()["code"] == "endpoint_already_exists"


def test_create_endpoint_unknown_connector_is_a_clear_error(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    response = client.post(
        f"{API_PREFIX}/endpoints",
        json={"name": "x", "connector": "does-not-exist", "role": "source", "settings": {}},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "connector_not_registered"


def test_create_endpoint_against_a_broken_connector_is_a_clear_error_not_a_health_result(
    signed_in_client: tuple[TestClient, str],
) -> None:
    """A broken connector cannot even be registered -- this is a registration problem,
    never something rendered as a red healthcheck (see routes/endpoints.py's module
    docstring)."""
    client, csrf = signed_in_client
    response = client.post(
        f"{API_PREFIX}/endpoints",
        json={"name": "x", "connector": "broken-conn", "role": "source", "settings": {}},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "connector_broken"


def test_healthcheck_for_an_unknown_endpoint_is_404(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    response = client.post(
        f"{API_PREFIX}/endpoints/does-not-exist/healthcheck", headers={CSRF_HEADER: csrf}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "endpoint_not_found"


# ========================================================================================
# DoD: invalid settings rejected with field-level errors from the connector's own
# ConfigModel
# ========================================================================================


def test_create_endpoint_rejects_invalid_settings_naming_the_bad_field(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    response = client.post(
        f"{API_PREFIX}/endpoints",
        json={
            "name": "bad_settings",
            "connector": "fake",
            "role": "source",
            # FakeConnectorConfig declares no fields at all (extra="forbid").
            "settings": {"nonexistent_field": "x"},
        },
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "endpoint_settings_invalid"
    assert "nonexistent_field" in body["message"]

    # And nothing was persisted.
    listed = client.get(f"{API_PREFIX}/endpoints")
    assert listed.json() == []


# ========================================================================================
# DoD / C2: a secret submitted inline must be refused, not stored
# ========================================================================================


def test_create_endpoint_rejects_an_inline_secret_rather_than_storing_it(
    signed_in_client: tuple[TestClient, str],
) -> None:
    """The dishonest case: a credential typed into ``settings`` must be refused as a
    clear 4xx -- never silently stored, never a 500."""
    client, csrf = signed_in_client
    sentinel = "sk-t12-3-inline-secret-should-never-be-stored"
    response = client.post(
        f"{API_PREFIX}/endpoints",
        json={
            "name": "secrety_ep",
            "connector": "secrety",
            "role": "target",
            "settings": {"api_key": sentinel},
        },
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "inline_secret_rejected"
    assert "api_key" in body["field"]

    listed = client.get(f"{API_PREFIX}/endpoints")
    assert listed.json() == []


# ========================================================================================
# DoD / C2: resolve status, without a value oracle
# ========================================================================================


def test_secret_resolve_response_shape_cannot_carry_a_value() -> None:
    """Reflection-style guard, mirroring ``tests/configstore/test_secrets.py``'s own
    field-set pin on ``SecretResolveStatus``: a future field added here that could carry
    a resolved value fails this test rather than silently opening a leak."""
    from qlabs_catalog_sync.api.routes.endpoints import SecretResolveOut

    assert set(SecretResolveOut.model_fields) == {"resolvable", "reason"}


def test_secret_resolve_reports_unresolvable_with_no_ref_bound(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    client.post(
        f"{API_PREFIX}/endpoints",
        json={"name": "no_secret_yet", "connector": "secrety", "role": "target", "settings": {}},
        headers={CSRF_HEADER: csrf},
    )

    response = client.get(f"{API_PREFIX}/endpoints/no_secret_yet/secret-resolve")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"resolvable", "reason"}
    assert body["resolvable"] is False


def test_secret_resolve_never_leaks_the_resolved_value(
    signed_in_client: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dishonest case: resolve-status must report resolvable/unresolvable and a
    value-free reason -- never the secret value itself, even on success."""
    client, csrf = signed_in_client
    sentinel = "sk-t12-3-resolve-status-must-never-leak-9f2ac1"
    monkeypatch.setenv("RESOLVE_SENTINEL__API_KEY", sentinel)

    client.post(
        f"{API_PREFIX}/endpoints",
        json={
            "name": "resolve_ep",
            "connector": "secrety",
            "role": "target",
            "settings": {},
            "secret_ref": "env:RESOLVE_SENTINEL",
        },
        headers={CSRF_HEADER: csrf},
    )

    response = client.get(f"{API_PREFIX}/endpoints/resolve_ep/secret-resolve")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"resolvable", "reason"}
    assert body["resolvable"] is True
    assert sentinel not in response.text
    assert sentinel not in json.dumps(body)


# ========================================================================================
# DoD / C2: the endpoint response never carries a secret
# ========================================================================================


def test_endpoint_response_shape_cannot_carry_a_secret_value() -> None:
    """Reflection-style guard, mirroring ``tests/configstore/test_credentials.py``:
    pins the exact field set on the endpoint response so a future field fails this test
    rather than silently opening a way for a secret to reach the wire."""
    from qlabs_catalog_sync.api.routes.endpoints import EndpointOut

    assert set(EndpointOut.model_fields) == {
        "name",
        "connector",
        "role",
        "settings",
        "secret_ref",
        "enabled",
        "created_at",
        "updated_at",
    }


def test_endpoint_response_never_contains_a_secret_value_even_when_one_resolves(
    signed_in_client: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, csrf = signed_in_client
    sentinel = "sk-t12-3-endpoint-response-must-never-leak-77ad0e"
    monkeypatch.setenv("LEAK_CHECK__API_KEY", sentinel)

    created = client.post(
        f"{API_PREFIX}/endpoints",
        json={
            "name": "leak_check",
            "connector": "secrety",
            "role": "target",
            "settings": {},
            "secret_ref": "env:LEAK_CHECK",
        },
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 201, created.text
    assert sentinel not in created.text

    fetched = client.get(f"{API_PREFIX}/endpoints/leak_check")
    assert sentinel not in fetched.text

    listed = client.get(f"{API_PREFIX}/endpoints")
    assert sentinel not in listed.text


# ========================================================================================
# DoD: an endpoint can be registered, healthchecked and enabled end to end against
# FakeConnector
# ========================================================================================


def test_endpoint_can_be_registered_healthchecked_and_enabled_end_to_end(
    signed_in_client: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in_client
    created = client.post(
        f"{API_PREFIX}/endpoints",
        json={"name": "fake_ep", "connector": "fake", "role": "source", "settings": {}},
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 201, created.text

    health = client.post(
        f"{API_PREFIX}/endpoints/fake_ep/healthcheck", headers={CSRF_HEADER: csrf}
    )
    assert health.status_code == 200, health.text
    body = health.json()
    assert body["endpoint"] == "fake_ep"
    assert body["state"] == "healthy"
    assert body["reason"] is None

    enabled = client.patch(
        f"{API_PREFIX}/endpoints/fake_ep", json={"enabled": True}, headers={CSRF_HEADER: csrf}
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True


# ========================================================================================
# DoD: a red healthcheck is a 200 describing an unhealthy endpoint, never a 500
# ========================================================================================


def test_an_auth_error_healthcheck_is_a_200_response_describing_an_unhealthy_endpoint(
    signed_in_client: tuple[TestClient, str], fake_connector: FakeConnector
) -> None:
    client, csrf = signed_in_client
    client.post(
        f"{API_PREFIX}/endpoints",
        json={"name": "flaky", "connector": "fake", "role": "source", "settings": {}},
        headers={CSRF_HEADER: csrf},
    )
    fake_connector.fail_next("healthcheck", AuthError("bad credentials", endpoint="fake"))

    response = client.post(
        f"{API_PREFIX}/endpoints/flaky/healthcheck", headers={CSRF_HEADER: csrf}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "unhealthy"
    assert body["reason"] == "bad credentials"


def test_a_healthcheck_timeout_is_a_200_response_describing_an_unhealthy_endpoint(
    signed_in_client: tuple[TestClient, str], fake_connector: FakeConnector
) -> None:
    client, csrf = signed_in_client
    client.post(
        f"{API_PREFIX}/endpoints",
        json={"name": "slow", "connector": "fake", "role": "source", "settings": {}},
        headers={CSRF_HEADER: csrf},
    )
    fake_connector.fail_next("healthcheck", TimeoutError("simulated timeout"))

    response = client.post(
        f"{API_PREFIX}/endpoints/slow/healthcheck", headers={CSRF_HEADER: csrf}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "unhealthy"
    assert "did not respond" in body["reason"]


def test_an_unexpected_healthcheck_exception_is_unhealthy_and_never_leaks_its_message(
    signed_in_client: tuple[TestClient, str], fake_connector: FakeConnector
) -> None:
    """The dishonest case: an unanticipated connector bug must still be a 200 with a
    generic, value-free reason -- the real exception text must never reach the wire."""
    client, csrf = signed_in_client
    client.post(
        f"{API_PREFIX}/endpoints",
        json={"name": "buggy", "connector": "fake", "role": "source", "settings": {}},
        headers={CSRF_HEADER: csrf},
    )
    sentinel = "sk-t12-3-unexpected-exception-must-not-leak-4b1c"
    fake_connector.fail_next("healthcheck", RuntimeError(f"boom {sentinel}"))

    response = client.post(
        f"{API_PREFIX}/endpoints/buggy/healthcheck", headers={CSRF_HEADER: csrf}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "unhealthy"
    assert sentinel not in response.text
    assert "RuntimeError" in body["reason"]


# ========================================================================================
# DoD: deleting an endpoint a pair uses is refused, naming the pairs
# ========================================================================================


async def test_deleting_an_endpoint_a_pair_uses_is_refused_naming_the_pairs(
    signed_in_client: tuple[TestClient, str], config_service: ConfigService
) -> None:
    client, csrf = signed_in_client
    client.post(
        f"{API_PREFIX}/endpoints",
        json={"name": "src", "connector": "fake", "role": "source", "settings": {}},
        headers={CSRF_HEADER: csrf},
    )
    client.post(
        f"{API_PREFIX}/endpoints",
        json={"name": "tgt", "connector": "qlik", "role": "target", "settings": {}},
        headers={CSRF_HEADER: csrf},
    )
    await config_service.create_sync_pair(
        name="src-to-tgt",
        source="src",
        target="tgt",
        target_space="Sales Space",
        actor=USERNAME,
        now=datetime.now(UTC),
    )

    response = client.delete(f"{API_PREFIX}/endpoints/src", headers={CSRF_HEADER: csrf})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "endpoint_in_use"
    assert "src-to-tgt" in body["message"]

    # The endpoint is still there -- the refusal did not half-apply.
    assert client.get(f"{API_PREFIX}/endpoints/src").status_code == 200


# ========================================================================================
# DoD: every route requires a session, and a mutating route requires a CSRF token
# (auth is middleware, so this should already hold -- the test that matters is the one
# that would fail if a route here somehow escaped it)
# ========================================================================================


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", f"{API_PREFIX}/connectors"),
        ("GET", f"{API_PREFIX}/endpoints"),
        ("POST", f"{API_PREFIX}/endpoints"),
        ("GET", f"{API_PREFIX}/endpoints/whatever"),
        ("PATCH", f"{API_PREFIX}/endpoints/whatever"),
        ("DELETE", f"{API_PREFIX}/endpoints/whatever"),
        ("GET", f"{API_PREFIX}/endpoints/whatever/secret-resolve"),
        ("POST", f"{API_PREFIX}/endpoints/whatever/healthcheck"),
    ],
)
def test_every_route_requires_a_session(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path)
    assert response.status_code == 401, f"{method} {path}: {response.status_code} {response.text}"
    assert response.json()["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", f"{API_PREFIX}/endpoints"),
        ("PATCH", f"{API_PREFIX}/endpoints/whatever"),
        ("DELETE", f"{API_PREFIX}/endpoints/whatever"),
        ("POST", f"{API_PREFIX}/endpoints/whatever/healthcheck"),
    ],
)
def test_a_mutating_route_without_a_csrf_token_is_refused(
    client: TestClient, method: str, path: str
) -> None:
    _sign_in(client)
    response = client.request(method, path, json={})
    assert response.status_code == 403, f"{method} {path}: {response.status_code} {response.text}"
    assert response.json()["code"] == "csrf_token_invalid"


# ========================================================================================
# T12.8: every response model this task adds must be declared in the OpenAPI schema
# ========================================================================================


def test_response_models_are_declared_in_the_openapi_schema(app: FastAPI) -> None:
    schema = app.openapi()
    component_names = set(schema.get("components", {}).get("schemas", {}))
    expected = {
        "EndpointOut",
        "EndpointCreateRequest",
        "EndpointUpdateRequest",
        "SecretResolveOut",
        "EndpointHealthOut",
        "ConnectorInfo",
        "CapabilityManifestOut",
        "EntityCapabilityOut",
        "FieldCapabilityOut",
    }
    assert expected <= component_names, component_names
