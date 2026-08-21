"""translate_snowflake_error: HttpEndpoint-raised errors map onto the SDK's typed
exceptions honestly — 401/403 -> AuthError, 404 -> NotFound, 409 -> ConflictError,
429/5xx/transport -> TransientError, any other 4xx -> CapabilityError.

TENANT-UNVERIFIED: exact Snowflake error JSON payload shapes are not confirmed against
a live tenant; these tests use a plausible ``{"message": ..., "code": ...}`` shape.
"""

from __future__ import annotations

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import (
    AuthError,
    CapabilityError,
    ConflictError,
    NotFound,
    TransientError,
)
from qlabs_connector_snowflake.auth import translate_snowflake_error

ENDPOINT = "snowflake"


def _status_error(status: int, *, json: dict[str, object] | None = None, headers=None):
    request = httpx.Request("GET", "https://acme-primary.snowflakecomputing.com/api/v2/databases")
    response = httpx.Response(status, json=json, headers=headers, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


@pytest.mark.parametrize("status", [401, 403])
def test_401_and_403_map_to_auth_error(status: int) -> None:
    exc = _status_error(status, json={"message": "JWT token is invalid.", "code": "390144"})

    mapped = translate_snowflake_error(exc, endpoint=ENDPOINT)

    assert isinstance(mapped, AuthError)
    assert mapped.retryable is False
    assert mapped.endpoint == ENDPOINT
    assert mapped.cause is exc
    assert "JWT token is invalid" in str(mapped)


def test_404_maps_to_not_found() -> None:
    exc = _status_error(404, json={"message": "Database 'NOPE' does not exist."})

    mapped = translate_snowflake_error(exc, endpoint=ENDPOINT, entity_type="dataset")

    assert isinstance(mapped, NotFound)
    assert mapped.retryable is False
    assert mapped.entity_type == "dataset"


def test_409_maps_to_conflict_error() -> None:
    exc = _status_error(409, json={"message": "Statement handle already in use."})

    mapped = translate_snowflake_error(exc, endpoint=ENDPOINT)

    assert isinstance(mapped, ConflictError)
    assert mapped.retryable is True


def test_429_maps_to_transient_error_with_retry_after() -> None:
    exc = _status_error(429, json={"message": "rate limited"}, headers={"Retry-After": "30"})

    mapped = translate_snowflake_error(exc, endpoint=ENDPOINT)

    assert isinstance(mapped, TransientError)
    assert mapped.retryable is True
    assert mapped.retry_after_seconds == 30.0


def test_429_without_a_retry_after_hint_still_maps_cleanly() -> None:
    exc = _status_error(429, json={"message": "rate limited"})

    mapped = translate_snowflake_error(exc, endpoint=ENDPOINT)

    assert isinstance(mapped, TransientError)
    assert mapped.retry_after_seconds is None


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_maps_to_transient_error(status: int) -> None:
    exc = _status_error(status, json={"message": "internal error"})

    mapped = translate_snowflake_error(exc, endpoint=ENDPOINT)

    assert isinstance(mapped, TransientError)
    assert mapped.retryable is True


def test_other_4xx_maps_to_capability_error() -> None:
    exc = _status_error(400, json={"message": "SQL compilation error: invalid statement"})

    mapped = translate_snowflake_error(exc, endpoint=ENDPOINT)

    assert isinstance(mapped, CapabilityError)
    assert mapped.retryable is False
    assert mapped.operation == "request"


def test_error_detail_falls_back_to_response_text_when_not_json() -> None:
    request = httpx.Request("GET", "https://acme-primary.snowflakecomputing.com/api/v2/databases")
    response = httpx.Response(500, text="upstream timeout", request=request)
    exc = httpx.HTTPStatusError("HTTP 500", request=request, response=response)

    mapped = translate_snowflake_error(exc, endpoint=ENDPOINT)

    assert "upstream timeout" in str(mapped)


def test_transport_level_failure_maps_to_transient_error() -> None:
    request = httpx.Request("GET", "https://acme-primary.snowflakecomputing.com/api/v2/databases")
    exc = httpx.ConnectError("connection reset by peer", request=request)

    mapped = translate_snowflake_error(exc, endpoint=ENDPOINT)

    assert isinstance(mapped, TransientError)
    assert mapped.cause is exc
