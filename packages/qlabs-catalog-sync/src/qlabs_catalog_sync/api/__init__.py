"""The REST API: a FastAPI application mounted alongside ``/healthz``/``/metrics``
(WP12 / T12.1), the shared error model every later route in this work package builds on,
and the static-asset mount the console (WP13) is built into.

See ``app.py`` for :func:`create_app` and :data:`API_PREFIX`, ``errors.py`` for the
shared error shape and exception-handler wiring, and ``static.py`` for the SPA history
fallback.
"""

from __future__ import annotations

from .app import API_PREFIX, create_app
from .errors import API_ERROR_RESPONSES, APIError, ErrorModel, install_error_handlers
from .static import mount_static, path_is_under_api_prefix

__all__ = [
    "API_ERROR_RESPONSES",
    "API_PREFIX",
    "APIError",
    "ErrorModel",
    "create_app",
    "install_error_handlers",
    "mount_static",
    "path_is_under_api_prefix",
]
