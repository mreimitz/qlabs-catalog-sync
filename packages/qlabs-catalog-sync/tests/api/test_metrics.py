"""``/metrics`` on the FastAPI app is byte-identical to
``qlabs_catalog_sync.observability.render_metrics`` -- the same function
``ObservabilityServer``'s stdlib handler already calls (T2.7): same Prometheus
text-exposition body, same content type, for a registry with real counters in it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry

from qlabs_catalog_sync.api.app import create_app
from qlabs_catalog_sync.observability import (
    METRIC_READS_TOTAL,
    HealthRegistry,
    PrometheusMetrics,
    render_metrics,
)


def test_empty_registry_matches_render_metrics_exactly() -> None:
    registry = CollectorRegistry()
    expected_body = render_metrics(registry)

    app = create_app(health=HealthRegistry(), metrics_registry=registry)
    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.content == expected_body
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST


def test_registry_with_real_counters_matches_render_metrics_exactly() -> None:
    registry = CollectorRegistry()
    metrics = PrometheusMetrics(registry=registry)
    metrics.increment(
        METRIC_READS_TOTAL, pair="db_to_qlik", endpoint="databricks_prod", entity_type="dataset"
    )
    metrics.increment(
        METRIC_READS_TOTAL, pair="db_to_qlik", endpoint="databricks_prod", entity_type="dataset"
    )

    expected_body = render_metrics(registry)
    assert b"qlabs_sync_reads_total" in expected_body  # sanity: the counter is really in there

    app = create_app(health=HealthRegistry(), metrics_registry=registry)
    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.content == expected_body
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST


def test_metrics_reflect_live_registry_mutations_after_app_construction() -> None:
    """The app must read the registry it was given at request time, not snapshot it at
    construction -- a counter incremented after ``create_app`` still shows up."""
    registry = CollectorRegistry()
    metrics = PrometheusMetrics(registry=registry)
    app = create_app(health=HealthRegistry(), metrics_registry=registry)
    client = TestClient(app)

    before = client.get("/metrics").content
    metrics.increment(METRIC_READS_TOTAL, pair="p", endpoint="e", entity_type="dataset")
    after = client.get("/metrics").content

    assert before != after
    assert after == render_metrics(registry)
