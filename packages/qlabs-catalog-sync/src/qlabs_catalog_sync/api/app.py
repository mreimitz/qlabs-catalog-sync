"""The FastAPI application: the process's single HTTP surface (C8).

WP12 / T12.1. Three things land in this module and nowhere else:

* ``/healthz`` and ``/metrics`` -- unprefixed, exactly where T2.7 put them, rendered by
  calling :func:`~qlabs_catalog_sync.observability.render_healthz` /
  :func:`~qlabs_catalog_sync.observability.render_metrics` directly and writing their
  return value straight into the response, byte for byte. This module never
  re-implements health/metrics rendering -- ``tests/api/test_healthz.py`` and
  ``tests/api/test_metrics.py`` are parity tests proving the two surfaces agree exactly,
  including status code and content type, for both a healthy and a degraded registry.
* :data:`API_PREFIX` -- every REST route WP12 adds from T12.3 onward mounts under this
  prefix (``app.include_router(router, prefix=API_PREFIX)``), so the static mount
  (``static.py``) knows which unmatched paths must return the JSON error model instead
  of the SPA shell.
* :func:`create_app` -- a factory, not a module-level singleton. A module-level app
  reading global state (a real ``HealthRegistry``, a real Prometheus
  ``CollectorRegistry``, a real ``ConfigService``...) would be untestable and would fight
  T12.2's auth, which needs to apply to every route without each one remembering to opt
  in. Tests, and the eventual ``serve`` wiring, each construct their own instance with
  explicit dependencies.

**The seam this task runs into -- read this before wiring anything up.** ``/healthz`` and
``/metrics`` already exist today as a second, independent HTTP surface:
:class:`~qlabs_catalog_sync.observability.ObservabilityServer`, a stdlib
``http.server.ThreadingHTTPServer`` on a background daemon thread, started by
``cli/serve_command.py`` (T9.1) -- deliberately not an ASGI framework, because RM-01 was
built as a worker process, not an API (see that module's own docstring). Decision C8
requires one process, one origin, one port for the API, the console and those two
endpoints together, which ``ObservabilityServer`` alone cannot provide once the API and
console exist alongside it.

This module only proves this app *can* be that single surface -- by reusing the exact
``render_healthz``/``render_metrics`` functions ``ObservabilityServer`` already calls,
with byte-for-byte parity tests -- so that whoever wires ``serve_command.py`` (not this
task; see T12.1's own report for the precise change) can make ``create_app`` the one
thing listening on the configured port, and either stop starting
``ObservabilityServer`` at all or leave it in place for a transition. Neither
``serve_command.py`` nor ``observability.py`` is touched here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry

from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.observability import HealthRegistry, render_healthz, render_metrics

from .auth import ConsoleAuth, install_auth
from .errors import API_ERROR_RESPONSES, install_error_handlers
from .routes import build_connectors_router, build_endpoints_router
from .static import mount_static

__all__ = ["API_PREFIX", "create_app"]

#: Prefix every REST API route (T12.3 onward) mounts under. Never applied to
#: ``/healthz``/``/metrics`` (C8: those stay exactly where T2.7 put them, unprefixed) nor
#: to the console's static assets (served at ``/`` -- see ``static.py``). Exported so
#: later tasks mount consistently, and so the SPA fallback knows which unmatched paths
#: must return the JSON error model instead of ``index.html``.
API_PREFIX: Final[str] = "/api"


def create_app(
    *,
    health: HealthRegistry,
    metrics_registry: CollectorRegistry,
    static_dir: Path | None = None,
    auth: ConsoleAuth | None = None,
    config_service: ConfigService | None = None,
    registry: ConnectorRegistry | None = None,
    title: str = "QLabs Catalog Sync API",
    version: str = "0.1.0",
) -> FastAPI:
    """Build one FastAPI application instance.

    ``health``/``metrics_registry`` are required -- process startup always has a real one
    of each (T2.7) -- and rendered through this app exactly as ``ObservabilityServer``
    renders them today (see the module docstring). ``static_dir`` is optional: the
    console (WP13) does not exist yet, and per this task's DoD the app must boot and keep
    serving ``/healthz``, ``/metrics`` and the API regardless -- ``static.py`` is what
    stays honest about the console's absence at ``/`` rather than 500ing or returning a
    confusing bare 404.

    No configuration store is required to construct or serve this app. ``auth`` (T12.2,
    C7) is a :class:`~qlabs_catalog_sync.api.auth.ConsoleAuth` and is what makes the whole
    app require an administrator session: :func:`~qlabs_catalog_sync.api.auth.install_auth`
    adds one ASGI middleware covering every route, present and future, plus the sign-in
    routes -- exactly the whole-app shape this factory was built for, never a per-route
    opt-in. A **deployment** obtains it from
    :func:`~qlabs_catalog_sync.api.auth.console_auth_from_environment`, which refuses to
    return one when no credential is configured, so the process cannot start unauthenticated
    (C7). Passing ``auth=None`` here builds an app with no authentication and logs a warning
    saying so; that is for tests of unrelated parts of this API, not for serving.

    ``config_service``/``registry`` (T12.3, C6) are what the ``/connectors`` and
    ``/endpoints`` routes need: a real
    :class:`~qlabs_catalog_sync.configstore.service.ConfigService` and the
    :class:`~qlabs_catalog_sync.discovery.ConnectorRegistry` the process discovered at
    startup. They are required together -- those routes mount only when both are given;
    either left at the default ``None`` (every test of an unrelated part of this API, and
    T12.1's own DoD that the app boots with no configuration store) leaves them
    unmounted, exactly like ``static_dir=None`` leaves the console unmounted.
    """
    app = FastAPI(title=title, version=version)

    install_error_handlers(app)
    install_auth(app, auth=auth, api_prefix=API_PREFIX)

    @app.get("/healthz", responses=API_ERROR_RESPONSES)
    async def _healthz() -> Response:
        """Byte-identical to ``ObservabilityServer``'s ``/healthz`` (T2.7): same status
        code, same JSON body, same content type -- rendered by the same function."""
        status_code, body = render_healthz(health)
        return Response(content=body, status_code=status_code, media_type="application/json")

    @app.get("/metrics", responses=API_ERROR_RESPONSES)
    async def _metrics() -> Response:
        """Byte-identical to ``ObservabilityServer``'s ``/metrics`` (T2.7): the same
        Prometheus text-exposition body, rendered by the same function."""
        body = render_metrics(metrics_registry)
        return Response(content=body, status_code=200, media_type=CONTENT_TYPE_LATEST)

    # T12.3's connector/endpoint routes, after /healthz and /metrics, before
    # mount_static. Route resolution in Starlette is insertion order, and
    # mount_static's fallback matches every unmatched GET path, so it must always be
    # registered last (see static.py's module docstring). Later WP12 tasks add their
    # own app.include_router(router, prefix=API_PREFIX) calls the same way.
    if config_service is not None and registry is not None:
        app.include_router(build_connectors_router(registry), prefix=API_PREFIX)
        app.include_router(build_endpoints_router(config_service, registry), prefix=API_PREFIX)

    mount_static(app, static_dir=static_dir, api_prefix=API_PREFIX)

    return app
