"""Shared fixtures for observability tests: structlog isolation between tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """Restore structlog's library defaults after every test.

    ``configure_logging`` mutates *global* structlog configuration (deliberately — see its
    docstring). Without this, a test that calls it would leak configuration into whichever
    test runs next, in this file or (since structlog config is process-wide) any other.
    """
    try:
        yield
    finally:
        structlog.reset_defaults()
