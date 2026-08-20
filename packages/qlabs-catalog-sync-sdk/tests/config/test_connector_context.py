"""ConnectorContext wiring, and the injected Clock / MetricsHandle it carries."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from qlabs_catalog_sync_sdk.config import (
    Clock,
    ConnectorConfig,
    ConnectorContext,
    ManualClock,
    MetricsHandle,
    NullMetrics,
    SystemClock,
)


class DummyConfig(ConnectorConfig):
    base_url: str
    api_key: SecretStr = SecretStr("unused")


class RecordingMetrics:
    """A minimal MetricsHandle stand-in that records every call for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float, dict[str, str]]] = []

    def increment(self, name: str, *, value: float = 1.0, **labels: str) -> None:
        self.calls.append(("increment", name, value, labels))

    def observe(self, name: str, value: float, **labels: str) -> None:
        self.calls.append(("observe", name, value, labels))

    def set(self, name: str, value: float, **labels: str) -> None:
        self.calls.append(("set", name, value, labels))


def _build_config() -> DummyConfig:
    return DummyConfig(base_url="https://example.com")


def test_recording_metrics_satisfies_the_metrics_handle_protocol() -> None:
    assert isinstance(RecordingMetrics(), MetricsHandle)
    assert isinstance(NullMetrics(), MetricsHandle)


def test_null_metrics_accepts_calls_and_does_nothing() -> None:
    metrics = NullMetrics()
    metrics.increment("writes_total", value=2.0, endpoint="qlik")
    metrics.observe("cycle_latency_seconds", 1.23, endpoint="qlik")
    metrics.set("backlog_size", 5.0, endpoint="qlik")
    # No exception, no observable state: purely a safe default.


def test_build_wires_logger_metrics_and_clock(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="qlabs.connector.qlik")

    config = _build_config()
    metrics = RecordingMetrics()
    clock = ManualClock()

    ctx = ConnectorContext.build(
        config=config, endpoint="qlik", tenant="acme", metrics=metrics, clock=clock
    )

    assert ctx.config is config
    assert ctx.endpoint == "qlik"
    assert ctx.tenant == "acme"
    assert ctx.metrics is metrics
    assert ctx.clock is clock

    ctx.logger.info("hello from a connector")
    assert len(caplog.records) == 1
    payload = json.loads(caplog.records[0].message)
    assert payload["endpoint"] == "qlik"
    assert payload["tenant"] == "acme"


def test_build_defaults_metrics_and_clock_when_not_supplied() -> None:
    ctx = ConnectorContext.build(config=_build_config(), endpoint="qlik")

    assert isinstance(ctx.metrics, NullMetrics)
    assert isinstance(ctx.clock, SystemClock)
    assert ctx.tenant is None


def test_context_is_frozen() -> None:
    ctx = ConnectorContext.build(config=_build_config(), endpoint="qlik")
    with pytest.raises(AttributeError):
        ctx.endpoint = "other"  # type: ignore[misc]


def test_system_clock_uses_real_time_and_real_sleep() -> None:
    clock: Clock = SystemClock()
    before = clock.now()
    assert before.tzinfo is not None

    async def _run() -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        await clock.sleep(0.01)
        return loop.time() - start

    elapsed = asyncio.run(_run())
    assert elapsed >= 0.01


def test_manual_clock_never_sleeps_and_advances_deterministically() -> None:
    clock = ManualClock(_now=datetime(2026, 1, 1, tzinfo=UTC))

    async def _run() -> None:
        await clock.sleep(5.0)
        await clock.sleep(2.5)

    start_wall_time = datetime.now(UTC)
    asyncio.run(_run())
    real_elapsed = (datetime.now(UTC) - start_wall_time).total_seconds()

    # The whole test ran instantly in wall-clock time...
    assert real_elapsed < 1.0
    # ...yet the clock itself thinks 7.5 simulated seconds passed.
    assert clock.now() == datetime(2026, 1, 1, 0, 0, 7, 500000, tzinfo=UTC)
    assert clock.sleep_calls == [5.0, 2.5]


def test_manual_clock_advance_moves_time_without_recording_a_sleep_call() -> None:
    clock = ManualClock(_now=datetime(2026, 1, 1, tzinfo=UTC))
    clock.advance(60.0)

    assert clock.now() == datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
    assert clock.sleep_calls == []


def test_manual_clock_rejects_negative_sleep() -> None:
    clock = ManualClock()

    async def _run() -> None:
        await clock.sleep(-1.0)

    with pytest.raises(ValueError, match="seconds must be >= 0"):
        asyncio.run(_run())
