"""``ObservabilityServer``: a real socket, hit with real HTTP GET requests.

Uses the stdlib ``urllib.request`` rather than a mocking library — this test exists precisely
to prove the handler wiring (routing, status codes, content types, body rendering) works end
to end over an actual connection, not to re-test ``render_metrics``/``render_healthz`` in
isolation (those have their own direct tests).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest
from prometheus_client import CollectorRegistry

from qlabs_catalog_sync.observability import (
    METRIC_READS_TOTAL,
    HealthRegistry,
    ObservabilityServer,
    PrometheusMetrics,
)


@pytest.fixture
def running_server() -> Iterator[tuple[ObservabilityServer, PrometheusMetrics, HealthRegistry]]:
    registry = CollectorRegistry()
    metrics = PrometheusMetrics(registry=registry)
    health = HealthRegistry()
    server = ObservabilityServer(registry=registry, health=health, host="127.0.0.1", port=0)
    server.start()
    try:
        yield server, metrics, health
    finally:
        server.stop()


def _get(server: ObservabilityServer, path: str) -> tuple[int, bytes, str]:
    url = f"http://127.0.0.1:{server.bound_port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - fixed local http url
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def test_metrics_endpoint_returns_exposition_text_containing_metric_names_after_activity(
    running_server: tuple[ObservabilityServer, PrometheusMetrics, HealthRegistry],
) -> None:
    server, metrics, _health = running_server
    metrics.increment(
        METRIC_READS_TOTAL, pair="db_to_qlik", endpoint="databricks_prod", entity_type="dataset"
    )

    status, body, content_type = _get(server, "/metrics")

    assert status == 200
    assert "text/plain" in content_type
    text = body.decode("utf-8")
    assert METRIC_READS_TOTAL in text
    assert 'pair="db_to_qlik"' in text


def test_healthz_returns_200_and_ok_when_healthy(
    running_server: tuple[ObservabilityServer, PrometheusMetrics, HealthRegistry],
) -> None:
    server, _metrics, health = running_server
    health.mark_healthy("qlik_acme")

    status, body, content_type = _get(server, "/healthz")

    assert status == 200
    assert content_type.startswith("application/json")
    assert b'"status": "ok"' in body


def test_healthz_returns_503_and_degraded_when_a_component_is_down(
    running_server: tuple[ObservabilityServer, PrometheusMetrics, HealthRegistry],
) -> None:
    server, _metrics, health = running_server
    health.mark_degraded("qlik_acme", reason="quarantined")

    status, body, _content_type = _get(server, "/healthz")

    assert status == 503
    assert b'"status": "degraded"' in body
    assert b"quarantined" in body


def test_unknown_path_returns_404(
    running_server: tuple[ObservabilityServer, PrometheusMetrics, HealthRegistry],
) -> None:
    server, _metrics, _health = running_server

    status, _body, _content_type = _get(server, "/does-not-exist")

    assert status == 404


def test_bound_port_before_start_raises() -> None:
    server = ObservabilityServer(registry=CollectorRegistry(), health=HealthRegistry())
    with pytest.raises(RuntimeError):
        _ = server.bound_port


def test_starting_twice_raises(
    running_server: tuple[ObservabilityServer, PrometheusMetrics, HealthRegistry],
) -> None:
    server, _metrics, _health = running_server
    with pytest.raises(RuntimeError):
        server.start()


def test_stop_is_idempotent(
    running_server: tuple[ObservabilityServer, PrometheusMetrics, HealthRegistry],
) -> None:
    server, _metrics, _health = running_server
    server.stop()
    server.stop()  # must not raise


def test_context_manager_starts_and_stops() -> None:
    registry = CollectorRegistry()
    health = HealthRegistry()
    with ObservabilityServer(registry=registry, health=health, host="127.0.0.1") as server:
        status, _body, _content_type = _get(server, "/healthz")
        assert status == 200
