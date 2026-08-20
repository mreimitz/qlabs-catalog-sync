"""Config base and connector context.

WP1 / T1.7. Provides:

* :class:`ConnectorConfig` — the ``pydantic-settings`` base every connector subclasses
  to declare its config and secrets. The engine builds and validates one instance per
  configured *endpoint* (via :meth:`ConnectorConfig.for_endpoint`) and injects it —
  connectors never read the environment directly (RM-01 agent-guide conventions).
* :class:`ConnectorContext` — what ``Connector.setup(ctx)`` (T1.2) receives: the
  resolved config, a structlog logger already bound with endpoint/tenant context and
  routed through secret redaction, a narrow :class:`MetricsHandle`, and an injected
  :class:`Clock`.
* The :class:`Clock` and :class:`MetricsHandle` protocols themselves, plus a
  production (:class:`SystemClock`) and a test (:class:`ManualClock`) clock, and a
  no-op (:class:`NullMetrics`) metrics handle so ``ConnectorContext`` is usable before
  the engine wires a real one in.

Secret-typed fields on a ``ConnectorConfig`` subclass should use pydantic's
``SecretStr``/``SecretBytes`` — pydantic redacts those from ``repr()``, ``str()``,
``model_dump()`` and ``model_dump_json()`` by construction, before this module or
``logging.py`` even get involved.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, Protocol, Self, TypeVar, runtime_checkable

import structlog
from pydantic_settings import BaseSettings, SettingsConfigDict

from .logging import get_connector_logger

__all__ = [
    "Clock",
    "ConnectorConfig",
    "ConnectorContext",
    "ManualClock",
    "MetricsHandle",
    "NullMetrics",
    "SystemClock",
]


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------


def _normalize_endpoint_key(endpoint: str) -> str:
    """Turn an arbitrary endpoint key into an env-var-safe, upper-cased segment."""
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", endpoint.strip()).strip("_")
    if not normalized:
        raise ValueError(
            f"endpoint key must contain at least one alphanumeric character: {endpoint!r}"
        )
    return normalized.upper()


class ConnectorConfig(BaseSettings):
    """Base for a connector's configuration and secrets.

    A connector declares one ``ConnectorConfig`` subclass (its ``ConfigModel``, T1.2)
    listing the fields it needs — plain values and, for anything sensitive,
    ``SecretStr``/``SecretBytes``. The *engine* is the only thing that constructs
    instances, via :meth:`for_endpoint`, and hands the validated result to the
    connector inside :class:`ConnectorContext`. A connector must never instantiate its
    own config or read ``os.environ`` itself.

    Nested sub-models (e.g. an ``auth: OAuth2M2M | ApiKey`` field) read from further
    ``__``-delimited env var segments, since ``env_nested_delimiter="__"`` is set here.
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        env_nested_delimiter="__",
        populate_by_name=True,
    )

    @classmethod
    def for_endpoint(cls, endpoint: str, /, **values: Any) -> Self:
        """Build a validated instance scoped to one endpoint's environment.

        Only ``<ENDPOINT>__<FIELD>`` variables are read (case-insensitive; nested
        fields go one ``__`` segment deeper, e.g. ``<ENDPOINT>__AUTH__CLIENT_SECRET``),
        so two endpoints of the same connector type running in one process — two Qlik
        tenants, for instance — are configured independently and never read each
        other's variables. ``endpoint`` is normalized (non-alphanumerics become
        ``_``, upper-cased) before use, so a config key like ``"qlik-acme"`` reads
        ``QLIK_ACME__...``.

        ``values`` are explicit overrides — already-resolved secrets from a secret
        manager, or test fixtures — that take precedence over the environment, exactly
        like any other pydantic-settings init kwarg.
        """
        prefix = f"{_normalize_endpoint_key(endpoint)}__"
        return cls(_env_prefix=prefix, **values)  # type: ignore[call-arg]


# --------------------------------------------------------------------------------------
# Clock
# --------------------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """Time source a connector reads instead of calling ``datetime.now()`` /
    ``asyncio.sleep()`` directly.

    Injected via :class:`ConnectorContext` so tests control time deterministically —
    no real sleeping in a test suite, no wall-clock flakiness in assertions that
    depend on elapsed time (backoff schedules, watermark timestamps, ...).
    """

    def now(self) -> datetime:
        """The current time, timezone-aware (UTC)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend for ``seconds`` — real time in production, simulated in tests."""
        ...


@dataclass(slots=True)
class SystemClock:
    """Production :class:`Clock`: real wall-clock time, real ``asyncio.sleep``."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(slots=True)
class ManualClock:
    """Test :class:`Clock`: time only moves when told to, never by waiting.

    Starts at a fixed instant (2024-01-01T00:00:00Z by default). ``sleep()`` does not
    actually suspend — it advances the clock by ``seconds`` and records the call in
    ``sleep_calls``, so a test can assert both that code "waited" and for how long,
    with the whole test running instantly.
    """

    _now: datetime = field(default_factory=lambda: datetime(2024, 1, 1, tzinfo=UTC))
    sleep_calls: list[float] = field(default_factory=list)

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("seconds must be >= 0")
        self.sleep_calls.append(seconds)
        self._now += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        """Move time forward without going through (and without recording) ``sleep``."""
        self._now += timedelta(seconds=seconds)


# --------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------


@runtime_checkable
class MetricsHandle(Protocol):
    """The narrow metrics surface a connector or the engine's shared code can call.

    Deliberately not a prometheus dependency: this is a ``Protocol`` so the SDK never
    imports ``prometheus_client``. The engine (T2.7) owns the real Prometheus wiring
    and supplies an implementation of this shape; :class:`NullMetrics` below is the
    ``ConnectorContext`` default until it does. Label values are keyword-only strings
    (Prometheus label values are always strings) so a call site never has to
    ``str()``-cast before passing them.
    """

    def increment(self, name: str, *, value: float = 1.0, **labels: str) -> None:
        """Add ``value`` to counter ``name`` (e.g. writes, conflicts, 429s)."""
        ...

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Record one observation of ``value`` for histogram/summary ``name`` (e.g.
        cycle latency)."""
        ...

    def set(self, name: str, value: float, **labels: str) -> None:
        """Set gauge ``name`` to ``value`` (e.g. current backlog size)."""
        ...


@dataclass(slots=True)
class NullMetrics:
    """A :class:`MetricsHandle` that records nothing.

    The ``ConnectorContext`` default so connectors and their tests can always call
    ``ctx.metrics.increment(...)`` without a real Prometheus registry present.
    """

    def increment(self, name: str, *, value: float = 1.0, **labels: str) -> None:
        return None

    def observe(self, name: str, value: float, **labels: str) -> None:
        return None

    def set(self, name: str, value: float, **labels: str) -> None:
        return None


# --------------------------------------------------------------------------------------
# ConnectorContext
# --------------------------------------------------------------------------------------


ConfigT = TypeVar("ConfigT", bound=ConnectorConfig)


@dataclass(frozen=True, slots=True)
class ConnectorContext(Generic[ConfigT]):
    """What the engine passes to ``Connector.setup(ctx)`` (the ABC lives in T1.2).

    * ``config`` — the connector's validated ``ConnectorConfig`` subclass instance,
      already bound by the engine (see :meth:`ConnectorConfig.for_endpoint`).
    * ``logger`` — a structlog logger already bound with ``endpoint``/``tenant``
      context, routed through the SDK's secret-redaction processor
      (``logging.redact_secrets``) by construction.
    * ``metrics`` — a :class:`MetricsHandle`; defaults to :class:`NullMetrics`.
    * ``clock`` — a :class:`Clock`; defaults to :class:`SystemClock`.
    * ``endpoint`` / ``tenant`` — duplicated off the logger's bound context for
      convenient direct access without re-deriving them from the logger.

    Construct via :meth:`build` rather than the dataclass constructor directly — it
    is what wires ``logger`` from ``endpoint``/``tenant`` and fills in the metrics/clock
    defaults.
    """

    config: ConfigT
    logger: structlog.stdlib.BoundLogger
    metrics: MetricsHandle
    clock: Clock
    endpoint: str
    tenant: str | None = None

    @classmethod
    def build(
        cls,
        *,
        config: ConfigT,
        endpoint: str,
        tenant: str | None = None,
        metrics: MetricsHandle | None = None,
        clock: Clock | None = None,
        **log_context: Any,
    ) -> ConnectorContext[ConfigT]:
        """Build a context, wiring the bound logger and defaulting metrics/clock.

        ``log_context`` is forwarded to :func:`~qlabs_catalog_sync_sdk.logging.get_connector_logger`
        as additional bound fields (e.g. a run id) beyond ``endpoint``/``tenant``.
        """
        return cls(
            config=config,
            logger=get_connector_logger(endpoint=endpoint, tenant=tenant, **log_context),
            metrics=metrics if metrics is not None else NullMetrics(),
            clock=clock if clock is not None else SystemClock(),
            endpoint=endpoint,
            tenant=tenant,
        )
