"""Running the API app as the process's one HTTP surface (C8).

Orchestrator wiring, not a board task. T12.1 built :func:`~.app.create_app` and proved it
*can* serve ``/healthz`` and ``/metrics`` byte-identically to
:class:`~qlabs_catalog_sync.observability.ObservabilityServer`, but deliberately stopped
short of starting it: ``cli/serve_command.py`` is outside every RM-06 task's owned paths,
so the line joining "an app exists" to "the service serves it" belonged to nobody. This
module is that line.

Why it replaces the stdlib probe server
---------------------------------------

Decision C8: *"The SPA is built to static assets and served by the same process that
exposes the REST API, ``/healthz`` and ``/metrics`` — one artifact, one origin, one
version, no CORS and no possibility of the console drifting from the engine it
configures."* WP14's container check curls ``/healthz`` **and** ``/`` on the same port, so
two listeners is not an option that later passes.

:class:`~qlabs_catalog_sync.observability.ObservabilityServer` (T2.7) is a stdlib
``ThreadingHTTPServer`` on a background thread. That was the right shape for a worker
process with no API, and the wrong one once a REST API and a browser console have to share
the origin. It is kept — it is still the reference the API's parity tests describe
themselves against, and it is still independently tested — but the service no longer starts
it. See ``cli/serve_command.py``.

Why a wrapper rather than ``uvicorn.run``
-----------------------------------------

``uvicorn.run`` owns the event loop and installs its own signal handlers. ``serve`` already
owns both: it runs inside an established asyncio loop and installs ``SIGTERM``/``SIGINT``
handlers that set an :class:`asyncio.Event`, because shutdown has to let a sync cycle
already in flight finish rather than throw away API budget already spent. So this runs
``uvicorn.Server.serve()`` as a task on the *existing* loop with signal handling
suppressed, and exposes the same ``start()``/``stop()``/``bound_port`` shape
``ObservabilityServer`` had — which is what keeps the change in ``serve_command.py`` to
swapping one object for another.

``bound_port`` matters for the same reason it did there: the service binds port ``0`` in
tests to get a free port from the OS, and the probe has to be able to find it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import uvicorn
from fastapi import FastAPI

__all__ = ["ApiServer"]

#: How long :meth:`ApiServer.start` waits for uvicorn to report a bound socket before
#: giving up. Startup is a socket bind on an already-running loop, so this is generous by
#: orders of magnitude; it exists so a failure to bind surfaces as a clear error instead of
#: hanging the service forever.
_STARTUP_TIMEOUT_SECONDS = 30.0
_STARTUP_POLL_SECONDS = 0.01


class _NoSignalHandlerServer(uvicorn.Server):
    """A ``uvicorn.Server`` that leaves signal handling to the caller.

    ``serve`` installs its own ``SIGTERM``/``SIGINT`` handlers so that a shutdown drains a
    cycle in flight. Uvicorn's defaults would replace them and tear the loop down
    underneath the scheduler instead.
    """

    def install_signal_handlers(self) -> None:
        return None


class ApiServer:
    """Serves one FastAPI application on the engine's configured host and port.

    Mirrors :class:`~qlabs_catalog_sync.observability.ObservabilityServer`'s lifecycle
    surface on purpose — ``start()``, ``stop()``, ``bound_port`` — so the service's startup
    reads the same way it did before, with one listener instead of two.
    """

    def __init__(self, app: FastAPI, *, host: str = "0.0.0.0", port: int = 0) -> None:
        self._app = app
        self._host = host
        self._port = port
        self._server: _NoSignalHandlerServer | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Bind the socket and begin serving, returning once the port is actually bound.

        Returning only after the bind is what makes ``bound_port`` meaningful and what
        stops a caller (or a container healthcheck) from racing the listener.
        """
        if self._server is not None:
            raise RuntimeError("ApiServer is already started")
        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            # The engine configures structlog itself (T2.7); letting uvicorn install its
            # own logging would produce a second, differently-shaped log stream on stdout.
            log_config=None,
            access_log=False,
        )
        server = _NoSignalHandlerServer(config)
        self._server = server
        self._task = asyncio.create_task(server.serve(), name="qlabs-api-http")

        waited = 0.0
        while not server.started:
            if self._task.done():  # startup failed - surface the real error, not a timeout
                await self._task
                raise RuntimeError("API server exited during startup")
            if waited >= _STARTUP_TIMEOUT_SECONDS:
                raise RuntimeError(
                    f"API server did not bind {self._host}:{self._port} within "
                    f"{_STARTUP_TIMEOUT_SECONDS:g}s"
                )
            await asyncio.sleep(_STARTUP_POLL_SECONDS)
            waited += _STARTUP_POLL_SECONDS

    @property
    def bound_port(self) -> int:
        """The port actually bound, which differs from ``port`` when ``port=0`` was asked for."""
        server = self._server
        if server is None or not server.started:
            raise RuntimeError("ApiServer is not started")
        sockets: list[Any] = list(server.servers[0].sockets)
        port = sockets[0].getsockname()[1]
        return int(port)

    async def stop(self) -> None:
        """Stop serving and wait for the server task to finish. Idempotent."""
        server, task = self._server, self._task
        self._server, self._task = None, None
        if server is None or task is None:
            return
        server.should_exit = True
        await task
