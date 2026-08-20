"""Observability: structured logging, Prometheus metrics, and a healthz/metrics HTTP surface.

WP2 / T2.7. Three things live here:

* :func:`configure_logging` + :func:`bind_sync_context` (+ :func:`get_logger`) — structlog
  wired for the *process*. The SDK's ``qlabs_catalog_sync_sdk.logging`` deliberately never
  calls ``structlog.configure()`` itself (mutating global structlog config is an
  application's call, not a library's) but reuses its :func:`~qlabs_catalog_sync_sdk.logging.
  redact_secrets` processor unconditionally, so no engine log line — connector-authored or
  engine-authored — can ever emit a secret. This engine *is* the application, so configuring
  structlog globally is legitimately this module's job, and it does so explicitly via
  :func:`configure_logging`. :func:`bind_sync_context` is the ergonomic way the sync loop
  (T2.4) binds pair/endpoint/entity_type/neutral_id once per cycle (or per record) so every
  log line inside inherits it without formatting it into the message string, built on
  ``structlog.contextvars`` so binding is asyncio-task-local and cannot leak between
  concurrently-running pairs.
* :class:`PrometheusMetrics` — the engine's implementation of the SDK's
  ``qlabs_catalog_sync_sdk.config.MetricsHandle`` protocol, backed by ``prometheus_client``.
  The SDK never imports ``prometheus_client``; this is the seam where a connector's
  ``ctx.metrics`` calls (injected by ``ConnectorContext``) land on real metrics. The registry
  is always injectable (never the ``prometheus_client`` global default), so constructing one
  is side-effect-free and safe to do many times in a test suite.
* :class:`HealthRegistry` + :class:`ObservabilityServer` — the small HTTP surface exposing
  ``/healthz`` and ``/metrics``. Built on the stdlib ``http.server`` (a background thread),
  not an ASGI/WSGI framework: none is a pinned dependency of this package, and the
  architecture doc is explicit that this is a worker process, not an API — a tiny
  health/metrics surface is enough (RS-07 section 6).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, TextIO, cast

import structlog
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from qlabs_catalog_sync_sdk.logging import redact_secrets

if TYPE_CHECKING:
    from qlabs_catalog_sync_sdk.config import MetricsHandle

__all__ = [
    "METRIC_CYCLE_DURATION_SECONDS",
    "METRIC_ERRORS_TOTAL",
    "METRIC_RATE_LIMITED_TOTAL",
    "METRIC_READS_TOTAL",
    "METRIC_SKIPS_TOTAL",
    "METRIC_WRITES_APPLIED_TOTAL",
    "METRIC_WRITES_PLANNED_TOTAL",
    "HealthRegistry",
    "HealthStatus",
    "ObservabilityServer",
    "PrometheusMetrics",
    "bind_sync_context",
    "configure_logging",
    "get_logger",
    "render_healthz",
    "render_metrics",
]


# ============================================================================================
# Logging
# ============================================================================================


def configure_logging(*, level: int = logging.INFO, stream: TextIO | None = None) -> None:
    """Configure structlog (and the stdlib ``logging`` it rides on) for this process.

    Call once at process startup (the CLI, T2.8). Safe to call again — e.g. once per test —
    since each call fully replaces the prior configuration rather than layering on top of it.

    Every event is rendered as one JSON object and written through the stdlib ``logging``
    module to ``stream`` (``sys.stdout`` by default), so anything already routing through
    stdlib logging (APScheduler, httpx, pytest's ``caplog``) lands in the same place instead
    of a second, disconnected sink. ``stream`` is injectable so a test can capture and assert
    on exactly what the pipeline renders, without touching real stdout.

    The processor chain, in order:

    1. ``structlog.contextvars.merge_contextvars`` — pulls in whatever
       :func:`bind_sync_context` (or a connector's own binding) has bound for the current
       asyncio task.
    2. ``structlog.stdlib.add_log_level``
    3. an ISO-8601 UTC timestamp
    4. the SDK's :func:`~qlabs_catalog_sync_sdk.logging.redact_secrets` — reused, not
       reimplemented, so engine and connector logs are redacted by the exact same rules.
    5. the stdlib bridge + JSON rendering.
    """
    target_stream: TextIO = stream if stream is not None else sys.stdout

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
    )
    handler = logging.StreamHandler(target_stream)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level)


@contextmanager
def bind_sync_context(
    *,
    pair: str | None = None,
    endpoint: str | None = None,
    entity_type: str | None = None,
    neutral_id: str | None = None,
    **extra: str,
) -> Iterator[None]:
    """Bind sync-cycle context onto every log line emitted inside this block.

    ``pair``, ``endpoint``, ``entity_type`` and ``neutral_id`` are named explicitly because
    they are the four things a sync log line needs to answer "which sync, on which endpoint,
    for which kind of thing, for which record" (T2.7's definition of done); ``extra`` covers
    anything else worth binding (e.g. a run id) without growing this signature. A ``None``
    value is dropped rather than bound as the literal string ``"None"`` — a create-candidate
    that has not been assigned a ``neutral_id`` yet should not claim one.

    Built on ``structlog.contextvars.bound_contextvars``, i.e. Python's :mod:`contextvars`:
    every ``asyncio.Task`` captures its own copy of the current context when it is created, so
    binding inside one concurrently-running pair's task can never leak into another's — this
    is proven under real concurrency in
    ``tests/observability/test_context_concurrency.py``, not asserted by inspection alone.

    Nestable: an outer ``bind_sync_context(pair=...)`` for the whole cycle composes with an
    inner ``bind_sync_context(entity_type=..., neutral_id=...)`` per record — the inner
    block's bindings are unwound (not just cleared) on exit, restoring exactly what the outer
    block had bound, including when the same key is bound at both levels.
    """
    values: dict[str, str] = {
        key: value
        for key, value in {
            "pair": pair,
            "endpoint": endpoint,
            "entity_type": entity_type,
            "neutral_id": neutral_id,
            **extra,
        }.items()
        if value is not None
    }
    with structlog.contextvars.bound_contextvars(**values):
        yield


#: The context + redaction portion of :func:`configure_logging`'s processor chain, exposed so
#: tests can exercise this module's *real* processors — not a re-declared copy of them — via
#: ``structlog.testing.capture_logs(processors=REDACTION_TEST_PROCESSORS)``, without also
#: pulling in the final JSON-rendering stage (which ``capture_logs`` replaces anyway).
REDACTION_TEST_PROCESSORS: tuple[structlog.typing.Processor, ...] = (
    structlog.contextvars.merge_contextvars,
    redact_secrets,
)


def get_logger(*args: Any, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Thin, typed re-export of ``structlog.get_logger``.

    Exists so engine modules pull their logger from this module — alongside
    :func:`configure_logging` and :func:`bind_sync_context` — rather than reaching into
    ``structlog`` directly. Requires :func:`configure_logging` to have run once at process
    startup for the redaction/context pipeline to be in effect; structlog's own (unredacted)
    defaults apply before that, exactly as they would for any other structlog user.
    """
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(*args, **initial_values))


# ============================================================================================
# Metrics
# ============================================================================================

#: Cycle latency (Histogram). Labeled by ``pair`` + ``entity_type`` — a cycle covers both the
#: source read and the target write for one pair/entity-type combination, so it is not
#: endpoint-specific. This is the label set that answers "which pair is slow".
METRIC_CYCLE_DURATION_SECONDS = "qlabs_sync_cycle_duration_seconds"

#: Records read from a source endpoint (Counter).
METRIC_READS_TOTAL = "qlabs_sync_reads_total"

#: Writes the diff computed, before any is applied (Counter).
METRIC_WRITES_PLANNED_TOTAL = "qlabs_sync_writes_planned_total"

#: Writes actually applied to the Qlik target (Counter).
METRIC_WRITES_APPLIED_TOTAL = "qlabs_sync_writes_applied_total"

#: Idempotent no-op skips: read + diffed, nothing changed since the last sync (Counter).
METRIC_SKIPS_TOTAL = "qlabs_sync_skips_total"

#: HTTP 429 (rate-limited) responses received from an endpoint (Counter). Labeled by
#: ``endpoint`` alone — a shared endpoint can be hit by several pairs, and the question this
#: metric answers ("which endpoint is throttling us") is about the endpoint, not the pair.
METRIC_RATE_LIMITED_TOTAL = "qlabs_sync_rate_limited_total"

#: Sync-cycle errors (Counter), labeled by which pair hit them and which endpoint was involved.
METRIC_ERRORS_TOTAL = "qlabs_sync_errors_total"

# Cardinality choice: every label above is a finite, config-defined domain — pair names and
# endpoint names come from EngineConfig (a handful each in practice), entity_type is a 4-member
# enum (qlabs_catalog_sync_sdk.models.EntityType). Deliberately absent: neutral_id, native keys,
# catalog/schema names, or anything else with effectively unbounded cardinality — one label like
# that turns a handful of time series into millions and is exactly the mistake the task calls
# out to avoid.
_CYCLE_DURATION_BUCKETS: tuple[float, ...] = (
    0.1,
    0.5,
    1,
    2.5,
    5,
    10,
    30,
    60,
    120,
    300,
    600,
    1200,
    float("inf"),
)

_COUNTER_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    METRIC_READS_TOTAL: (
        "Records read from a source endpoint.",
        ("pair", "endpoint", "entity_type"),
    ),
    METRIC_WRITES_PLANNED_TOTAL: (
        "Writes the diff computed for the target, before any is applied.",
        ("pair", "entity_type"),
    ),
    METRIC_WRITES_APPLIED_TOTAL: (
        "Writes actually applied to the Qlik target.",
        ("pair", "entity_type"),
    ),
    METRIC_SKIPS_TOTAL: (
        "Idempotent no-op skips: read and diffed, nothing changed since the last sync.",
        ("pair", "entity_type"),
    ),
    METRIC_RATE_LIMITED_TOTAL: (
        "HTTP 429 (rate-limited) responses received from an endpoint.",
        ("endpoint",),
    ),
    METRIC_ERRORS_TOTAL: (
        "Sync-cycle errors, by pair and the endpoint involved.",
        ("pair", "endpoint"),
    ),
}

_HISTOGRAM_SPECS: dict[str, tuple[str, tuple[str, ...], tuple[float, ...]]] = {
    METRIC_CYCLE_DURATION_SECONDS: (
        "Wall-clock duration of one sync cycle, in seconds.",
        ("pair", "entity_type"),
        _CYCLE_DURATION_BUCKETS,
    ),
}


class PrometheusMetrics:
    """The engine's ``MetricsHandle`` (see ``qlabs_catalog_sync_sdk.config``) implementation.

    Backed by ``prometheus_client``. The SDK's ``MetricsHandle`` is a ``Protocol`` precisely
    so it never has to import ``prometheus_client`` itself; this class is the concrete
    implementation the engine hands to a connector via ``ConnectorContext.metrics`` and uses
    for its own sync-loop metrics.

    ``registry`` is always injectable and defaults to a **fresh**
    ``prometheus_client.CollectorRegistry()`` — never the ``prometheus_client`` global default
    registry. That makes constructing one side-effect-free (safe to do many times in one test
    session, safe on module reload) and lets a test inspect exactly the metrics one instance
    produced without cross-test pollution via the process-wide default registry.

    The seven counters/histogram named by the T2.7 plan (`METRIC_*` constants above) are
    pre-registered at construction with a fixed, curated label set each — this is a closed,
    curated surface, not an open "any name" metrics sink: :meth:`increment`/:meth:`observe`
    raise ``KeyError`` for any other name, which is exactly what keeps the label sets (and
    therefore cardinality) intentional rather than accidental. Gauges were not named by the
    plan, so :meth:`set` is the one open door: it lazily registers a ``Gauge`` the first time a
    given name is used, with labelnames inferred from that call — still fully usable for a
    later need (e.g. a backlog-size gauge) without pre-guessing every gauge the engine will
    ever want.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry: CollectorRegistry = registry if registry is not None else CollectorRegistry()
        self._counters: dict[str, Counter] = {
            name: Counter(name, doc, labelnames=labelnames, registry=self.registry)
            for name, (doc, labelnames) in _COUNTER_SPECS.items()
        }
        self._histograms: dict[str, Histogram] = {
            name: Histogram(
                name, doc, labelnames=labelnames, buckets=buckets, registry=self.registry
            )
            for name, (doc, labelnames, buckets) in _HISTOGRAM_SPECS.items()
        }
        self._gauges: dict[str, Gauge] = {}

    def increment(self, name: str, *, value: float = 1.0, **labels: str) -> None:
        """Add ``value`` to counter ``name`` — one of the ``METRIC_*_TOTAL`` constants."""
        counter = self._counters.get(name)
        if counter is None:
            raise KeyError(
                f"{name!r} is not one of this engine's fixed counters: {sorted(self._counters)}"
            )
        counter.labels(**labels).inc(value)

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Record one observation for histogram ``name`` (``METRIC_CYCLE_DURATION_SECONDS``)."""
        histogram = self._histograms.get(name)
        if histogram is None:
            raise KeyError(
                f"{name!r} is not one of this engine's fixed histograms: {sorted(self._histograms)}"
            )
        histogram.labels(**labels).observe(value)

    def set(self, name: str, value: float, **labels: str) -> None:
        """Set gauge ``name`` to ``value``, registering it on first use.

        Unlike :meth:`increment`/:meth:`observe`, ``name`` is not restricted to a fixed list —
        no gauge is named by the T2.7 plan, so the label set (and therefore cardinality) is the
        caller's responsibility here, same as any direct ``prometheus_client`` use.
        """
        gauge = self._gauges.get(name)
        if gauge is None:
            gauge = Gauge(
                name, f"Engine gauge {name!r}.", labelnames=tuple(labels), registry=self.registry
            )
            self._gauges[name] = gauge
        if labels:
            gauge.labels(**labels).set(value)
        else:
            gauge.set(value)


if TYPE_CHECKING:
    # Static proof, not just a runtime isinstance check: PrometheusMetrics must satisfy
    # MetricsHandle. Never executed (TYPE_CHECKING is always False at runtime), so this costs
    # nothing and registers nothing.
    def _typecheck_prometheus_metrics_satisfies_metrics_handle() -> MetricsHandle:
        return PrometheusMetrics()


def render_metrics(registry: CollectorRegistry) -> bytes:
    """Render ``registry`` in Prometheus text exposition format — the body of ``/metrics``."""
    return generate_latest(registry)


# ============================================================================================
# Health
# ============================================================================================


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """The last-reported health of one named component (typically an endpoint)."""

    name: str
    healthy: bool
    detail: str | None = None


class HealthRegistry:
    """Tracks per-component health so ``/healthz`` reflects real state, not just process liveness.

    A component is anything worth reporting on independently — in practice, a configured
    endpoint (so a quarantined Databricks connector is visible even while Qlik itself is fine).
    The sync loop (T2.4) calls :meth:`mark_healthy`/:meth:`mark_degraded` as endpoints succeed
    or fail; :meth:`snapshot` is what :func:`render_healthz` (and therefore ``/healthz``) reads.

    Design choice: an empty registry — nothing has reported in yet, e.g. immediately after
    process startup before the first cycle has run — is **healthy**. There is nothing yet known
    to be broken, and refusing to answer "ok" before the first cycle would make every fresh
    deployment fail its readiness probe for no operational reason. From then on, overall status
    is healthy only while *every* registered component is: a process that answers "ok" while an
    endpoint is quarantined is a liability (it hides exactly the condition an operator needs to
    see), so one degraded component is enough to flip the whole response to degraded / HTTP 503.

    Thread-safe: the HTTP surface reads this from its own server thread while the engine's
    asyncio event loop (a different thread, in the normal deployment shape) writes to it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._components: dict[str, HealthStatus] = {}

    def mark_healthy(self, name: str) -> None:
        """Record that component ``name`` is currently healthy."""
        with self._lock:
            self._components[name] = HealthStatus(name=name, healthy=True)

    def mark_degraded(self, name: str, *, reason: str) -> None:
        """Record that component ``name`` is degraded, with a human-readable ``reason``."""
        with self._lock:
            self._components[name] = HealthStatus(name=name, healthy=False, detail=reason)

    def snapshot(self) -> dict[str, Any]:
        """The current overall + per-component status, as a JSON-serializable dict.

        ``{"status": "ok" | "degraded", "components": {name: {"healthy": ..., "detail": ...}}}``.
        """
        with self._lock:
            components = dict(self._components)
        overall_healthy = all(status.healthy for status in components.values())
        return {
            "status": "ok" if overall_healthy else "degraded",
            "components": {
                name: {"healthy": status.healthy, "detail": status.detail}
                for name, status in components.items()
            },
        }


def render_healthz(health: HealthRegistry) -> tuple[int, bytes]:
    """Render the ``/healthz`` response as ``(http_status_code, json_body)``.

    200 when every reporting component is healthy (or none have reported yet); 503 the moment
    any component is degraded — see :class:`HealthRegistry` for why.
    """
    snapshot = health.snapshot()
    status_code = 200 if snapshot["status"] == "ok" else 503
    return status_code, json.dumps(snapshot).encode("utf-8")


# ============================================================================================
# HTTP surface
# ============================================================================================


def _build_handler(
    registry: CollectorRegistry, health: HealthRegistry
) -> type[BaseHTTPRequestHandler]:
    """Build a request-handler class closed over ``registry``/``health``.

    A closure (rather than module-level globals) is what makes :class:`ObservabilityServer`
    instances independent of each other and of any global state — two servers in the same
    process (e.g. two tests running back to back) never share a registry by accident.
    """

    class _Handler(BaseHTTPRequestHandler):
        server_version = "qlabs-catalog-sync-observability/1.0"

        def log_message(self, format: str, *args: object) -> None:
            # Silence BaseHTTPRequestHandler's default stderr access logging: this process's
            # logs go through structlog (configure_logging), not raw stderr writes.
            return

        def do_GET(self) -> None:
            if self.path == "/healthz":
                status_code, body = render_healthz(health)
                self._respond(status_code, body, "application/json")
            elif self.path == "/metrics":
                self._respond(200, render_metrics(registry), CONTENT_TYPE_LATEST)
            else:
                self._respond(404, b"not found", "text/plain; charset=utf-8")

        def _respond(self, status_code: int, body: bytes, content_type: str) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


class ObservabilityServer:
    """The small HTTP surface exposing ``/healthz`` and ``/metrics``.

    .. note::

       **No longer the service's HTTP surface.** RM-06 decision C8 requires the REST API,
       the console's static assets, ``/healthz`` and ``/metrics`` to share one process,
       one origin and one port, which a second listener on its own thread cannot do. Since
       WP12, ``cli/serve_command.py`` runs the FastAPI application
       (:func:`qlabs_catalog_sync.api.app.create_app` via
       :class:`qlabs_catalog_sync.api.server.ApiServer`) instead, and that app renders both
       endpoints through the very same :func:`render_healthz` / :func:`render_metrics`
       functions this class calls -- byte for byte, asserted by ``tests/api/test_healthz.py``
       and ``tests/api/test_metrics.py``.

       This class is kept because it is the reference those parity tests are written
       against and it remains independently tested, but nothing in the running service
       starts it. Removing it is a deliberate follow-up, not an oversight.

    Built on the stdlib ``http.server.ThreadingHTTPServer`` running in a background daemon
    thread — not an ASGI/WSGI framework. Neither ``prometheus_client`` nor any part of this
    package pulls one in, and the architecture doc (RS-07 section 6) is explicit that this is
    a worker process, not an API: "a tiny health/metrics HTTP surface is enough". A background
    thread also keeps it fully decoupled from the engine's asyncio event loop — ``start()``/
    ``stop()`` are plain synchronous calls, usable from sync startup code or via
    ``loop.run_in_executor`` from async code, and a slow or stuck probe request can never block
    a sync cycle (they run on entirely separate threads).

    ``host`` defaults to ``"0.0.0.0"`` — the realistic default for the deployment shape this is
    built for (a container behind a Kubernetes liveness/readiness probe, RS-07 section 6),
    which needs the surface reachable from outside the container's own network namespace.
    ``port`` defaults to ``0`` (let the OS pick a free port), which is what makes this
    trivially testable — read the real port back from :attr:`bound_port` after :meth:`start`.
    """

    def __init__(
        self,
        *,
        registry: CollectorRegistry,
        health: HealthRegistry,
        host: str = "0.0.0.0",
        port: int = 0,
    ) -> None:
        self._registry = registry
        self._health = health
        self._host = host
        self._port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind the socket and start serving in a background daemon thread."""
        if self._httpd is not None:
            raise RuntimeError("ObservabilityServer is already started")
        handler_cls = _build_handler(self._registry, self._health)
        self._httpd = ThreadingHTTPServer((self._host, self._port), handler_cls)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="qlabs-observability-http",
            daemon=True,
        )
        self._thread.start()

    @property
    def bound_port(self) -> int:
        """The actual port in use — the resolved value when constructed with ``port=0``."""
        if self._httpd is None:
            raise RuntimeError("ObservabilityServer is not started")
        return int(self._httpd.server_address[1])

    def stop(self) -> None:
        """Stop serving and release the socket. Safe to call more than once."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None

    def __enter__(self) -> ObservabilityServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
