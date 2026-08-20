"""``GET /connectors`` against the **real** entry-point registry (C6, WP12/T12.3).

Every other test of this route -- ``tests/api/test_endpoints.py``,
``tests/api/test_console_configures_a_running_engine.py``,
``tests/api/test_serve_single_origin.py`` -- builds its
:class:`~qlabs_catalog_sync.discovery.ConnectorRegistry` from
:class:`~qlabs_catalog_sync_sdk.testing.FakeConnector`, whose ``capabilities()`` answers
happily on an unconfigured instance. Every real connector need not.

That gap shipped a bug. RS-08's connector lifecycle is *discover, configure, ``setup``,
then ``capabilities``*, and the Databricks connector holds the route to it: D6 makes
``tags`` readable only when a SQL warehouse is configured, so an unconfigured instance
raises ``RuntimeError("capabilities() needs the resolved config: call setup(ctx) first")``
rather than inventing an answer. ``GET /connectors`` lists connector *classes*, which have
no configuration by definition -- so on any image with the Databricks connector installed,
which is every real image, the route raised straight through the generic 500 handler and
the console's first screen could not load at all. No test caught it, because no test ever
pointed the route at a real connector.

So this suite deliberately calls :func:`~qlabs_catalog_sync.discovery.discover_connectors`
with no arguments -- the real ``qlabs_catalog_sync.connectors`` entry points installed in
this environment, exactly as engine startup calls it. It is the only test in this
repository that does. If it is ever "simplified" to use a fake, it stops testing the one
thing it exists for.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from qlabs_catalog_sync.api.app import API_PREFIX, create_app
from qlabs_catalog_sync.api.routes.connectors import ConnectorInfo
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.discovery import ConnectorRegistry, discover_connectors
from qlabs_catalog_sync.observability import HealthRegistry
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore

#: The MVP's source connector, and the one whose manifest genuinely depends on its
#: resolved configuration (D6). If this is ever not installed in the dev environment the
#: tests below skip rather than pass vacuously -- a green run that proved nothing is the
#: failure mode this whole module exists to prevent.
_CONFIG_DEPENDENT_CONNECTOR = "databricks"


@pytest.fixture
def real_registry() -> ConnectorRegistry:
    """The real entry-point registry -- no fakes. See the module docstring."""
    return discover_connectors()


@pytest.fixture
def app(real_registry: ConnectorRegistry, tmp_path: pytest.TempPathFactory) -> Iterator[FastAPI]:
    db_url = f"sqlite:///{tmp_path}/state.db"  # type: ignore[str-bytes-safe]
    upgrade_to_head(db_url)
    store = StateStore.from_url(db_url)
    try:
        yield create_app(
            health=HealthRegistry(),
            metrics_registry=CollectorRegistry(),
            config_service=ConfigService(store.engine, real_registry),
            registry=real_registry,
            auth=None,
        )
    finally:
        store.engine.dispose()


def test_the_real_registry_actually_has_connectors(real_registry: ConnectorRegistry) -> None:
    """Guards every other test here from passing vacuously against an empty registry."""
    assert real_registry.names(), (
        "no connectors are installed in this environment -- run `uv sync --all-packages`. "
        "Every assertion below would otherwise pass while proving nothing."
    )


def test_listing_connectors_does_not_500_on_the_real_registry(app: FastAPI) -> None:
    """The regression test. Before the fix this returned 500 on any image with the
    Databricks connector installed, because the route called ``capabilities()`` on an
    unconfigured connector class and let the ``RuntimeError`` escape."""
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"{API_PREFIX}/connectors")

    assert response.status_code == 200, response.text


def test_a_connector_that_cannot_describe_itself_yet_is_available_with_a_reason(
    app: FastAPI, real_registry: ConnectorRegistry
) -> None:
    """A connector whose manifest needs configuration is reported as ``available`` -- it
    loaded, and an operator can register an endpoint against it -- with ``manifest``
    unset and ``manifest_unavailable_reason`` saying why.

    Never ``available=False``: that means discovery could not load the entry point at
    all, which is a different fact an operator acts on differently. And never an empty
    manifest: "describes itself once configured" and "supports nothing" are opposite
    facts, and the console must not render one as the other.
    """
    if _CONFIG_DEPENDENT_CONNECTOR not in real_registry.names():
        pytest.skip(f"the {_CONFIG_DEPENDENT_CONNECTOR!r} connector is not installed here")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"{API_PREFIX}/connectors")
    assert response.status_code == 200, response.text
    entry = next(
        ConnectorInfo.model_validate(item)
        for item in response.json()
        if item["name"] == _CONFIG_DEPENDENT_CONNECTOR
    )

    assert entry.available is True
    assert entry.manifest is None
    assert entry.manifest_unavailable_reason is not None
    assert "setup" in entry.manifest_unavailable_reason


def test_one_silent_connector_never_hides_the_others(
    app: FastAPI, real_registry: ConnectorRegistry
) -> None:
    """The listing is complete even when a connector in it cannot report a manifest --
    the failure is per-connector, not per-request. Before the fix, one connector that
    refused took the whole listing down with it."""
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"{API_PREFIX}/connectors")

    assert response.status_code == 200, response.text
    listed = {item["name"] for item in response.json()}
    assert set(real_registry.names()) <= listed, (
        f"the listing dropped connectors the registry holds: "
        f"{sorted(set(real_registry.names()) - listed)}"
    )


def test_a_connector_that_can_describe_itself_still_reports_a_manifest(
    app: FastAPI, real_registry: ConnectorRegistry
) -> None:
    """The degradation is narrow: a connector that *can* answer without configuration
    still returns a real manifest. A fix that simply stopped reporting manifests would
    pass every other test in this file."""
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"{API_PREFIX}/connectors")
    assert response.status_code == 200, response.text
    described = [
        item
        for item in response.json()
        if item["available"] and item["manifest"] is not None
    ]

    assert described, (
        "no installed connector reported a capability manifest -- the route has stopped "
        "serializing manifests entirely rather than degrading only where it must. "
        f"Registry holds: {sorted(real_registry.names())}"
    )
    assert all(item["manifest_unavailable_reason"] is None for item in described), (
        "a connector reported both a manifest and a reason for not having one"
    )
