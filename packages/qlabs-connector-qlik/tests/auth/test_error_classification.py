"""``auth.classify_response_error`` / ``auth.classify_transport_error`` — the mapping
from a terminal ``httpx`` failure (``HttpEndpoint`` has already exhausted any retries by
the time these run) onto the SDK's typed exception vocabulary, independent of
``healthcheck()``'s own use of it."""

from __future__ import annotations

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import AuthError, ConnectorError, NotFound, TransientError
from qlabs_connector_qlik.auth import classify_response_error, classify_transport_error


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://acme.eu.qlikcloud.example/api/v1/spaces/space-123")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.parametrize("status_code", [401, 403])
def test_401_and_403_classify_as_auth_error(status_code: int) -> None:
    error = classify_response_error(_status_error(status_code), endpoint="qlik")

    assert isinstance(error, AuthError)
    assert error.retryable is False
    assert error.endpoint == "qlik"
    assert str(status_code) in str(error)


def test_404_classifies_as_not_found() -> None:
    error = classify_response_error(_status_error(404), endpoint="qlik")

    assert isinstance(error, NotFound)
    assert error.retryable is False


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_429_and_5xx_classify_as_transient_error(status_code: int) -> None:
    error = classify_response_error(_status_error(status_code), endpoint="qlik")

    assert isinstance(error, TransientError)
    assert error.retryable is True
    assert str(status_code) in str(error)


def test_unrecognized_status_falls_back_to_a_bare_connector_error() -> None:
    error = classify_response_error(_status_error(400), endpoint="qlik")

    assert type(error) is ConnectorError
    assert not isinstance(error, (AuthError, NotFound, TransientError))


def test_transport_error_classifies_as_transient() -> None:
    request = httpx.Request("GET", "https://acme.eu.qlikcloud.example/api/v1/spaces/space-123")
    exc = httpx.ConnectError("connection refused", request=request)

    error = classify_transport_error(exc, endpoint="qlik")

    assert isinstance(error, TransientError)
    assert error.retryable is True
    assert error.cause is exc
