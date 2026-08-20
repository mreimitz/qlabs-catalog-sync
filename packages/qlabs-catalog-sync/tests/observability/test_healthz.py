"""``HealthRegistry`` / ``render_healthz``: the healthy, degraded, and empty-registry cases."""

from __future__ import annotations

import json

from qlabs_catalog_sync.observability import HealthRegistry, render_healthz


def test_empty_registry_reports_ok() -> None:
    """Nothing has reported in yet (e.g. process just started) — nothing is known to be broken."""
    health = HealthRegistry()

    status_code, body = render_healthz(health)

    assert status_code == 200
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["components"] == {}


def test_all_components_healthy_reports_ok() -> None:
    health = HealthRegistry()
    health.mark_healthy("databricks_prod")
    health.mark_healthy("qlik_acme")

    status_code, body = render_healthz(health)

    assert status_code == 200
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["components"]["databricks_prod"] == {"healthy": True, "detail": None}
    assert payload["components"]["qlik_acme"] == {"healthy": True, "detail": None}


def test_one_degraded_endpoint_flips_the_whole_response_to_degraded() -> None:
    """A process that says 'ok' while an endpoint is quarantined would hide exactly the
    condition an operator needs to see — so one bad component is enough."""
    health = HealthRegistry()
    health.mark_healthy("databricks_prod")
    health.mark_degraded("qlik_acme", reason="rate limited: 429 for 5 consecutive cycles")

    status_code, body = render_healthz(health)

    assert status_code == 503
    payload = json.loads(body)
    assert payload["status"] == "degraded"
    assert payload["components"]["databricks_prod"]["healthy"] is True
    assert payload["components"]["qlik_acme"] == {
        "healthy": False,
        "detail": "rate limited: 429 for 5 consecutive cycles",
    }


def test_marking_healthy_again_clears_the_degraded_state() -> None:
    health = HealthRegistry()
    health.mark_degraded("qlik_acme", reason="rate limited")
    assert render_healthz(health)[0] == 503

    health.mark_healthy("qlik_acme")

    status_code, _body = render_healthz(health)
    assert status_code == 200
