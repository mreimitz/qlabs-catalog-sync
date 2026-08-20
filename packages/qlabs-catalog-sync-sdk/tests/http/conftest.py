"""Shared fixtures for HttpEndpoint tests.

A fake ``sleep`` records requested durations instead of ever waiting for real, a
fixed ``clock`` makes HTTP-date ``Retry-After`` assertions exact, and
``make_endpoint`` builds an :class:`HttpEndpoint` wired to both (plus fast,
small-bound retry settings) so every test stays fast and deterministic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import pytest

from qlabs_catalog_sync_sdk.http import HttpEndpoint

BASE_URL = "https://api.example.test"
FIXED_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class RecordingSleep:
    """A fake async sleep: records the requested duration instead of waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def base_url() -> str:
    return BASE_URL


@pytest.fixture
def fixed_now() -> datetime:
    return FIXED_NOW


@pytest.fixture
def fixed_clock(fixed_now: datetime) -> Callable[[], datetime]:
    return lambda: fixed_now


@pytest.fixture
def recording_sleep() -> RecordingSleep:
    return RecordingSleep()


@pytest.fixture
async def make_endpoint(
    base_url: str,
    recording_sleep: RecordingSleep,
    fixed_clock: Callable[[], datetime],
) -> AsyncIterator[Callable[..., HttpEndpoint]]:
    """Factory for a test ``HttpEndpoint``: recorded sleeps, a fixed clock, and a
    small bounded attempt count so a "retries exhausted" test makes few calls.

    Endpoints built through the factory are closed automatically at teardown.
    """
    made: list[HttpEndpoint] = []

    def _make(**overrides: object) -> HttpEndpoint:
        kwargs: dict[str, object] = dict(
            sleep=recording_sleep,
            clock=fixed_clock,
            max_attempts=3,
            backoff_base_seconds=0.01,
            backoff_max_seconds=0.05,
        )
        kwargs.update(overrides)
        endpoint = HttpEndpoint(base_url, **kwargs)  # type: ignore[arg-type]
        made.append(endpoint)
        return endpoint

    yield _make
    for endpoint in made:
        await endpoint.aclose()
