"""Shared HTTP-level plumbing for the T12.4 route tests (``test_pairs.py``,
``test_selection_routes.py``): a never-instantiated stub connector pair to build a
minimal :class:`~qlabs_catalog_sync.discovery.ConnectorRegistry` from, plus tiny
``POST``-and-assert wrappers for the two things almost every test in both files needs
first -- a registered endpoint, and a sync pair built from two of them.

Named ``sync_pair_helpers`` rather than ``helpers`` -- this repo collects tests with
pytest's ``importlib`` import mode over one flat module namespace, and that basename has
already collided twice (see ``tests/api/api_helpers.py``'s own docstring). Deliberately
holds no ``@pytest.fixture`` of its own: every other ``*_helpers.py`` module in this
package (``tests/api/api_helpers.py``, ``tests/configstore/config_service_helpers.py``)
is plain builder functions and classes, with each test module wiring its own local
fixtures around them (``tests/api/test_endpoints.py`` is the closest relative -- same
``db_url``/``registry``/``config_service``/``app``/``client``/``signed_in_client``
fixture chain, kept file-local in each of ``test_pairs.py`` and
``test_selection_routes.py`` rather than centralized here, to match that convention
exactly instead of inventing a new one for this task alone).
"""

from __future__ import annotations

from typing import Any, Final

from fastapi.testclient import TestClient

from qlabs_catalog_sync.api.app import API_PREFIX
from qlabs_catalog_sync.api.auth import AUTH_SESSION_ROUTE, CSRF_HEADER, ScryptParams, hash_password
from qlabs_catalog_sync.discovery import ConnectorRegistry
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

__all__ = [
    "CSRF_HEADER",
    "PASSWORD",
    "PASSWORD_HASH",
    "SESSION_PATH",
    "USERNAME",
    "build_registry",
    "create_endpoint",
    "create_pair",
    "sign_in",
]

USERNAME: Final[str] = "console-operator"
PASSWORD: Final[str] = "SENTINEL-t12-4-pair-selection-admin-password"
#: Cheap on purpose (mirrors ``tests/api/test_endpoints.py``'s own ``_TEST_SCRYPT_PARAMS``)
#: -- this suite does not test auth itself, only that it applies.
_TEST_SCRYPT_PARAMS: Final = ScryptParams(log_n=14, r=8, p=1)
PASSWORD_HASH: Final[str] = hash_password(PASSWORD, params=_TEST_SCRYPT_PARAMS)
SESSION_PATH: Final[str] = f"{API_PREFIX}{AUTH_SESSION_ROUTE}"


# --------------------------------------------------------------------------------------
# A stub connector class, never instantiated by anything these routes exercise
# --------------------------------------------------------------------------------------


class _StubConfig(ConnectorConfig):
    """Empty config -- this class is never instantiated (see below); ``ConfigService``
    only ever reads ``.ConfigModel`` off the class when validating endpoint settings."""


class _StubConnector(Connector):
    """Every abstract lifecycle/read method stubbed with ``NotImplementedError``.

    Mirrors ``tests/configstore/config_service_helpers.py``'s own ``_StubConnector`` for
    the identical reason: neither ``ConfigService`` (T10.3) nor ``routes/pairs.py`` /
    ``routes/selection.py`` (T12.4) ever call ``registry.get_connector(name)()`` --
    ``ConnectorRegistry.get_connector`` (``discovery.py``) is a plain dict lookup keyed
    by the *name* an endpoint's ``connector`` field carries, never by connector class
    identity, so one class registered under two different names (see
    :func:`build_registry`) is a legitimate registry, not a shortcut that happens to work.
    """

    ConfigModel = _StubConfig

    def capabilities(self) -> CapabilityManifestBase:
        raise NotImplementedError

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        raise NotImplementedError

    async def healthcheck(self) -> HealthStatus:
        raise NotImplementedError

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        raise NotImplementedError

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        raise NotImplementedError


def build_registry() -> ConnectorRegistry:
    """A registry with two names: ``"source"`` (any non-Qlik connector name, usable as a
    pair's source) and ``"qlik"`` -- the one name the v1 direction guardrail accepts as a
    target (``qlabs_catalog_sync.config.WRITE_CONNECTOR_NAME``). Both point at the same
    never-instantiated stub class; see :class:`_StubConnector`'s docstring for why that
    is not a shortcut."""
    return ConnectorRegistry({"source": _StubConnector, "qlik": _StubConnector}, {})


# --------------------------------------------------------------------------------------
# Small HTTP-level builders
# --------------------------------------------------------------------------------------


def sign_in(client: TestClient) -> str:
    """Sign in as the one configured administrator; return the CSRF token every
    mutating call in these suites needs."""
    response = client.post(SESSION_PATH, json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()["csrf_token"]
    assert isinstance(token, str) and token
    return token


def create_endpoint(
    client: TestClient,
    csrf: str,
    *,
    name: str,
    connector: str,
    role: str = "source",
    settings: dict[str, object] | None = None,
    secret_ref: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """``POST /endpoints``, assert it succeeded, return the created endpoint's JSON body.

    ``enabled`` defaults to ``True`` here (unlike the route's own default of ``False``)
    because most tests in these two suites need an immediately-usable endpoint, not C6's
    multi-step registration flow; a test that specifically needs a *disabled* endpoint
    passes ``enabled=False`` explicitly.
    """
    response = client.post(
        f"{API_PREFIX}/endpoints",
        json={
            "name": name,
            "connector": connector,
            "role": role,
            "settings": settings or {},
            "secret_ref": secret_ref,
            "enabled": enabled,
        },
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def create_pair(
    client: TestClient,
    csrf: str,
    *,
    name: str = "pair-1",
    source: str,
    target: str,
    target_space: str = "personal",
    **extra: Any,
) -> dict[str, Any]:
    """``POST /pairs``, assert it succeeded, return the created pair's JSON body.
    ``extra`` passes any other ``SyncPairCreateRequest`` field straight through (e.g.
    ``entity_types``, ``enabled``, ``cadence_seconds``)."""
    payload: dict[str, Any] = {
        "name": name,
        "source": source,
        "target": target,
        "target_space": target_space,
    }
    payload.update(extra)
    response = client.post(f"{API_PREFIX}/pairs", json=payload, headers={CSRF_HEADER: csrf})
    assert response.status_code == 201, response.text
    return dict(response.json())
