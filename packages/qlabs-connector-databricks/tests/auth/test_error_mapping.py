"""translate_databricks_error: databricks-sdk (and transport) errors map onto the
SDK's typed exceptions honestly — 401/403 -> AuthError (not retryable, quarantines the
endpoint), 429/5xx/transport -> TransientError (retryable)."""

from __future__ import annotations

import pytest
from databricks.sdk.errors.platform import (
    InternalError,
    PermissionDenied,
    TemporarilyUnavailable,
    TooManyRequests,
    Unauthenticated,
)

from qlabs_catalog_sync_sdk.exceptions import AuthError, TransientError
from qlabs_connector_databricks.auth import translate_databricks_error

ENDPOINT = "databricks"


@pytest.mark.parametrize("exc_cls", [Unauthenticated, PermissionDenied])
def test_401_and_403_map_to_auth_error(exc_cls):
    source = exc_cls("bad credentials")

    mapped = translate_databricks_error(source, endpoint=ENDPOINT)

    assert isinstance(mapped, AuthError)
    assert mapped.retryable is False
    assert mapped.endpoint == ENDPOINT
    assert mapped.cause is source


def test_429_maps_to_transient_error_with_retry_after():
    source = TooManyRequests("slow down", retry_after_secs=30)

    mapped = translate_databricks_error(source, endpoint=ENDPOINT)

    assert isinstance(mapped, TransientError)
    assert mapped.retryable is True
    assert mapped.retry_after_seconds == 30.0
    assert mapped.cause is source


def test_429_without_a_retry_after_hint_still_maps_cleanly():
    source = TooManyRequests("slow down")

    mapped = translate_databricks_error(source, endpoint=ENDPOINT)

    assert isinstance(mapped, TransientError)
    assert mapped.retry_after_seconds is None


@pytest.mark.parametrize("exc_cls", [InternalError, TemporarilyUnavailable])
def test_5xx_maps_to_transient_error(exc_cls):
    source = exc_cls("workspace is having a bad day")

    mapped = translate_databricks_error(source, endpoint=ENDPOINT)

    assert isinstance(mapped, TransientError)
    assert mapped.retryable is True


def test_transport_level_os_error_maps_to_transient_error():
    source = ConnectionError("connection reset by peer")

    mapped = translate_databricks_error(source, endpoint=ENDPOINT)

    assert isinstance(mapped, TransientError)
    assert mapped.cause is source
