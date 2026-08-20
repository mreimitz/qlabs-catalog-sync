"""An unexpected exception becomes a generic 500 with a correlation id -- its *message*
goes only to the structured log, keyed by that id, and never into the response body.

This is the dishonest-case test the task calls for explicitly: it is written to FAIL the
moment ``api/errors.py``'s generic-exception handler is changed to include ``str(exc)``
(or any exception detail) in the JSON body -- e.g. by someone "helpfully" adding
``detail=str(exc)`` for debuggability. A sentinel value that would only ever appear if
that regression happened is asserted absent from the *entire* raw response text, not just
from the fields we expect it in.
"""

from __future__ import annotations

import structlog
from fastapi.testclient import TestClient

from qlabs_catalog_sync.observability import REDACTION_TEST_PROCESSORS

from .api_helpers import add_raising_route, build_app

_SENTINEL = "SENTINEL-db-password-hunter2-do-not-leak-c9f3a1"


class _BoomWithSecret(RuntimeError):
    def __init__(self) -> None:
        super().__init__(f"connection failed: password={_SENTINEL}")


def test_unexpected_exception_message_never_reaches_the_response_body() -> None:
    app = build_app()
    add_raising_route(app, "/__test/boom", _BoomWithSecret)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/__test/boom")

    assert response.status_code == 500
    assert _SENTINEL not in response.text
    body = response.json()
    assert body["code"] == "internal_error"
    assert _SENTINEL not in body["message"]
    assert body["correlation_id"] is not None
    # The generic message is fixed and uninformative on purpose -- it must not echo
    # anything about *why* this particular request failed.
    assert body["message"] == "an unexpected error occurred"


def test_unexpected_exception_detail_does_reach_the_structured_log() -> None:
    """The flip side: the detail is not simply discarded -- it goes to the log, keyed by
    the same correlation id the client received, so an operator can find it."""
    app = build_app()
    add_raising_route(app, "/__test/boom", _BoomWithSecret)
    client = TestClient(app, raise_server_exceptions=False)

    with structlog.testing.capture_logs(processors=REDACTION_TEST_PROCESSORS) as entries:
        response = client.get("/__test/boom")

    correlation_id = response.json()["correlation_id"]
    matching = [e for e in entries if e.get("correlation_id") == correlation_id]
    assert len(matching) == 1
    assert _SENTINEL in matching[0]["error"]


def test_response_never_contains_a_python_traceback() -> None:
    app = build_app()
    add_raising_route(app, "/__test/boom", _BoomWithSecret)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/__test/boom")

    assert "Traceback (most recent call last)" not in response.text
    assert "_BoomWithSecret" not in response.text
