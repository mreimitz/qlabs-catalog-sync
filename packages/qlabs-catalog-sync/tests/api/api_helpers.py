"""Shared helpers for the T12.1 API test suite: building a minimal app the same way the
DoD requires it to boot (no configuration store, no console assets, no auth), a way to
register a throwaway route that raises a given exception (so the error-model mapping in
``api/errors.py`` can be proven without any real business route to trigger it -- those
land with T12.3 onward), and a tiny built-console tree builder for the SPA-fallback
tests.

Named ``api_helpers`` rather than ``helpers`` -- this repo collects tests with pytest's
``importlib`` import mode over one flat module namespace, and that basename has already
collided twice (see e.g. ``tests/configstore/config_service_helpers.py``'s own
docstring).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from prometheus_client import CollectorRegistry

from qlabs_catalog_sync.api.app import create_app
from qlabs_catalog_sync.observability import HealthRegistry

__all__ = ["add_raising_route", "build_app", "write_console_dist"]


def build_app(*, static_dir: Path | None = None) -> FastAPI:
    """Exactly what T12.1's DoD requires the app boot with: no configuration store, no
    console assets (unless ``static_dir`` is given), no auth."""
    return create_app(
        health=HealthRegistry(), metrics_registry=CollectorRegistry(), static_dir=static_dir
    )


def add_raising_route(app: FastAPI, path: str, make_exc: Callable[[], BaseException]) -> None:
    """Register a throwaway ``GET`` route at ``path`` that always raises ``make_exc()``.

    Exists so this suite can prove the exception -> :class:`~qlabs_catalog_sync.api.
    errors.ErrorModel` mapping for every ``ConfigService``/discovery exception without a
    real business route to trigger them -- those land with T12.3 onward.
    ``include_in_schema=False`` keeps these diagnostic-only routes out of the OpenAPI
    schema the client-generation test reads.

    ``create_app`` always registers the SPA catch-all (``"/{full_path:path}"``) *last*
    (``static.py``'s module docstring explains why: Starlette matches routes in
    registration order, not by specificity, and that route matches every unmatched GET
    path). ``app.add_api_route`` appends, which would land this route *after* the
    catch-all and make it unreachable -- so this moves it to just before instead. Real
    WP12 routes never need this: they are added inside ``create_app`` itself, before
    ``mount_static`` runs.
    """

    async def _raise() -> None:
        raise make_exc()

    app.add_api_route(path, _raise, methods=["GET"], include_in_schema=False)
    routes = app.router.routes
    new_route = routes.pop()
    routes.insert(len(routes) - 1, new_route)


def write_console_dist(tmp_path: Path, *, index_html: str = "<html>console shell</html>") -> Path:
    """A minimal built-console directory: ``index.html`` plus one hashed asset file --
    the same shape Vite's ``dist/`` output takes (T13.1/T14.1)."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(index_html, encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log('hi');\n", encoding="utf-8")
    return dist
