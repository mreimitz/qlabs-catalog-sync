"""``GET /endpoints/{name}/manifest`` -- what a *configured* endpoint supports (C6).

``GET /connectors`` lists connector *classes*, which have no configuration, and a
connector whose manifest genuinely depends on its resolved config is entitled to refuse
there (``routes/connectors.py``'s module docstring; the Databricks connector does exactly
that, because D6 makes ``tags`` readable only when a SQL warehouse is configured). That
left the console with no way at all to show the MVP's own source connector's
capabilities. This route is the other half of C6's *"reading its capability manifest"*:
an endpoint that has been configured can always be asked, because ``setup()`` has run.

Two properties this suite exists to hold:

* **Always a 200, exactly like a red healthcheck is** (see ``routes/endpoints.py``'s
  module docstring on why that is the right shape). An unreachable tenant is a fact about
  the endpoint, not an error in the request that asked for it. A 500 here would send the
  console an untyped failure for an entirely ordinary situation.
* **A failure reason never carries credential material.** This route resolves the same
  secret reference the healthcheck does, so it reproduces the healthcheck's failure
  taxonomy exactly rather than simplifying it: only errors documented safe to surface are
  echoed, and anything else becomes a generic, value-free reason plus a correlation id.
  ``test_an_unexpected_failure_reason_never_echoes_credential_material`` is what fails if
  that is ever loosened.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pydantic import SecretStr, field_validator, model_validator

from qlabs_catalog_sync.api.app import API_PREFIX, create_app
from qlabs_catalog_sync.api.auth import CSRF_HEADER, AdminCredential, ConsoleAuth, hash_password
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
from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_catalog_sync_sdk.models import EntityType, NeutralEntity
from qlabs_catalog_sync_sdk.testing import FakeConnector
from qlabs_catalog_sync_sdk.testing.manifests import databricks_shaped_manifest

_USERNAME = "admin"
_PASSWORD = "manifest-suite-password"
_SESSION_PATH = f"{API_PREFIX}/auth/session"

#: A value that must never appear in any response body. Threaded through a failure that
#: this route deliberately does NOT trust to be safe to echo.
_CREDENTIAL_MATERIAL = "s3cr3t-token-must-never-be-echoed"


class _WarehouseConfig(ConnectorConfig):
    """The one setting that changes what this connector supports -- D6's SQL warehouse,
    modelled minimally."""

    sql_warehouse_id: str | None = None


class _ConfigDependentConnector(Connector):
    """A connector that models the real Databricks connector's contract exactly: its
    manifest depends on resolved configuration, so ``capabilities()`` **refuses before**
    ``setup()`` rather than inventing an answer (RS-08's lifecycle: discover, configure,
    ``setup``, then ``capabilities``).

    ``FakeConnector`` cannot stand in here. ``_wrap_as_class`` hands the route back the
    same instance the test already holds, which makes "the connector class" and "this
    configured endpoint's connector" indistinguishable -- so a route that asked the class
    would pass anyway. That is the same substitution that hid the ``GET /connectors`` 500
    for the whole of WP12, and this stub exists so it cannot hide this route's equivalent.
    """

    name = "config-dependent"
    ConfigModel = _WarehouseConfig

    def __init__(self) -> None:
        self._ctx: ConnectorContext[_WarehouseConfig] | None = None

    def capabilities(self) -> CapabilityManifestBase:
        if self._ctx is None:
            raise RuntimeError("capabilities() needs the resolved config: call setup(ctx) first")
        return databricks_shaped_manifest(
            has_sql_warehouse=self._ctx.config.sql_warehouse_id is not None
        )

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        self._ctx = ctx

    async def healthcheck(self) -> HealthStatus:
        raise NotImplementedError

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        raise NotImplementedError

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        raise NotImplementedError


class _CredentialRouteConfig(ConnectorConfig):
    """Two optional secret fields with a cross-field rule requiring exactly one -- the
    real Databricks connector's shape (OAuth service principal *or* personal access
    token), modelled minimally. Both are optional *by type* precisely because only one
    route is ever set, so neither is ``is_required()`` and neither gets a placeholder
    during settings validation."""

    host: str
    token: SecretStr | None = None
    api_key: SecretStr | None = None

    @model_validator(mode="after")
    def _exactly_one_route(self) -> _CredentialRouteConfig:
        configured = [name for name in ("token", "api_key") if getattr(self, name) is not None]
        if not configured:
            raise ValueError("configure exactly one credential route: 'token' or 'api_key'")
        if len(configured) > 1:
            raise ValueError("configure only one credential route, not both")
        return self


class _LeakyFieldConfig(ConnectorConfig):
    """A connector whose *field* validator on a secret-typed field echoes the value it
    rejected. Connector-authored and entirely possible; the route must never pass that
    message on."""

    token: SecretStr | None = None

    @field_validator("token")
    @classmethod
    def _reject(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            raise ValueError(f"token {value.get_secret_value()!r} is not acceptable")
        return value


class _UnconfigurableConnector(Connector):
    """Never reaches ``setup()``: building its ``ConnectorConfig`` is what fails."""

    name = "unconfigurable"
    ConfigModel = _CredentialRouteConfig

    def capabilities(self) -> CapabilityManifestBase:
        raise AssertionError("capabilities() must not be reached: the config never built")

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        raise AssertionError("setup() must not be reached: the config never built")

    async def healthcheck(self) -> HealthStatus:
        raise NotImplementedError

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        raise NotImplementedError

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        raise NotImplementedError


class _LeakyConnector(_UnconfigurableConnector):
    name = "leaky"
    ConfigModel = _LeakyFieldConfig


def _wrap_as_class(instance: Connector) -> type[Connector]:
    """Wrap a built connector instance as a zero-argument-constructible class.

    Same mechanism and same reason as ``tests/api/test_endpoints.py``'s file-local copy:
    the route builds its connector the production way (``registry.get_connector(name)()``,
    zero arguments), and this is what lets a test still hold the exact instance that call
    produces, so it can seed ``fail_next`` on it before driving the route over HTTP.
    """
    base = type(instance)

    class _Wrapped(base):  # type: ignore[valid-type, misc]
        def __new__(cls) -> Connector:  # noqa: D102
            return instance

        def __init__(self) -> None:  # noqa: D107
            pass

    return _Wrapped


@pytest.fixture
def source_connector() -> FakeConnector:
    """Databricks-shaped: a read-only source whose canned manifest is the same shape the
    real source connector reports once a SQL warehouse is configured (D6)."""
    return FakeConnector.read_only_source(name="fake")


@pytest.fixture
def registry(source_connector: FakeConnector) -> ConnectorRegistry:
    return ConnectorRegistry(
        {
            "fake": _wrap_as_class(source_connector),
            "unconfigurable": _UnconfigurableConnector,
            "leaky": _LeakyConnector,
        },
        {},
    )


@pytest.fixture
def config_service(tmp_path: Path, registry: ConnectorRegistry) -> Iterator[ConfigService]:
    url = f"sqlite:///{tmp_path / 'config.db'}"
    upgrade_to_head(url)
    yield ConfigService.from_url(url, registry)


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


def _register(client: TestClient, csrf: str, name: str = "src") -> None:
    created = client.post(
        f"{API_PREFIX}/endpoints",
        json={"name": name, "connector": "fake", "role": "source", "settings": {}},
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 201, created.text


def test_a_configured_endpoint_reports_its_capability_manifest(
    signed_in: tuple[TestClient, str],
) -> None:
    """The whole reason this route exists: a manifest an operator can read, for an
    endpoint that has been configured -- which ``GET /connectors`` cannot give them for a
    connector whose capabilities depend on that configuration."""
    client, csrf = signed_in
    _register(client, csrf)

    response = client.get(f"{API_PREFIX}/endpoints/src/manifest")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["endpoint"] == "src"
    assert body["unavailable_reason"] is None
    assert body["manifest"] is not None
    assert body["manifest"]["entities"], "the manifest reported no entity types at all"


def test_the_route_is_actually_mounted_on_the_real_app(signed_in: tuple[TestClient, str]) -> None:
    """``create_app`` mounts the endpoints router only when given both a ConfigService and
    a ConnectorRegistry, and defaults both to ``None`` -- the same escape hatch that once
    left authentication uninstalled. Assert from outside that the path resolves at all,
    rather than trusting the factory was called correctly."""
    client, csrf = signed_in
    _register(client, csrf)

    response = client.get(f"{API_PREFIX}/endpoints/src/manifest")

    assert response.status_code != 404, "the manifest route is not mounted"


def test_a_connector_that_cannot_be_set_up_is_a_200_describing_why(
    signed_in: tuple[TestClient, str], source_connector: FakeConnector
) -> None:
    """An unreachable or unauthenticated tenant is an ordinary fact about the endpoint,
    reported in the body -- never a 500, and never an untyped failure for the console."""
    client, csrf = signed_in
    _register(client, csrf)
    source_connector.fail_next("setup", AuthError("bad credentials", endpoint="fake"))

    response = client.get(f"{API_PREFIX}/endpoints/src/manifest")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["manifest"] is None
    assert body["unavailable_reason"] == "bad credentials"


def test_an_unexpected_failure_reason_never_echoes_credential_material(
    signed_in: tuple[TestClient, str], source_connector: FakeConnector
) -> None:
    """An exception this route does NOT recognize gets a generic, value-free reason.

    ``ConnectorError.message`` is documented safe to surface; an arbitrary exception is
    not -- a pydantic ``ValidationError`` over a ``SecretStr`` field can echo its raw
    input. So anything unrecognized becomes a correlation id and a type name, and the
    message goes only to the structured log. This test fails if that is ever loosened to
    ``str(exc)``.
    """
    client, csrf = signed_in
    _register(client, csrf)
    source_connector.fail_next(
        "setup", RuntimeError(f"connection string was postgres://user:{_CREDENTIAL_MATERIAL}@h/db")
    )

    response = client.get(f"{API_PREFIX}/endpoints/src/manifest")

    assert response.status_code == 200, response.text
    assert _CREDENTIAL_MATERIAL not in response.text, (
        "the failure reason echoed credential material back to the client"
    )
    reason = response.json()["unavailable_reason"]
    assert "RuntimeError" in reason
    assert "correlation id" in reason


def test_an_unknown_endpoint_is_an_error_not_an_empty_manifest(
    signed_in: tuple[TestClient, str],
) -> None:
    """ "This endpoint does not exist" and "this endpoint reports nothing" are opposite
    facts. Asking about a name that was never registered is a request error, not a 200
    with an empty body the console would render as a real, empty manifest."""
    client, _ = signed_in

    response = client.get(f"{API_PREFIX}/endpoints/never-registered/manifest")

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "endpoint_not_found"


def test_reading_a_manifest_requires_a_session(app: FastAPI) -> None:
    """C7: everything under the API prefix is behind the administrator session."""
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"{API_PREFIX}/endpoints/src/manifest")

    assert response.status_code == 401, response.text


def test_the_manifest_reflects_this_endpoint_s_configuration_not_the_connector_class(
    tmp_path: Path,
) -> None:
    """The property that makes this route necessary rather than redundant, tested against
    a connector that actually behaves like the real one.

    ``GET /connectors`` can only ever describe a connector *class*. D6 makes the
    Databricks connector's ``tags`` readable only when a SQL warehouse is configured, so
    two endpoints on the same connector, configured differently, genuinely support
    different things -- and an unconfigured class supports nothing it can name at all.

    Two failures this kills, both of which a ``FakeConnector``-based version let through:
    a route that asks the *class* (which raises, so the manifest silently degrades to
    ``unavailable_reason`` for every endpoint), and a route that returns one cached
    manifest for every endpoint on a connector regardless of how each is configured --
    which would show an operator capabilities their endpoint does not have.
    """
    bodies = []
    for warehouse in ("wh-123", None):
        registry = ConnectorRegistry({"config-dependent": _ConfigDependentConnector}, {})
        url = f"sqlite:///{tmp_path / f'{warehouse}.db'}"
        upgrade_to_head(url)
        credential = AdminCredential.from_password_hash(
            hash_password(_PASSWORD), username=_USERNAME
        )
        app = create_app(
            health=HealthRegistry(),
            metrics_registry=CollectorRegistry(),
            auth=ConsoleAuth(credential=credential),
            config_service=ConfigService.from_url(url, registry),
            registry=registry,
        )
        client = TestClient(app, raise_server_exceptions=False)
        token = client.post(
            _SESSION_PATH, json={"username": _USERNAME, "password": _PASSWORD}
        ).json()["csrf_token"]
        created = client.post(
            f"{API_PREFIX}/endpoints",
            json={
                "name": "src",
                "connector": "config-dependent",
                "role": "source",
                "settings": {"sql_warehouse_id": warehouse},
            },
            headers={CSRF_HEADER: token},
        )
        assert created.status_code == 201, created.text

        response = client.get(f"{API_PREFIX}/endpoints/src/manifest")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["unavailable_reason"] is None, (
            "the route could not read a manifest from a perfectly well-configured "
            f"endpoint -- it is asking the connector class, not this endpoint: {body}"
        )
        assert body["manifest"] is not None
        bodies.append(body["manifest"])

    assert bodies[0] != bodies[1], (
        "both endpoints reported the same manifest despite being configured differently "
        "-- this route is describing the connector class, not the endpoint"
    )


def test_an_endpoint_with_no_credential_is_told_which_credential_is_missing(
    signed_in: tuple[TestClient, str],
) -> None:
    """The commonest state a half-configured endpoint is in, and the one the console has
    to be able to explain: settings are filled in, no credential is bound yet.

    Building the ``ConnectorConfig`` raises a pydantic ``ValidationError``, which used to
    land in the generic ``except Exception`` tier and come back as "failed unexpectedly
    (ValidationError); see server logs (correlation id ...)". That is the right answer for
    an unknown failure and a useless one here: it sends an operator to a log file to be
    told they never set a credential. The connector's own cross-field message says exactly
    what to do, carries no value, and is what this route now returns.
    """
    client, csrf = signed_in
    created = client.post(
        f"{API_PREFIX}/endpoints",
        json={
            "name": "half-configured",
            "connector": "unconfigurable",
            "role": "source",
            "settings": {"host": "https://tenant.example"},
        },
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 201, created.text

    response = client.get(f"{API_PREFIX}/endpoints/half-configured/manifest")

    assert response.status_code == 200, response.text
    reason = response.json()["unavailable_reason"]
    assert response.json()["manifest"] is None
    assert "configure exactly one credential route" in reason
    assert "correlation id" not in reason, (
        "a missing credential is a known, explainable state -- not an unexpected failure"
    )


def test_a_rejected_secret_field_never_echoes_the_value_it_rejected(
    signed_in: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Surfacing validation detail must not become a way for a credential to escape.

    A connector's *field* validator on a secret-typed field is free to write
    ``f"token {value!r} is not acceptable"``. The route reports which field failed and
    withholds that message, because the field is one of the connector's own secret-typed
    fields -- the same single definition the configuration service refuses inline secrets
    by. Model-level messages are trusted (see the test above); field-level ones on a
    secret are not, and this test is what fails if that distinction is ever dropped.
    """
    client, csrf = signed_in
    monkeypatch.setenv("LEAKY__TOKEN", _CREDENTIAL_MATERIAL)
    created = client.post(
        f"{API_PREFIX}/endpoints",
        json={
            "name": "leaky-endpoint",
            "connector": "leaky",
            "role": "source",
            "settings": {},
            "secret_ref": "env:LEAKY",
        },
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 201, created.text

    response = client.get(f"{API_PREFIX}/endpoints/leaky-endpoint/manifest")

    assert response.status_code == 200, response.text
    assert _CREDENTIAL_MATERIAL not in response.text, (
        "a secret-typed field's own validator message was echoed back to the client"
    )
    reason = response.json()["unavailable_reason"]
    assert "token" in reason and "withheld" in reason
