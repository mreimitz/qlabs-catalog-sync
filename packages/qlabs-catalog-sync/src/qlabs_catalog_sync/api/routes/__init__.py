"""REST API routes (WP12, T12.3 onward), each mounted under ``api.app.API_PREFIX``.

One module per resource family; each exposes a ``build_<name>_router(...) -> APIRouter``
factory taking its dependencies explicitly -- mirrors ``api.auth._build_auth_router`` --
so :func:`~qlabs_catalog_sync.api.app.create_app` builds every router once, from the real
dependencies it was given, rather than any router reading module-level or global state.
"""

from __future__ import annotations

from .connectors import build_connectors_router
from .endpoints import build_endpoints_router

__all__ = ["build_connectors_router", "build_endpoints_router"]
