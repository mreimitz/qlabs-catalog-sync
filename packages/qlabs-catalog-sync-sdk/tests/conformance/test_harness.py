"""The respx/vcrpy harness helpers, exercised directly (not through the suite).

``assert_no_http_calls`` and ``capture_requests`` are proven both ways: they pass when
their claim is true and raise when it is false, so a connector author trusting them
against their own connector is trusting something that has itself been tested for false
positives and false negatives, not just for going green.
"""

from __future__ import annotations

import httpx
import pytest
import vcr

from qlabs_catalog_sync_sdk.conformance.harness import (
    assert_if_match_sent,
    assert_no_http_calls,
    capture_requests,
    retry_then_succeed,
    vcr_config,
)
from qlabs_catalog_sync_sdk.http import HttpEndpoint


async def test_assert_no_http_calls_passes_when_nothing_is_sent() -> None:
    with assert_no_http_calls():
        pass  # no client, no request — should not raise


async def test_assert_no_http_calls_raises_when_a_request_is_sent() -> None:
    with pytest.raises(AssertionError, match="zero HTTP requests"), assert_no_http_calls():
        async with httpx.AsyncClient() as client:
            await client.get("https://example.test/thing")


async def test_capture_requests_records_method_url_and_headers() -> None:
    with capture_requests() as route:
        async with HttpEndpoint("https://example.test") as endpoint:
            await endpoint.patch("/items/1", headers={"If-Match": "etag-123"})

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.method == "PATCH"
    assert str(request.url) == "https://example.test/items/1"
    assert request.headers["if-match"] == "etag-123"


async def test_capture_requests_default_response_does_not_raise_for_status() -> None:
    with capture_requests() as route:
        async with HttpEndpoint("https://example.test") as endpoint:
            response = await endpoint.get("/things")
    assert response.status_code == 204
    assert route.call_count == 1


async def test_assert_if_match_sent_passes_when_present_and_required() -> None:
    with capture_requests() as route:
        async with HttpEndpoint("https://example.test") as endpoint:
            await endpoint.patch("/items/1", headers={"If-Match": "etag-123"})
    assert_if_match_sent(route.calls, required=True)


async def test_assert_if_match_sent_raises_when_missing_and_required() -> None:
    with capture_requests() as route:
        async with HttpEndpoint("https://example.test") as endpoint:
            await endpoint.patch("/items/1")
    with pytest.raises(AssertionError, match="If-Match"):
        assert_if_match_sent(route.calls, required=True)


async def test_assert_if_match_sent_raises_when_present_but_not_required() -> None:
    with capture_requests() as route:
        async with HttpEndpoint("https://example.test") as endpoint:
            await endpoint.patch("/items/1", headers={"If-Match": "etag-123"})
    with pytest.raises(AssertionError, match="unexpected If-Match"):
        assert_if_match_sent(route.calls, required=False)


async def test_assert_if_match_sent_passes_when_absent_and_not_required() -> None:
    with capture_requests() as route:
        async with HttpEndpoint("https://example.test") as endpoint:
            await endpoint.patch("/items/1")
    assert_if_match_sent(route.calls, required=False)


def test_assert_if_match_sent_raises_on_an_empty_capture() -> None:
    with pytest.raises(AssertionError, match="no requests were captured"):
        assert_if_match_sent([], required=True)


async def test_capture_requests_accepts_raw_httpx_requests_too() -> None:
    """``assert_if_match_sent`` also accepts plain ``httpx.Request`` objects, not just
    respx ``Call`` records — useful when a connector author captured requests some other
    way."""
    with capture_requests() as route:
        async with HttpEndpoint("https://example.test") as endpoint:
            await endpoint.patch("/items/1", headers={"If-Match": "etag-123"})
    requests = [call.request for call in route.calls]
    assert_if_match_sent(requests, required=True)


async def test_retry_then_succeed_lets_a_connector_recover_from_a_429(
    respx_mock: object,
) -> None:
    base_url = "https://example.test"
    respx_mock.get(f"{base_url}/things").mock(
        side_effect=retry_then_succeed(first_status=429, retry_after="0")
    )
    async with HttpEndpoint(
        base_url, max_attempts=3, backoff_base_seconds=0.0, backoff_max_seconds=0.0
    ) as endpoint:
        response = await endpoint.get("/things")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_vcr_config_filters_credentials_and_defaults_to_record_once(tmp_path: object) -> None:
    config = vcr_config(tmp_path)  # type: ignore[arg-type]
    assert isinstance(config, vcr.VCR)
    assert config.record_mode == "once"
    assert "authorization" in config.filter_headers
    assert "x-api-key" in config.filter_headers
