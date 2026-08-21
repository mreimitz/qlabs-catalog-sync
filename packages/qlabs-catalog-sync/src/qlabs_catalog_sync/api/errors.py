"""The shared error model and the exception-handler wiring every API route relies on.

WP12 / T12.1. One JSON shape -- :class:`ErrorModel` -- for every failure this API can
produce, and one place, :func:`install_error_handlers`, that turns a raised exception
into it. A route (T12.3 onward) never constructs a status code or an error body itself:
it calls :class:`~qlabs_catalog_sync.configstore.service.ConfigService` (or another
domain layer) and lets its typed exceptions propagate. That is deliberate -- it is what
keeps every future route agreeing with every other one about what a given failure looks
like on the wire, and it is what T12.8's generated TypeScript client needs: an error
shape that is *declared* in ``openapi.json`` (see :data:`API_ERROR_RESPONSES`), not just
produced at runtime.

Three tiers of mapping, in order of specificity (Starlette's exception-handler lookup
walks the raised exception's MRO and dispatches to the most specific registered class, so
all three coexist without conflict):

1. **Named domain exceptions** -- every ``ConfigServiceError`` subclass
   (``configstore/service.py``, T10.3), the connector-discovery lookup errors
   (``discovery.py``), and ``SecretRefFormatError`` (``configstore/secrets.py``, T10.2)
   each get their own handler below, with a stable ``code``, the right HTTP status, and
   ``field``/``entity`` filled in from whatever the exception itself carries.
2. **Base-class safety nets** -- ``ConfigServiceError`` and ``ConnectorLookupError``
   themselves are also registered, mapped to a generic-but-honest 422. A future error
   type added to either hierarchy without its own handler still gets a deliberate
   4xx, never the generic 500 below -- it *is* a domain validation failure, the author
   just has not named it yet.
3. **Framework-wide fallbacks** -- ``RequestValidationError`` (a bad request body/query)
   and ``HTTPException`` (an explicit ``raise`` from route code, or Starlette's own
   "no route matched") are redirected into the same shape rather than FastAPI's default
   bodies, and a bare ``Exception`` -- anything nobody anticipated -- becomes a generic
   500 with a correlation id. Its *message* goes only to the structured log, keyed by
   that same id; the response body never repeats it. See
   ``tests/api/test_generic_500_never_leaks.py`` for the test that fails if this ever
   regresses.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import FastAPI, status
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from qlabs_catalog_sync.configstore.secrets import SecretRefFormatError
from qlabs_catalog_sync.configstore.service import (
    ConfigServiceError,
    EndpointAlreadyExistsError,
    EndpointInUseError,
    EndpointNotFoundError,
    EndpointSettingsValidationError,
    InlineSecretRejectedError,
    SelectionOverrideAlreadyExistsError,
    SelectionOverrideNotFoundError,
    SelectionRuleNotFoundError,
    SelectionRuleOrdinalConflictError,
    SelectionRuleReorderMismatchError,
    SyncPairAlreadyExistsError,
    SyncPairEndpointError,
    SyncPairNotFoundError,
)
from qlabs_catalog_sync.discovery import (
    ConnectorBrokenError,
    ConnectorLookupError,
    ConnectorNotRegisteredError,
)
from qlabs_catalog_sync.observability import get_logger

_LOG = get_logger("qlabs.catalog_sync.api.errors")

__all__ = [
    "API_ERROR_RESPONSES",
    "APIError",
    "ErrorModel",
    "install_error_handlers",
]


class ErrorModel(BaseModel):
    """The one JSON error shape every failure this API can produce returns.

    ``code`` is the stable, machine-readable identifier the console (and T12.8's
    generated TypeScript client) switches on -- it never changes shape or meaning once
    shipped, even if ``message`` is reworded. ``message`` is a human-readable
    explanation: never a stack trace, never a secret (see the module docstring's tier 3).
    ``field``/``entity`` are filled in only when the failure can name which request field
    or which domain entity was at fault; both are ``None`` otherwise, never an empty
    string. ``correlation_id`` is set only for an unexpected (500) failure -- the one
    case where the client cannot act on ``message`` alone and instead needs something to
    hand to an operator, who can grep the structured logs for it.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    field: str | None = None
    entity: str | None = None
    correlation_id: str | None = None


#: Attach to every route's ``responses=`` so the generated OpenAPI schema documents this
#: contract even though the body actually comes from a global exception handler, never
#: from the route function itself. ``"default"`` is the OpenAPI key for "every status
#: code not otherwise listed" -- the honest description of what is true here: any route
#: can fail in a way :func:`install_error_handlers` catches, not just the status codes it
#: succeeds with.
API_ERROR_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    "default": {
        "model": ErrorModel,
        "description": "Every error this API returns shares this shape.",
    },
}


class APIError(Exception):
    """Raise directly -- rather than a domain exception -- when a route or mount needs a
    specific status/code pair that has no corresponding typed domain exception. The
    static mount's "not under the API prefix" 404 (``static.py``) is the only place this
    task itself raises one; later routes are free to use it for the same reason.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        field: str | None = None,
        entity: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field = field
        self.entity = entity

    def to_model(self) -> ErrorModel:
        return ErrorModel(
            code=self.code, message=self.message, field=self.field, entity=self.entity
        )


def _respond(status_code: int, model: ErrorModel) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=model.model_dump(mode="json"))


#: ``code`` for a plain ``HTTPException``/unmatched-route status when nothing more
#: specific applies -- e.g. Starlette's own "no route matched" 404 for a non-GET request
#: (the SPA fallback in ``static.py`` claims every unmatched *GET* path itself, so in
#: practice this is reached only for other methods).
_CODE_BY_HTTP_STATUS: Final[dict[int, str]] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "unprocessable_entity",
}


def install_error_handlers(app: FastAPI) -> None:
    """Register every exception -> :class:`ErrorModel` mapping this API promises, once,
    on ``app``. See the module docstring for the three tiers this builds."""

    @app.exception_handler(APIError)
    async def _handle_api_error(request: Request, exc: APIError) -> JSONResponse:
        return _respond(exc.status_code, exc.to_model())

    # ---- ConfigService (T10.3) typed errors --------------------------------------------

    @app.exception_handler(EndpointNotFoundError)
    async def _handle_endpoint_not_found(
        request: Request, exc: EndpointNotFoundError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_404_NOT_FOUND,
            ErrorModel(code="endpoint_not_found", message=str(exc), entity=exc.name),
        )

    @app.exception_handler(EndpointAlreadyExistsError)
    async def _handle_endpoint_already_exists(
        request: Request, exc: EndpointAlreadyExistsError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_409_CONFLICT,
            ErrorModel(code="endpoint_already_exists", message=str(exc), entity=exc.name),
        )

    @app.exception_handler(EndpointInUseError)
    async def _handle_endpoint_in_use(request: Request, exc: EndpointInUseError) -> JSONResponse:
        return _respond(
            status.HTTP_409_CONFLICT,
            ErrorModel(code="endpoint_in_use", message=str(exc), entity=exc.name),
        )

    @app.exception_handler(InlineSecretRejectedError)
    async def _handle_inline_secret_rejected(
        request: Request, exc: InlineSecretRejectedError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorModel(
                code="inline_secret_rejected",
                message=str(exc),
                field=", ".join(exc.fields),
                entity=exc.connector,
            ),
        )

    @app.exception_handler(EndpointSettingsValidationError)
    async def _handle_endpoint_settings_invalid(
        request: Request, exc: EndpointSettingsValidationError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorModel(code="endpoint_settings_invalid", message=str(exc), entity=exc.connector),
        )

    @app.exception_handler(SyncPairNotFoundError)
    async def _handle_sync_pair_not_found(
        request: Request, exc: SyncPairNotFoundError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_404_NOT_FOUND,
            ErrorModel(code="sync_pair_not_found", message=str(exc), entity=str(exc.pair_id)),
        )

    @app.exception_handler(SyncPairAlreadyExistsError)
    async def _handle_sync_pair_already_exists(
        request: Request, exc: SyncPairAlreadyExistsError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_409_CONFLICT,
            ErrorModel(code="sync_pair_already_exists", message=str(exc), entity=exc.name),
        )

    @app.exception_handler(SyncPairEndpointError)
    async def _handle_sync_pair_endpoint_error(
        request: Request, exc: SyncPairEndpointError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorModel(code="sync_pair_endpoint_invalid", message=str(exc)),
        )

    @app.exception_handler(SelectionRuleNotFoundError)
    async def _handle_selection_rule_not_found(
        request: Request, exc: SelectionRuleNotFoundError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_404_NOT_FOUND,
            ErrorModel(code="selection_rule_not_found", message=str(exc), entity=str(exc.rule_id)),
        )

    @app.exception_handler(SelectionRuleOrdinalConflictError)
    async def _handle_selection_rule_ordinal_conflict(
        request: Request, exc: SelectionRuleOrdinalConflictError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_409_CONFLICT,
            ErrorModel(
                code="selection_rule_ordinal_conflict",
                message=str(exc),
                field="ordinal",
                entity=str(exc.pair_id),
            ),
        )

    @app.exception_handler(SelectionRuleReorderMismatchError)
    async def _handle_selection_rule_reorder_mismatch(
        request: Request, exc: SelectionRuleReorderMismatchError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorModel(
                code="selection_rule_reorder_mismatch", message=str(exc), entity=str(exc.pair_id)
            ),
        )

    @app.exception_handler(SelectionOverrideNotFoundError)
    async def _handle_selection_override_not_found(
        request: Request, exc: SelectionOverrideNotFoundError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_404_NOT_FOUND,
            ErrorModel(
                code="selection_override_not_found",
                message=str(exc),
                entity=str(exc.override_id),
            ),
        )

    @app.exception_handler(SelectionOverrideAlreadyExistsError)
    async def _handle_selection_override_already_exists(
        request: Request, exc: SelectionOverrideAlreadyExistsError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_409_CONFLICT,
            ErrorModel(
                code="selection_override_already_exists", message=str(exc), entity=str(exc.pair_id)
            ),
        )

    @app.exception_handler(ConfigServiceError)
    async def _handle_config_service_error(
        request: Request, exc: ConfigServiceError
    ) -> JSONResponse:
        # Safety net -- see the module docstring's tier 2. Every subclass above is
        # matched first: Starlette's exception-handler lookup walks the raised
        # exception's MRO and dispatches to the *most specific* registered class.
        return _respond(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorModel(code="config_service_error", message=str(exc)),
        )

    # ---- Connector discovery (WP2) errors, surfaced through endpoint registration ------

    @app.exception_handler(ConnectorNotRegisteredError)
    async def _handle_connector_not_registered(
        request: Request, exc: ConnectorNotRegisteredError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorModel(
                code="connector_not_registered",
                message=str(exc),
                field="connector",
                entity=exc.name,
            ),
        )

    @app.exception_handler(ConnectorBrokenError)
    async def _handle_connector_broken(request: Request, exc: ConnectorBrokenError) -> JSONResponse:
        return _respond(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ErrorModel(code="connector_broken", message=str(exc), entity=exc.broken.name),
        )

    @app.exception_handler(ConnectorLookupError)
    async def _handle_connector_lookup_error(
        request: Request, exc: ConnectorLookupError
    ) -> JSONResponse:
        # Safety net -- see the module docstring's tier 2.
        return _respond(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorModel(code="connector_lookup_error", message=str(exc)),
        )

    # ---- Secret reference parsing (T10.2) -----------------------------------------------

    @app.exception_handler(SecretRefFormatError)
    async def _handle_secret_ref_format_error(
        request: Request, exc: SecretRefFormatError
    ) -> JSONResponse:
        return _respond(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorModel(code="secret_ref_invalid", message=str(exc), field="secret_ref"),
        )

    # ---- FastAPI/Starlette built-ins, redirected to the same shape ---------------------

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        field = ".".join(str(part) for part in errors[0]["loc"]) if errors else None
        return _respond(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorModel(
                code="request_validation_error",
                message="request validation failed",
                field=field,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Covers an explicit ``raise HTTPException(...)`` from route code and
        # Starlette's own "no route matched" 404 for any path this app's routing did not
        # claim outright.
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return _respond(
            exc.status_code,
            ErrorModel(
                code=_CODE_BY_HTTP_STATUS.get(exc.status_code, "http_error"), message=detail
            ),
        )

    # ---- Anything else: never leak it (tier 3) ------------------------------------------

    @app.exception_handler(Exception)
    async def _handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        correlation_id = str(uuid.uuid4())
        _LOG.error(
            "api.unhandled_exception",
            correlation_id=correlation_id,
            error=str(exc),
            error_type=type(exc).__name__,
            path=request.url.path,
            exc_info=True,
        )
        return _respond(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ErrorModel(
                code="internal_error",
                message="an unexpected error occurred",
                correlation_id=correlation_id,
            ),
        )
