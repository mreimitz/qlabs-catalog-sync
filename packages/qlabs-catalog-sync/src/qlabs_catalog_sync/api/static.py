"""The console's static-asset mount and SPA history fallback (C8, WP13's build target).

WP13 (the console SPA) does not exist yet -- ``static_dir`` is ``None`` in every
environment until T14.1 builds it and copies its ``dist/`` output into the container.
This module's whole job is staying honest about that (T12.1's DoD: "the app boots with
no static assets present") while also being correct once assets do exist:

* **The API prefix wins first, always.** :func:`mount_static` must be called *last* in
  ``app.py`` -- after ``/healthz``, ``/metrics`` and every future WP12 route -- because
  its fallback route matches literally every unmatched ``GET`` path
  (``"/{full_path:path}"``). Route resolution in Starlette is insertion order, not
  specificity: registered first, this route would swallow every other route including
  the API. The one thing this module refuses to get backwards regardless: a path under
  ``api_prefix`` never receives ``index.html`` -- it always raises
  :class:`~qlabs_catalog_sync.api.errors.APIError`, decided *before* the filesystem is
  even touched. See ``tests/api/test_static_spa_fallback.py`` for the test that fails if
  this ever gets swapped -- the console would receive HTML where it expected JSON and
  fail with a parse error that points nowhere.
* **A real file on disk wins over the SPA shell.** A request for e.g.
  ``/assets/app-abc123.js`` (or any other file Vite's build actually produced, wherever
  in ``static_dir`` it landed) is served as itself, with its content type guessed from
  the extension; only a path that does not resolve to a real file falls back to
  ``index.html`` -- that is what makes client-side routing work (``/endpoints/42`` has no
  matching file on disk, so the SPA shell loads and its own router takes over) without
  breaking a plain asset request.
* **No path traversal.** ``full_path`` is attacker-controlled input threaded straight
  into a filesystem path; every candidate is resolved and checked to still be inside
  ``static_dir`` before it is ever served.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from .errors import APIError

__all__ = ["mount_static", "path_is_under_api_prefix"]


def path_is_under_api_prefix(path: str, *, api_prefix: str) -> bool:
    """``True`` when ``path`` (``request.url.path``, always ``/``-prefixed) falls under
    ``api_prefix`` and must therefore never receive the SPA shell -- only the JSON error
    model, whatever the reason for the miss."""
    return path == api_prefix or path.startswith(f"{api_prefix}/")


def mount_static(app: FastAPI, *, static_dir: Path | None, api_prefix: str) -> None:
    """Register the SPA history-fallback route on ``app``.

    Call this **last** from ``create_app`` -- see the module docstring for why order is
    load-bearing here. ``static_dir`` is the directory holding the console's built
    ``index.html`` (Vite's ``dist/`` output, or ``None`` before WP13/T14.1 exist); this
    function never raises for a missing or absent directory -- the fallback route reports
    that at request time instead, so app startup never depends on the console having been
    built.
    """
    resolved_root = static_dir.resolve() if static_dir is not None else None

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str, request: Request) -> FileResponse:
        request_path = request.url.path

        if path_is_under_api_prefix(request_path, api_prefix=api_prefix):
            raise APIError(
                status_code=404,
                code="not_found",
                message=f"no API route for {request_path!r}",
            )

        if resolved_root is None:
            raise APIError(
                status_code=404,
                code="console_not_installed",
                message=(
                    "the console has not been built into this image yet (see RM-06 "
                    "WP13/T14.1)"
                ),
            )

        index_path = resolved_root / "index.html"
        if not index_path.is_file():
            raise APIError(
                status_code=404,
                code="console_not_installed",
                message=(
                    "the configured console asset directory has no index.html (see "
                    "RM-06 WP13/T14.1)"
                ),
            )

        candidate = (resolved_root / full_path.lstrip("/")).resolve()
        if candidate.is_relative_to(resolved_root) and candidate.is_file():
            return FileResponse(candidate)

        return FileResponse(index_path)
