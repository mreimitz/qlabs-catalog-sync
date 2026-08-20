"""Retry/backoff behavior, driven against real respx mocks — never real sleeps.

Covers: a 429 with a delta-seconds ``Retry-After`` is honored; a 429 with an
HTTP-date ``Retry-After`` is honored; a 500 is retried with jittered backoff; a
404 is never retried; a transport error is retried; retries are bounded and the
last error surfaces; and retry activity is observable via structlog without ever
logging the ``Authorization`` header.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import structlog.testing

from qlabs_catalog_sync_sdk.http import HttpEndpoint

from .conftest import RecordingSleep


async def test_429_with_delta_seconds_retry_after_is_honored(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    recording_sleep: RecordingSleep,
    base_url: str,
) -> None:
    route = respx_mock.get(f"{base_url}/things").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    endpoint = make_endpoint()

    response = await endpoint.get("/things")

    assert response.status_code == 200
    assert route.call_count == 2
    assert recording_sleep.calls == [pytest.approx(1.0)]


async def test_429_with_http_date_retry_after_is_honored(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    recording_sleep: RecordingSleep,
    base_url: str,
    fixed_now: datetime,
) -> None:
    retry_at = fixed_now + timedelta(seconds=2)
    route = respx_mock.get(f"{base_url}/things").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": format_datetime(retry_at, usegmt=True)}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    endpoint = make_endpoint()

    response = await endpoint.get("/things")

    assert response.status_code == 200
    assert route.call_count == 2
    assert recording_sleep.calls == [pytest.approx(2.0)]


async def test_500_is_retried_with_jittered_backoff(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    recording_sleep: RecordingSleep,
    base_url: str,
) -> None:
    route = respx_mock.get(f"{base_url}/things").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    endpoint = make_endpoint(backoff_base_seconds=0.02, backoff_max_seconds=0.02)

    response = await endpoint.get("/things")

    assert response.status_code == 200
    assert route.call_count == 2
    assert len(recording_sleep.calls) == 1
    # Full-jitter backoff: uniform(0, min(base * 2**0, max)) == uniform(0, 0.02).
    assert 0.0 <= recording_sleep.calls[0] <= 0.02


async def test_404_is_not_retried(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    recording_sleep: RecordingSleep,
    base_url: str,
) -> None:
    route = respx_mock.get(f"{base_url}/things/missing").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    endpoint = make_endpoint()

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await endpoint.get("/things/missing")

    assert excinfo.value.response.status_code == 404
    assert route.call_count == 1
    assert recording_sleep.calls == []


async def test_transport_error_is_retried(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    recording_sleep: RecordingSleep,
    base_url: str,
) -> None:
    route = respx_mock.get(f"{base_url}/things").mock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    endpoint = make_endpoint()

    response = await endpoint.get("/things")

    assert response.status_code == 200
    assert route.call_count == 2
    assert len(recording_sleep.calls) == 1


async def test_retries_are_bounded_and_the_last_error_surfaces(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    recording_sleep: RecordingSleep,
    base_url: str,
) -> None:
    route = respx_mock.get(f"{base_url}/things").mock(return_value=httpx.Response(503))
    endpoint = make_endpoint(max_attempts=3)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await endpoint.get("/things")

    assert excinfo.value.response.status_code == 503
    assert route.call_count == 3
    assert len(recording_sleep.calls) == 2


async def test_retry_is_observable_via_structlog_without_leaking_the_auth_header(
    respx_mock: object,
    make_endpoint: Callable[..., HttpEndpoint],
    base_url: str,
) -> None:
    respx_mock.get(f"{base_url}/things").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    endpoint = make_endpoint(auth=("Bearer", "super-secret-token"))

    with structlog.testing.capture_logs() as captured:
        await endpoint.get("/things")

    retry_events = [entry for entry in captured if entry.get("event") == "http.retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["status_code"] == 429
    assert retry_events[0]["wait_seconds"] == pytest.approx(1.0)

    rendered = repr(captured)
    assert "super-secret-token" not in rendered
    assert "Authorization" not in rendered
