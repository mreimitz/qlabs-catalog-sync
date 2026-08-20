"""``/healthz`` on the FastAPI app is byte-identical to
``qlabs_catalog_sync.observability.render_healthz`` -- the exact function
``ObservabilityServer``'s stdlib handler already calls (T2.7). This app never
re-implements health rendering; these tests prove the two surfaces cannot drift by
comparing this app's response directly against ``render_healthz``'s own return value,
for the same registry, in every state T2.7 defined: empty, all healthy, and degraded.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from qlabs_catalog_sync.api.app import create_app
from qlabs_catalog_sync.observability import HealthRegistry, render_healthz


def test_empty_registry_matches_render_healthz_exactly() -> None:
    health = HealthRegistry()
    expected_status, expected_body = render_healthz(health)

    app = create_app(health=health, metrics_registry=CollectorRegistry())
    response = TestClient(app).get("/healthz")

    assert response.status_code == expected_status == 200
    assert response.content == expected_body
    assert response.headers["content-type"] == "application/json"


def test_all_healthy_matches_render_healthz_exactly() -> None:
    health = HealthRegistry()
    health.mark_healthy("databricks_prod")
    health.mark_healthy("qlik_acme")
    expected_status, expected_body = render_healthz(health)

    app = create_app(health=health, metrics_registry=CollectorRegistry())
    response = TestClient(app).get("/healthz")

    assert response.status_code == expected_status == 200
    assert response.content == expected_body


def test_degraded_component_matches_render_healthz_exactly_including_503() -> None:
    health = HealthRegistry()
    health.mark_healthy("databricks_prod")
    health.mark_degraded("qlik_acme", reason="rate limited: 429 for 5 consecutive cycles")
    expected_status, expected_body = render_healthz(health)
    assert expected_status == 503  # sanity: this is genuinely the degraded case

    app = create_app(health=health, metrics_registry=CollectorRegistry())
    response = TestClient(app, raise_server_exceptions=False).get("/healthz")

    assert response.status_code == expected_status == 503
    assert response.content == expected_body
    assert response.headers["content-type"] == "application/json"


def test_marking_healthy_again_after_degraded_still_matches_render_healthz() -> None:
    health = HealthRegistry()
    health.mark_degraded("qlik_acme", reason="rate limited")
    app = create_app(health=health, metrics_registry=CollectorRegistry())
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/healthz").status_code == 503

    health.mark_healthy("qlik_acme")
    expected_status, expected_body = render_healthz(health)

    response = client.get("/healthz")
    assert response.status_code == expected_status == 200
    assert response.content == expected_body
