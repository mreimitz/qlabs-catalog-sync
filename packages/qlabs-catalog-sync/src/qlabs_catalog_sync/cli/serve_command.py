"""``serve`` -- the long-running service, the shape the container actually runs.

WP9 / T9.1. ``run`` and ``dry-run`` execute one cycle per pair and exit, which is right for
an operator checking something or for a CI step. A deployment wants the other shape: one
process that stays up, fires each pair on its own cadence, and answers a health probe. Every
piece of that already existed and was tested --
:class:`~qlabs_catalog_sync.scheduler.SyncScheduler` (per-pair jobs, jitter,
``max_instances=1``, graceful shutdown) and the ``/healthz``/``/metrics`` surface -- but
nothing wired them together, so the container had no service to run. This module is that
wiring and nothing more.

**One origin (C8).** The HTTP surface is now the FastAPI application (WP12/T12.1) run by
:class:`~qlabs_catalog_sync.api.server.ApiServer`: the REST API, the console's static
assets, ``/healthz`` and ``/metrics`` all answer on one port, so the console cannot drift
from the engine it configures and there is no CORS to configure.
:class:`~qlabs_catalog_sync.observability.ObservabilityServer` served the last two on their
own stdlib thread and is no longer started here; ``/healthz`` and ``/metrics`` are rendered
by the same ``render_healthz``/``render_metrics`` functions as before, byte for byte.

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
from datetime import UTC, datetime
from pathlib import Path

import click

from qlabs_catalog_sync.api.app import create_app
from qlabs_catalog_sync.api.auth import console_auth_from_environment
from qlabs_catalog_sync.api.server import ApiServer
from qlabs_catalog_sync.configstore.bootstrap import (
    BootstrapPartialFailureError,
    bootstrap_from_environment,
)
from qlabs_catalog_sync.configstore.runtime import (
    StoreConnectorPool,
)
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.observability import (
    HealthRegistry,
    PrometheusMetrics,
    get_logger,
)
from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.scheduler import (
    ConfigStorePairSource,
    PairPlan,
    PairRunner,
    SyncScheduler,
)

from .config_loading import load_engine_config, resolve_credentials
from .deps import RuntimeContext
from .wiring import (
    ConnectorPool,
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
    console_assets: Path | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Build every pair's loop, start the scheduler and the probe surface, then block.

    ``stop`` is the seam a test uses to end the service without sending a real signal; when
    it is ``None`` this installs ``SIGTERM``/``SIGINT`` handlers and waits on those instead.
    """
    engine_config = load_engine_config(config_path)
    # Unlike `run`/`dry-run`, a config file declaring no pairs is legitimate here: since
    # C1 the store is authoritative and a console-first deployment declares its pairs in
    # the browser, not in YAML. `serve` with nothing configured anywhere is a service
    # waiting to be configured, which is exactly what the console is for -- refusing to
    # start would mean the operator could never reach the console to configure it.
    pairs = select_pairs(engine_config, pair_names) if engine_config.pairs else []
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

    # One listener, not two (C8): the FastAPI app serves the REST API, the console's
    # static assets, /healthz and /metrics on a single port. ObservabilityServer served
    # the last two on their own stdlib thread, which cannot share an origin with an API
    # and a browser console -- see api/server.py for the full reasoning.
    # Fail closed (C7). console_auth_from_environment RAISES when no administrator
    # credential is configured, so the service cannot come up serving an unauthenticated
    # console. Deliberately built before anything binds a socket: refusing to start is a
    # clear crash with the reason in the logs, whereas a process that boots and serves only
    # /healthz keeps passing its liveness probe forever while the console is unusable.
    auth = console_auth_from_environment()

    # The configuration store the console reads and writes (C1). It shares the state
    # store's engine -- one database, one connection pool -- and the same connector
    # registry the pool was built from, so a route validating an endpoint's settings sees
    # exactly the connectors this image actually has (C6).
    config_service = ConfigService(store.engine, runtime.connector_registry())

    # Seed it from the environment-declared config on first start, then never again: from
    # then on the database is authoritative and an operator's console edits stick (C1).
    # A partial import is reported and does not stop the service - the console is how the
    # remainder gets fixed, so refusing to start would remove the only tool for the job.
    try:
        bootstrap = await bootstrap_from_environment(
            config_service, engine_config, now=datetime.now(UTC)
        )
        if bootstrap.seeded:
            _LOG.info(
                "serve.config_store.seeded",
                endpoints=bootstrap.endpoints_created,
                pairs=bootstrap.pairs_created,
                rules=bootstrap.rules_created,
            )
    except BootstrapPartialFailureError as exc:
        _LOG.warning(
            "serve.config_store.seeded_partially",
            failures=[str(failure) for failure in exc.report.failures],
            secret_ref_skips=[str(skip) for skip in exc.report.secret_ref_skips],
        )

    # Run history (T11.4). Built here rather than inside the scheduler so a deployment
    # without it degrades to "no reporting", never to "no sync".
    recorder = RunRecorder.from_store(store)

    api = ApiServer(
        create_app(
            health=health,
            metrics_registry=metrics.registry,
            static_dir=console_assets,
            auth=auth,
            config_service=config_service,
            registry=runtime.connector_registry(),
            store=store,
            resolver=resolver,
            recorder=recorder,
            metrics=metrics,
        ),
        host=host,
        port=port,
    )
    # Connectors for endpoints that exist only in the configuration store - the ones an
    # operator registered in the console (C6). The startup pool above can only build what
    # the YAML config named, so without this a console-registered endpoint could never
    # sync. Built lazily and rebuilt when an endpoint's settings or secret_ref change.
    store_pool = StoreConnectorPool(config_service, runtime.connector_registry(), metrics=metrics)

    async def _build_runner(plan: PairPlan) -> PairRunner:
        """Build one pair's runner from its stored configuration (C1).

        Called by reconcile whenever a pair is added or its configuration changes. The
        pair's selection rules come from the store and are handed to the loop through
        T11.3's ``selection_rules`` seam, which is what connects C1 to C3/C4: a rule
        edited in the console decides the next cycle's scope.
        """
        source = await store_pool.get(plan.pair.source)
        target = await store_pool.get(plan.pair.target)
        return build_sync_loop(
            pair=plan.pair,
            pool=ConnectorPool(connectors={plan.pair.source: source, plan.pair.target: target}),
            store=store,
            resolver=resolver,
            metrics=metrics,
            health=health,
            dry_run=False,
            create_missing=create_missing,
            selection_rules=plan.selection_rules,
        )

    scheduler = SyncScheduler(
        recorder=recorder,
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
        # restrict_to matters: without it, `serve --pair X` would see every other pair
        # reappear at the first reconcile.
        config_source=ConfigStorePairSource(config_service, restrict_to=pair_names or None),
        runner_factory=_build_runner,
    )

    waiter = stop if stop is not None else asyncio.Event()
    if stop is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, waiter.set)

    # A process killed mid-cycle leaves its run row at RUNNING; nothing else can close
    # those out, so the next start does it before scheduling anything new.
    reaped = await recorder.reap_stale(now=datetime.now(UTC))
    if reaped:
        _LOG.info("serve.run_history.reaped_stale", count=len(reaped))

    await api.start()
    scheduler.start()
    _LOG.info(
        "serve.started",
        pairs=list(scheduler.pairs),
        http_port=api.bound_port,
        console_assets=str(console_assets) if console_assets is not None else None,
        create_missing=create_missing,
    )
    try:
        await waiter.wait()
    finally:
        _LOG.info("serve.stopping")
        await scheduler.shutdown(timeout=shutdown_timeout)
        await api.stop()
        await store_pool.close()
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
@click.option(
    "--host",
    default="0.0.0.0",
    show_default=True,
    help="Interface for the API, the console, /healthz and /metrics.",
)
@click.option(
    "--port",
    default=8080,
    show_default=True,
    type=int,
    help="Port for the API, the console, /healthz and /metrics -- one origin (C8).",
)
@click.option(
    "--console-assets",
    "console_assets",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help=(
        "Directory of built console assets to serve at /. Omit to run headless -- the API, "
        "/healthz and /metrics still serve, and / explains that the console is not installed."
    ),
)
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
    console_assets: Path | None,
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
            console_assets=console_assets,
        )
    )
