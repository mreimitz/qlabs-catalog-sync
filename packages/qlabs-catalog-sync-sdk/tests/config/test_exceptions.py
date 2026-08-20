"""The typed exception hierarchy: raisable, catchable via the common base, structured."""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.exceptions import (
    AuthError,
    CapabilityError,
    ConflictError,
    ConnectorError,
    NotFound,
    TransientError,
)

ALL_EXCEPTION_TYPES = [TransientError, AuthError, NotFound, ConflictError, CapabilityError]


@pytest.mark.parametrize("exc_type", ALL_EXCEPTION_TYPES)
def test_every_exception_type_is_a_connector_error(
    exc_type: type[ConnectorError],
) -> None:
    assert issubclass(exc_type, ConnectorError)
    assert issubclass(exc_type, Exception)


@pytest.mark.parametrize("exc_type", ALL_EXCEPTION_TYPES)
def test_each_exception_is_raisable_and_catchable_via_the_common_base(
    exc_type: type[ConnectorError],
) -> None:
    with pytest.raises(ConnectorError) as exc_info:
        raise exc_type("something went wrong", endpoint="qlik", entity_type="dataset")

    caught = exc_info.value
    assert isinstance(caught, exc_type)
    assert caught.message == "something went wrong"
    assert caught.endpoint == "qlik"
    assert caught.entity_type == "dataset"
    assert str(caught) == "something went wrong"


@pytest.mark.parametrize(
    ("exc_type", "expected_retryable"),
    [
        (TransientError, True),
        (AuthError, False),
        (NotFound, False),
        (ConflictError, True),
        (CapabilityError, False),
    ],
)
def test_retryable_flag_matches_the_engine_reaction(
    exc_type: type[ConnectorError], expected_retryable: bool
) -> None:
    error = exc_type("boom")
    assert error.retryable is expected_retryable
    # Also true at the class level, so the engine can inspect without instantiating.
    assert exc_type.retryable is expected_retryable


def test_connector_error_base_defaults_to_not_retryable() -> None:
    assert ConnectorError("boom").retryable is False


def test_cause_is_preserved_and_chained() -> None:
    original = ValueError("root cause")

    try:
        try:
            raise original
        except ValueError as inner:
            raise TransientError("wrapping", cause=inner) from inner
    except TransientError as outer:
        assert outer.cause is original
        assert outer.__cause__ is original


def test_transient_error_carries_retry_after_seconds() -> None:
    error = TransientError("rate limited", endpoint="qlik", retry_after_seconds=30.0)
    assert error.retryable is True
    assert error.retry_after_seconds == 30.0

    # Optional, defaults to None when the endpoint gave no hint.
    assert TransientError("timeout").retry_after_seconds is None


def test_not_found_carries_native_key() -> None:
    error = NotFound("no such dataset", endpoint="databricks", native_key="main.retail.orders")
    assert error.native_key == "main.retail.orders"


def test_conflict_error_carries_revision_mismatch() -> None:
    error = ConflictError(
        "etag mismatch",
        endpoint="qlik",
        entity_type="data_product",
        expected_revision="etag-1",
        actual_revision="etag-2",
    )
    assert error.retryable is True
    assert error.expected_revision == "etag-1"
    assert error.actual_revision == "etag-2"


def test_capability_error_carries_field_and_mode() -> None:
    error = CapabilityError(
        "field is read-only",
        endpoint="databricks",
        entity_type="dataset",
        field="tags",
        capability_mode="ro",
    )
    assert error.retryable is False
    assert error.field == "tags"
    assert error.capability_mode == "ro"


def test_repr_includes_structured_context() -> None:
    error = CapabilityError("field is read-only", endpoint="databricks", field="tags")
    text = repr(error)
    assert "CapabilityError" in text
    assert "databricks" in text
