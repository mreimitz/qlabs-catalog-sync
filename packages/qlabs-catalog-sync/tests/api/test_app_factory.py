"""``create_app`` boots and serves with nothing configured -- the DoD's baseline every
later WP12 task builds on: no configuration store, no console assets, no auth.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from qlabs_catalog_sync.api.app import API_PREFIX, create_app
from qlabs_catalog_sync.observability import HealthRegistry

from .api_helpers import build_app


def test_create_app_returns_a_fastapi_instance_with_no_optional_arguments() -> None:
    app = create_app(health=HealthRegistry(), metrics_registry=CollectorRegistry())
    assert isinstance(app, FastAPI)


def test_api_prefix_is_a_stable_exported_constant() -> None:
    # Future WP12 route tasks mount under this; the SPA fallback keys off it too. If it
    # ever changes, it changes here and only here.
    assert API_PREFIX == "/api"


def test_two_app_instances_do_not_share_health_or_metrics_state() -> None:
    """The factory shape (not a module-level singleton) is what makes this true, and
    what makes the app testable and compatible with T12.2's auth."""
    health_a = HealthRegistry()
    health_a.mark_degraded("qlik_acme", reason="boom")
    app_a = create_app(health=health_a, metrics_registry=CollectorRegistry())
    app_b = create_app(health=HealthRegistry(), metrics_registry=CollectorRegistry())

    client_a = TestClient(app_a, raise_server_exceptions=False)
    client_b = TestClient(app_b, raise_server_exceptions=False)

    assert client_a.get("/healthz").status_code == 503
    assert client_b.get("/healthz").status_code == 200


def test_app_serves_healthz_metrics_and_root_with_nothing_else_configured() -> None:
    app = build_app()
    client = TestClient(app, raise_server_exceptions=False)

    healthz = client.get("/healthz")
    metrics = client.get("/metrics")
    root = client.get("/")

    assert healthz.status_code == 200
    assert metrics.status_code == 200
    # No console assets: "/" must not 500 and must not silently pretend to be a console.
    assert root.status_code == 404
    assert root.headers["content-type"].startswith("application/json")
