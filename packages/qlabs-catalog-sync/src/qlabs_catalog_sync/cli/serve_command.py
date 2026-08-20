"""``serve`` -- the long-running service, the shape the container actually runs.

WP9 / T9.1. ``run`` and ``dry-run`` execute one cycle per pair and exit, which is right for
an operator checking something or for a CI step. A deployment wants the other shape: one
process that stays up, fires each pair on its own cadence, and answers a health probe. Every
piece of that already existed and was tested --
:class:`~qlabs_catalog_sync.scheduler.SyncScheduler` (per-pair jobs, jitter,
``max_instances=1``, graceful shutdown) and
:class:`~qlabs_catalog_sync.observability.ObservabilityServer` (``/healthz`` and
``/metrics``) -- but nothing wired them together, so the container had no service to run.
This module is that wiring and nothing more.

Two behaviours worth knowing, because they are what make this safe in a container:

* **Shutdown waits for work in flight.** ``SIGTERM`` pauses the scheduler immediately, then
  gives a cycle already running its bounded chance to finish. The engine commits state in one
  transaction, so an abandoned cycle is safe either way -- but a cycle that finishes has
  already paid for its API budget, and throwing that away on every deploy is waste. Past the
  timeout it is abandoned rather than waited on forever.
* **Creation stays opt-in.** ``--create-missing`` exists here as it does on ``run``, and is off
  by default for the same reason: on a tenant that already holds data products, creating
  blindly duplicates every one of them. A service left running with it on would do that
  repeatedly, so it is a deliberate choice an operator makes, not a deployment default.
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import click

from qlabs_catalog_sync.observability import (
    HealthRegistry,
    ObservabilityServer,
    PrometheusMetrics,
    get_logger,
)
from qlabs_catalog_sync.scheduler import SyncScheduler

from .config_loading import load_engine_config, resolve_credentials
from .deps import RuntimeContext
from .wiring import (
    build_connector_pool,
    build_identity_resolver,
    build_state_store,
    build_sync_loop,
    select_pairs,
)

__all__ = ["serve"]

_LOG = get_logger("qlabs.catalog_sync.cli.serve")

#: How long shutdown waits for a cycle already in flight before abandoning it.
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 30.0


async def _serve(
    *,
    config_path: Path,
    runtime: RuntimeContext,
    pair_names: tuple[str, ...],
    create_missing: bool,
    host: str,
    port: int,
    shutdown_timeout: float,
    run_immediately: bool,
    stop: asyncio.Event | None = None,
) -> None:
    """Build every pair's loop, start the scheduler and the probe surface, then block.

    ``stop`` is the seam a test uses to end the service without sending a real signal; when
    it is ``None`` this installs ``SIGTERM``/``SIGINT`` handlers and waits on those instead.
    """
    engine_config = load_engine_config(config_path)
    pairs = select_pairs(engine_config, pair_names)
    credentials = resolve_credentials(engine_config)
    needed_endpoints = {pair.source for pair in pairs} | {pair.target for pair in pairs}

    metrics = PrometheusMetrics()
    health = HealthRegistry()
    store = await build_state_store(runtime.state_db)
    resolver = build_identity_resolver(store, runtime.review_path)
    pool = await build_connector_pool(
        config=engine_config,
        credentials=credentials,
        endpoint_keys=needed_endpoints,
        registry=runtime.connector_registry(),
        metrics=metrics,
    )

    observability = ObservabilityServer(registry=metrics.registry, health=health, host=host,
                                        port=port)
    scheduler = SyncScheduler(
        runners=[
            build_sync_loop(
                pair=pair,
                pool=pool,
                store=store,
                resolver=resolver,
                metrics=metrics,
                health=health,
                dry_run=False,
                create_missing=create_missing,
            )
            for pair in pairs
        ],
        health=health,
        run_immediately=run_immediately,
    )

    waiter = stop if stop is not None else asyncio.Event()
    if stop is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, waiter.set)

    observability.start()
    scheduler.start()
    _LOG.info(
        "serve.started",
        pairs=list(scheduler.pairs),
        observability_port=observability.bound_port,
        create_missing=create_missing,
    )
    try:
        await waiter.wait()
    finally:
        _LOG.info("serve.stopping")
        await scheduler.shutdown(timeout=shutdown_timeout)
        observability.stop()
        await pool.close()
        _LOG.info("serve.stopped")


@click.command("serve")
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Engine config file (YAML or JSON) -- endpoints and sync pairs.",
)
@click.option(
    "--pair",
    "pair_names",
    multiple=True,
    help="Restrict to this sync pair (repeatable). Default: every configured pair.",
)
@click.option(
    "--create-missing/--no-create-missing",
    default=False,
    show_default=True,
    help=(
        "Create a target object when a source object has no confirmed identity binding. "
        "Off by default -- a service running with this on re-creates on every cycle it "
        "cannot bind. Bootstrap identity with `identity-confirm` instead."
    ),
)
@click.option("--host", default="0.0.0.0", show_default=True,
              help="Interface for /healthz and /metrics.")
@click.option("--port", default=8080, show_default=True, type=int,
              help="Port for /healthz and /metrics.")
@click.option(
    "--shutdown-timeout",
    default=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    show_default=True,
    type=float,
    help="Seconds to let a cycle already in flight finish after SIGTERM before abandoning it.",
)
@click.option(
    "--run-immediately/--no-run-immediately",
    default=False,
    show_default=True,
    help="Fire every pair once at startup instead of waiting a full cadence.",
)
@click.pass_obj
def serve(
    runtime: RuntimeContext,
    config_path: Path,
    pair_names: tuple[str, ...],
    create_missing: bool,
    host: str,
    port: int,
    shutdown_timeout: float,
    run_immediately: bool,
) -> None:
    """Run the sync service: one process, one job per pair, until SIGTERM."""
    asyncio.run(
        _serve(
            config_path=config_path,
            runtime=runtime,
            pair_names=pair_names,
            create_missing=create_missing,
            host=host,
            port=port,
            shutdown_timeout=shutdown_timeout,
            run_immediately=run_immediately,
        )
    )
