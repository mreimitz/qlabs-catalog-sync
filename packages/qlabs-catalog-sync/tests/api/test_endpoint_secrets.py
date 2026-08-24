"""Credentials entered in the console (amended C2), end to end over HTTP.

C2 as originally written said the console never accepts or persists a secret value: an
endpoint held ``env:QLIK_ACME`` and the value lived in the process environment. That
requires an operator to edit a file on the host and restart the service **for every client
they add**, which is not a workflow -- it is the opposite of the console-first premise C1
states. Endpoint configuration belongs in the configuration database, and a credential is
endpoint configuration.

What replaces "never persists" is narrower and has to be tested as such:

* **The credential goes in and never comes back out.** No route returns one; the endpoint
  representation has no field for one; the audit log records that a field was set, not
  what it was set to.
* **The endpoint works afterwards, with no restart.** ``PUT`` the secret, and the very
  next healthcheck on the same running service resolves it. That is the entire point of
  the change, so it is tested as the round trip an operator actually performs.
* **What lands in the database is ciphertext** -- see
  ``tests/configstore/test_credentials.py``, which reads the bytes back through plain SQL.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pydantic import SecretStr
from sqlalchemy import text

from qlabs_catalog_sync.api.app import API_PREFIX, create_app
from qlabs_catalog_sync.api.auth import CSRF_HEADER, AdminCredential, ConsoleAuth, hash_password
from qlabs_catalog_sync.configstore.crypto import SecretCipher, generate_master_key
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.discovery import ConnectorRegistry
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
from qlabs_catalog_sync_sdk.models import EntityType, NeutralEntity
from qlabs_catalog_sync_sdk.testing.manifests import databricks_shaped_manifest

_USERNAME = "admin"
_PASSWORD = "secrets-suite-password"
_SESSION_PATH = f"{API_PREFIX}/auth/session"

#: The credential under test. Distinctive enough that a substring check for it in a
#: response body, an audit row or a database file is meaningful.
_CREDENTIAL = "dapi-console-entered-credential-9f2b"


class _TenantConfig(ConnectorConfig):
    """One plain field and one required secret -- the shape every real connector in this
    system has."""

    host: str
    client_secret: SecretStr


class _TenantConnector(Connector):
    """Reports healthy only when the credential it was configured with is the one that was
    entered, so "the healthcheck passed" cannot be true for a connector that resolved
    nothing."""

    name = "tenant"
    ConfigModel = _TenantConfig

    def __init__(self) -> None:
        self._ctx: ConnectorContext[_TenantConfig] | None = None

    def capabilities(self) -> CapabilityManifestBase:
        return databricks_shaped_manifest(has_sql_warehouse=False)

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        self._ctx = ctx

    async def healthcheck(self) -> HealthStatus:
        assert self._ctx is not None
        secret = self._ctx.config.client_secret.get_secret_value()
        if secret != _CREDENTIAL:
            return HealthStatus.unhealthy("tenant", "the connector received the wrong credential")
        return HealthStatus.healthy("tenant")

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        raise NotImplementedError

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        raise NotImplementedError


@pytest.fixture
def registry() -> ConnectorRegistry:
    return ConnectorRegistry({"tenant": _TenantConnector}, {})


@pytest.fixture
def cipher() -> SecretCipher:
    return SecretCipher.from_key(base64.urlsafe_b64decode(generate_master_key()))


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'config.db'}"
    upgrade_to_head(url)
    return url


@pytest.fixture
def config_service(
    db_url: str, registry: ConnectorRegistry, cipher: SecretCipher
) -> Iterator[ConfigService]:
    yield ConfigService.from_url(db_url, registry, cipher=cipher)


@pytest.fixture
def app(config_service: ConfigService, registry: ConnectorRegistry) -> FastAPI:
    credential = AdminCredential.from_password_hash(hash_password(_PASSWORD), username=_USERNAME)
    return create_app(
        health=HealthRegistry(),
        metrics_registry=CollectorRegistry(),
        auth=ConsoleAuth(credential=credential),
        config_service=config_service,
        registry=registry,
    )


@pytest.fixture
def signed_in(app: FastAPI) -> tuple[TestClient, str]:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(_SESSION_PATH, json={"username": _USERNAME, "password": _PASSWORD})
    assert response.status_code == 200, response.text
    return client, response.json()["csrf_token"]


def _register(client: TestClient, csrf: str, name: str = "acme") -> None:
    created = client.post(
        f"{API_PREFIX}/endpoints",
        json={
            "name": name,
            "connector": "tenant",
            "role": "source",
            "settings": {"host": "https://acme.example"},
            "secret_ref": f"db:{name}",
        },
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 201, created.text


def _put_secret(client: TestClient, csrf: str, *, name: str = "acme", value: str) -> Any:
    return client.put(
        f"{API_PREFIX}/endpoints/{name}/secrets/client_secret",
        json={"value": value},
        headers={CSRF_HEADER: csrf},
    )


def test_entering_a_credential_makes_the_endpoint_work_without_a_restart(
    signed_in: tuple[TestClient, str],
) -> None:
    """The whole point of the amendment, as the round trip an operator performs.

    Register the endpoint, save the credential, run the healthcheck -- all against one
    running service, with nothing in the process environment and no restart in between.
    Under the original C2 the middle step did not exist and the last one could not pass.
    """
    client, csrf = signed_in
    _register(client, csrf)

    before = client.post(f"{API_PREFIX}/endpoints/acme/healthcheck", headers={CSRF_HEADER: csrf})
    assert before.status_code == 200, before.text
    assert before.json()["state"] != "healthy", "healthy before any credential was entered"

    saved = _put_secret(client, csrf, value=_CREDENTIAL)
    assert saved.status_code == 204, saved.text

    after = client.post(f"{API_PREFIX}/endpoints/acme/healthcheck", headers={CSRF_HEADER: csrf})

    assert after.status_code == 200, after.text
    assert after.json()["state"] == "healthy", after.text


def test_a_stored_credential_is_never_returned_by_any_route(
    signed_in: tuple[TestClient, str],
) -> None:
    """Write-only, checked across every route that says anything about this endpoint --
    not just the one that stored it."""
    client, csrf = signed_in
    _register(client, csrf)
    assert _put_secret(client, csrf, value=_CREDENTIAL).status_code == 204

    responses = [
        client.get(f"{API_PREFIX}/endpoints"),
        client.get(f"{API_PREFIX}/endpoints/acme"),
        client.get(f"{API_PREFIX}/endpoints/acme/secrets"),
        client.get(f"{API_PREFIX}/endpoints/acme/secret-resolve"),
        client.get(f"{API_PREFIX}/endpoints/acme/manifest"),
        client.post(f"{API_PREFIX}/endpoints/acme/healthcheck", headers={CSRF_HEADER: csrf}),
    ]

    for response in responses:
        assert _CREDENTIAL not in response.text, (
            f"{response.request.method} {response.request.url.path} echoed the credential"
        )


def test_the_secret_listing_says_which_fields_are_set_and_nothing_more(
    signed_in: tuple[TestClient, str],
) -> None:
    """A field with nothing stored is listed too -- "this connector wants a client secret
    and none has been entered" is exactly what the console has to render."""
    client, csrf = signed_in
    _register(client, csrf)

    empty = client.get(f"{API_PREFIX}/endpoints/acme/secrets").json()
    assert empty == [
        {"field": "client_secret", "is_set": False, "updated_at": None, "key_id": None}
    ]

    assert _put_secret(client, csrf, value=_CREDENTIAL).status_code == 204
    filled = client.get(f"{API_PREFIX}/endpoints/acme/secrets").json()

    assert [row["field"] for row in filled] == ["client_secret"]
    assert filled[0]["is_set"] is True
    assert filled[0]["updated_at"] is not None, "the console cannot say 'saved just now' without it"


def test_replacing_a_credential_takes_effect_immediately(
    signed_in: tuple[TestClient, str],
) -> None:
    """A typo'd credential must be fixable by typing the right one, with no extra step --
    the pooled-connector cache is what would silently keep serving the old value."""
    client, csrf = signed_in
    _register(client, csrf)
    assert _put_secret(client, csrf, value="the-wrong-credential").status_code == 204
    assert (
        client.post(f"{API_PREFIX}/endpoints/acme/healthcheck", headers={CSRF_HEADER: csrf}).json()[
            "state"
        ]
        != "healthy"
    )

    assert _put_secret(client, csrf, value=_CREDENTIAL).status_code == 204

    after = client.post(f"{API_PREFIX}/endpoints/acme/healthcheck", headers={CSRF_HEADER: csrf})
    assert after.json()["state"] == "healthy", after.text


def test_clearing_a_credential_removes_it(signed_in: tuple[TestClient, str]) -> None:
    client, csrf = signed_in
    _register(client, csrf)
    assert _put_secret(client, csrf, value=_CREDENTIAL).status_code == 204

    cleared = client.delete(
        f"{API_PREFIX}/endpoints/acme/secrets/client_secret", headers={CSRF_HEADER: csrf}
    )

    assert cleared.status_code == 204, cleared.text
    assert client.get(f"{API_PREFIX}/endpoints/acme/secrets").json()[0]["is_set"] is False
    resolve = client.get(f"{API_PREFIX}/endpoints/acme/secret-resolve").json()
    assert resolve["resolvable"] is False


def test_clearing_a_credential_that_was_never_stored_is_a_404(
    signed_in: tuple[TestClient, str],
) -> None:
    """ "I removed that credential" and "there was never one there" are different answers
    to the operator's question, and only one of them means the endpoint just changed."""
    client, csrf = signed_in
    _register(client, csrf)

    response = client.delete(
        f"{API_PREFIX}/endpoints/acme/secrets/client_secret", headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "secret_not_stored"


def test_a_field_the_connector_does_not_declare_is_refused(
    signed_in: tuple[TestClient, str],
) -> None:
    """Storing a credential nothing will ever read is worse than refusing it: the endpoint
    looks configured and fails at the tenant."""
    client, csrf = signed_in
    _register(client, csrf)

    response = client.put(
        f"{API_PREFIX}/endpoints/acme/secrets/host",
        json={"value": "not-a-secret-field"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "unknown_secret_field"
    assert "client_secret" in response.json()["message"], "it should name the fields that exist"


def test_an_empty_credential_is_refused_rather_than_stored(
    signed_in: tuple[TestClient, str],
) -> None:
    """An empty credential fails at the tenant with a confusing authentication error, and
    "I meant to remove it" has its own explicit, separately audited operation."""
    client, csrf = signed_in
    _register(client, csrf)

    response = _put_secret(client, csrf, value="")

    assert response.status_code == 422, response.text


def test_storing_a_credential_requires_a_session(app: FastAPI) -> None:
    """The one route in this API that accepts a credential must be the last one to be
    reachable without signing in."""
    client = TestClient(app, raise_server_exceptions=False)

    response = client.put(
        f"{API_PREFIX}/endpoints/acme/secrets/client_secret", json={"value": _CREDENTIAL}
    )

    assert response.status_code == 401, response.text


def test_storing_a_credential_requires_the_csrf_header(
    signed_in: tuple[TestClient, str],
) -> None:
    """A session cookie alone must not be enough: without CSRF protection, any page the
    operator visits could write a credential into their console."""
    client, csrf = signed_in
    _register(client, csrf)

    response = client.put(
        f"{API_PREFIX}/endpoints/acme/secrets/client_secret", json={"value": _CREDENTIAL}
    )

    assert response.status_code == 403, response.text


def test_the_audit_log_records_the_change_but_never_the_value(
    signed_in: tuple[TestClient, str], config_service: ConfigService
) -> None:
    """A credential change is a real configuration change and must be attributable like
    any other -- but the audit log is dumped, exported and read by people, so the value
    must not be able to reach it even by accident."""
    client, csrf = signed_in
    _register(client, csrf)
    assert _put_secret(client, csrf, value=_CREDENTIAL).status_code == 204

    with config_service.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT field, old_value, new_value, actor FROM config_changes "
                "WHERE entity_id = 'acme'"
            )
        ).all()

    dumped = repr(rows)
    assert _CREDENTIAL not in dumped, "the audit log carries the credential"
    secret_rows = [row for row in rows if row.field and "client_secret" in row.field]
    assert secret_rows, "storing a credential wrote no audit row at all"
    assert secret_rows[0].actor == _USERNAME
