"""Endpoint CRUD routes (C6, WP12/T12.3): register, edit, heathcheck, resolve, delete.

C6 in full: *"'Install an endpoint' means registering an instance of a connector that is
already present... naming one, pointing it at a tenant, binding a secret reference,
running a healthcheck, reading its capability manifest and enabling it. No package is
fetched, installed or executed."* This module is the "naming... binding... healthcheck...
enabling" half (the capability manifest itself is ``routes/connectors.py``, T12.3's other
half). Also C2: the console *"reports whether a reference resolves and whether
``healthcheck()`` passes; it never displays, accepts or persists a secret value."*

Every write goes through :class:`~qlabs_catalog_sync.configstore.service.ConfigService`
(T10.3) -- this module never touches an ORM row directly, and never invents a status code:
a route calls the service and lets its typed exceptions propagate to the handlers already
installed by :func:`~qlabs_catalog_sync.api.errors.install_error_handlers` (T12.1). In
particular :class:`~qlabs_catalog_sync.configstore.service.InlineSecretRejectedError` --
raised when ``settings`` names one of the connector's own secret-typed fields -- already
has a handler mapping it to a 422 with a ``field``-naming body; this module adds nothing
of its own for that case, it just never intercepts it.

**Two things never happen here, by construction:**

* **A secret value never enters a request or a response.** ``EndpointCreateRequest``/
  ``EndpointUpdateRequest`` carry ``secret_ref`` (a reference string, e.g.
  ``"env:QLIK_ACME"``) and non-secret ``settings``; ``EndpointOut`` carries the same two
  back out. Nothing here has a field capable of holding a resolved value.
  ``SecretResolveOut`` mirrors
  :class:`~qlabs_catalog_sync.configstore.secrets.SecretResolveStatus` exactly --
  ``resolvable``/``reason`` and nothing else -- for the same reason that type has no more
  fields than that: it is enforced by the shape, not a promise kept elsewhere.
* **A red healthcheck is a 200, never a 500.** ``Connector.healthcheck()`` is real I/O
  against a tenant and can fail in every way a network can -- a bad credential, a timeout,
  the tenant itself being down. :func:`_run_connector_healthcheck` catches all of it and
  always returns a :class:`~qlabs_catalog_sync_sdk.contract.HealthStatus`, never lets an
  exception reach the route; the console renders "this endpoint is down", not an error
  toast. The one thing that *does* propagate as an ordinary 4xx/5xx is the endpoint naming
  a connector that is not installed or is broken (``qlabs_catalog_sync.discovery``'s own
  lookup errors) -- that is a registration problem to fix, not a health signal to display.
  Credential material never reaches the response or a log record: a
  :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError`'s ``message`` is documented
  safe to surface (``qlabs_catalog_sync_sdk.exceptions``'s own module docstring), a
  :class:`~qlabs_catalog_sync.config.SecretNotFoundError`/
  :class:`~qlabs_catalog_sync.configstore.secrets.SecretRefFormatError` names only the
  endpoint/key/backend or the malformed reference string -- never a value -- and anything
  else unrecognized gets a generic, value-free reason with the real exception logged
  server-side only (mirrors ``api.errors``'s own generic-500 tier, which this route never
  reaches for a healthcheck precisely because it is caught here first).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any, Final, cast

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from qlabs_catalog_sync.config import SecretNotFoundError
from qlabs_catalog_sync.configstore.models import EndpointRow
from qlabs_catalog_sync.configstore.secrets import (
    SecretRef,
    SecretRefFormatError,
    resolve_connector_kwargs,
    resolve_status,
)
from qlabs_catalog_sync.configstore.service import UNSET, ConfigService, EndpointNotFoundError
from qlabs_catalog_sync.configstore.types import EndpointRole
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.observability import get_logger
from qlabs_catalog_sync_sdk.config import ConnectorConfig, ConnectorContext
from qlabs_catalog_sync_sdk.contract import Connector, HealthState, HealthStatus
from qlabs_catalog_sync_sdk.exceptions import ConnectorError

from ..auth import require_session
from ..errors import API_ERROR_RESPONSES

__all__ = ["build_endpoints_router"]

_LOG = get_logger("qlabs.catalog_sync.api.routes.endpoints")

#: Ceiling on one healthcheck's ``setup()`` + ``healthcheck()`` combined. Generous --
#: this is an operator clicking a button in the console, not a scheduled cycle -- but
#: bounded, so one unreachable tenant cannot hang the request indefinitely; a timeout
#: becomes an ordinary unhealthy result (see the module docstring), not a hung response.
HEALTHCHECK_TIMEOUT_SECONDS: Final[float] = 30.0


# --------------------------------------------------------------------------------------
# Response / request models
# --------------------------------------------------------------------------------------


class EndpointOut(BaseModel):
    """One registered endpoint. Carries ``secret_ref`` (a reference, C2) and non-secret
    ``settings`` -- never a secret value; there is no field here capable of holding one.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    connector: str
    role: EndpointRole
    settings: dict[str, JsonValue]
    secret_ref: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: EndpointRow) -> EndpointOut:
        return cls(
            name=row.name,
            connector=row.connector,
            role=row.role,
            # EndpointRow.settings is a plain dict[str, object] column (it round-trips
            # through the database's own JSON codec, which only ever produces JSON-safe
            # values); pydantic validates the shape for real on construction, this cast
            # just tells mypy what that already-JSON-safe value's type is.
            settings=cast(dict[str, JsonValue], dict(row.settings)),
            secret_ref=row.secret_ref,
            enabled=row.enabled,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class EndpointCreateRequest(BaseModel):
    """Register an instance of an already-discovered connector (C6). ``settings`` is
    validated against the connector's own ``ConfigModel``; an inline secret there is
    refused (``InlineSecretRejectedError``, already handled -- see the module
    docstring), never stored. Binding a credential is ``secret_ref``'s job."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    connector: str = Field(min_length=1, max_length=128)
    role: EndpointRole
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    secret_ref: str | None = Field(default=None, max_length=255)
    enabled: bool = False


class EndpointUpdateRequest(BaseModel):
    """A partial update (C6: edit settings, bind a secret reference, enable/disable). A
    field omitted from the request body keeps its current value. ``secret_ref`` is the
    one nullable column: sending it explicitly as ``null`` clears the reference (an
    endpoint can be un-bound without deleting and recreating it); omitting it leaves
    whatever is currently bound alone. Distinguishing "omitted" from "sent as null" is
    read from ``model_fields_set``, then translated into
    :class:`~qlabs_catalog_sync.configstore.service.ConfigService`'s own ``UNSET``
    convention -- this route never invents a second way to say "not supplied".
    """

    model_config = ConfigDict(extra="forbid")

    connector: str | None = Field(default=None, min_length=1, max_length=128)
    role: EndpointRole | None = None
    settings: dict[str, JsonValue] | None = None
    secret_ref: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None


class SecretResolveOut(BaseModel):
    """Whether an endpoint's ``secret_ref`` resolves (C2) -- ``resolvable``/``reason``
    and nothing else. Mirrors
    :class:`~qlabs_catalog_sync.configstore.secrets.SecretResolveStatus` field for
    field: that type carries no field capable of holding a resolved value, and neither
    does this one."""

    model_config = ConfigDict(frozen=True)

    resolvable: bool
    reason: str


class EndpointHealthOut(BaseModel):
    """The result of running one endpoint's connector ``healthcheck()`` (C6). Always a
    200 -- see the module docstring for why a red result lands here, never as an HTTP
    error."""

    model_config = ConfigDict(frozen=True)

    endpoint: str
    state: HealthState
    reason: str | None
    checked_at: datetime
    details: dict[str, JsonValue]


# --------------------------------------------------------------------------------------
# Healthcheck: build a throwaway connector instance, run setup()+healthcheck(), and
# always return a HealthStatus -- never let an exception escape (see module docstring).
# --------------------------------------------------------------------------------------


async def _run_connector_healthcheck(
    row: EndpointRow, connector_cls: type[Connector]
) -> HealthStatus:
    """Build, ``setup()`` and ``healthcheck()`` a fresh instance of ``connector_cls`` for
    ``row``, catching everything into a :class:`HealthStatus` rather than raising.

    Mirrors how ``cli/wiring.py``'s ``build_connector_pool`` builds a real connector
    (``connector_cls()``, then ``ConfigModel.for_endpoint(...)``, then
    ``ConnectorContext.build(...)``, then ``setup()``) -- the difference is this instance
    is used once and discarded (a console-triggered healthcheck, not a pooled connector
    kept for a sync cycle), and every failure becomes an unhealthy result instead of a
    raised ``CliError``.

    Credential resolution follows ``configstore.secrets``'s own documented pattern
    exactly (see that module's docstring): ``resolve_connector_kwargs(ref,
    connector_cls.ConfigModel, settings=row.settings)`` keyed on ``ref.locator``, the
    secret reference's own addressing -- decoupled from ``row.name`` on purpose, so
    renaming an endpoint or pointing two endpoints at one shared credential never touches
    an environment variable. An endpoint with no ``secret_ref`` bound yet (C6: naming and
    settings can be registered before a credential is) has nothing to resolve, so its own
    ``name`` is used as the (irrelevant, since no secret-typed field is required to have
    a value) environment prefix instead.
    """
    now = datetime.now(UTC)
    config_model_cls = cast(type[ConnectorConfig], connector_cls.ConfigModel)
    connector: Connector | None = None
    try:
        kwargs: dict[str, Any]
        if row.secret_ref is not None:
            ref = SecretRef.parse(row.secret_ref)
            kwargs = resolve_connector_kwargs(ref, config_model_cls, settings=row.settings)
            locator = ref.locator
        else:
            kwargs = dict(row.settings)
            locator = row.name
        connector_config = config_model_cls.for_endpoint(locator, **kwargs)

        connector = connector_cls()
        ctx = ConnectorContext.build(config=connector_config, endpoint=row.name)
        async with asyncio.timeout(HEALTHCHECK_TIMEOUT_SECONDS):
            await connector.setup(ctx)
            return await connector.healthcheck()
    except TimeoutError:
        return HealthStatus.unhealthy(
            row.name,
            f"connector did not respond within {HEALTHCHECK_TIMEOUT_SECONDS:.0f}s",
            checked_at=now,
        )
    except ConnectorError as exc:
        # exc.message is documented safe to surface (qlabs_catalog_sync_sdk.exceptions's
        # own module docstring: connectors must never put credential material in it).
        return HealthStatus.unhealthy(row.name, exc.message, checked_at=now)
    except (SecretNotFoundError, SecretRefFormatError) as exc:
        # Both name only the endpoint/key/backend or the malformed reference string --
        # never a resolved value (see each class's own docstring).
        return HealthStatus.unhealthy(row.name, str(exc), checked_at=now)
    except Exception as exc:
        # Anything else unrecognized: a connector bug, a config-validation failure while
        # building its ConnectorConfig, ... str(exc) is NOT assumed safe here (unlike the
        # three narrower cases above) -- a pydantic ValidationError over a SecretStr
        # field can echo its raw input, for instance -- so the response gets a generic,
        # value-free reason and the real exception (type only, no message) goes to the
        # structured log, which is itself routed through the SDK's redact_secrets
        # processor (qlabs_catalog_sync.observability.configure_logging) as a second line
        # of defense, not the only one.
        correlation_id = str(uuid.uuid4())
        _LOG.error(
            "endpoint.healthcheck.unexpected_error",
            endpoint=row.name,
            connector=row.connector,
            correlation_id=correlation_id,
            error_type=type(exc).__name__,
        )
        return HealthStatus.unhealthy(
            row.name,
            f"healthcheck failed unexpectedly ({type(exc).__name__}); see server logs "
            f"(correlation id {correlation_id})",
            checked_at=now,
        )
    finally:
        if connector is not None:
            with contextlib.suppress(Exception):
                await connector.close()


# --------------------------------------------------------------------------------------
# The router
# --------------------------------------------------------------------------------------


def build_endpoints_router(config_service: ConfigService, registry: ConnectorRegistry) -> APIRouter:
    """Build the ``/endpoints`` router over an already-built ``config_service``/``registry``.

    Mirrors ``api.auth._build_auth_router``'s shape: a factory taking its dependencies
    explicitly, called once from :func:`~qlabs_catalog_sync.api.app.create_app`.
    """
    router = APIRouter(prefix="/endpoints", tags=["endpoints"])

    @router.get(
        "",
        response_model=list[EndpointOut],
        responses=API_ERROR_RESPONSES,
        summary="List configured endpoints",
    )
    async def list_endpoints() -> list[EndpointOut]:
        rows = await config_service.list_endpoints()
        return [EndpointOut.of(row) for row in rows]

    @router.post(
        "",
        response_model=EndpointOut,
        status_code=status.HTTP_201_CREATED,
        responses=API_ERROR_RESPONSES,
        summary="Register an instance of a discovered connector (C6)",
    )
    async def create_endpoint(payload: EndpointCreateRequest, request: Request) -> EndpointOut:
        session = require_session(request)
        row = await config_service.create_endpoint(
            name=payload.name,
            connector=payload.connector,
            role=payload.role,
            settings=payload.settings,
            secret_ref=payload.secret_ref,
            enabled=payload.enabled,
            actor=session.username,
            now=datetime.now(UTC),
        )
        return EndpointOut.of(row)

    @router.get(
        "/{name}",
        response_model=EndpointOut,
        responses=API_ERROR_RESPONSES,
        summary="Read one endpoint",
    )
    async def get_endpoint(name: str) -> EndpointOut:
        row = await config_service.get_endpoint(name)
        if row is None:
            raise EndpointNotFoundError(name)
        return EndpointOut.of(row)

    @router.patch(
        "/{name}",
        response_model=EndpointOut,
        responses=API_ERROR_RESPONSES,
        summary="Edit an endpoint: settings, secret reference, role, or enabled",
    )
    async def update_endpoint(
        name: str, payload: EndpointUpdateRequest, request: Request
    ) -> EndpointOut:
        session = require_session(request)
        supplied = payload.model_fields_set
        row = await config_service.update_endpoint(
            name,
            connector=(
                payload.connector
                if "connector" in supplied and payload.connector is not None
                else UNSET
            ),
            role=payload.role if "role" in supplied and payload.role is not None else UNSET,
            settings=(
                payload.settings
                if "settings" in supplied and payload.settings is not None
                else UNSET
            ),
            # secret_ref is the one nullable field: `None` here, when the client sent it,
            # means "clear the reference" -- see EndpointUpdateRequest's own docstring.
            secret_ref=payload.secret_ref if "secret_ref" in supplied else UNSET,
            enabled=(
                payload.enabled if "enabled" in supplied and payload.enabled is not None else UNSET
            ),
            actor=session.username,
            now=datetime.now(UTC),
        )
        return EndpointOut.of(row)

    @router.delete(
        "/{name}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=API_ERROR_RESPONSES,
        summary="Delete an endpoint",
    )
    async def delete_endpoint(name: str, request: Request) -> Response:
        session = require_session(request)
        await config_service.delete_endpoint(name, actor=session.username, now=datetime.now(UTC))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/{name}/secret-resolve",
        response_model=SecretResolveOut,
        responses=API_ERROR_RESPONSES,
        summary="Whether this endpoint's secret reference resolves (C2)",
    )
    async def secret_resolve(name: str) -> SecretResolveOut:
        row = await config_service.get_endpoint(name)
        if row is None:
            raise EndpointNotFoundError(name)
        connector_cls = registry.get_connector(row.connector)
        if row.secret_ref is None:
            return SecretResolveOut(
                resolvable=False, reason="no secret_ref is bound to this endpoint yet"
            )
        ref = SecretRef.parse(row.secret_ref)
        config_model_cls = cast(type[ConnectorConfig], connector_cls.ConfigModel)
        result = resolve_status(ref, config_model_cls)
        return SecretResolveOut(resolvable=result.resolvable, reason=result.reason)

    @router.post(
        "/{name}/healthcheck",
        response_model=EndpointHealthOut,
        responses=API_ERROR_RESPONSES,
        summary="Run this endpoint's connector healthcheck() (C6)",
    )
    async def run_healthcheck(name: str) -> EndpointHealthOut:
        row = await config_service.get_endpoint(name)
        if row is None:
            raise EndpointNotFoundError(name)
        # Not found / broken propagates as an ordinary error: an endpoint naming a
        # connector that is not installed is a registration problem to fix, not a health
        # signal (see the module docstring).
        connector_cls = registry.get_connector(row.connector)
        result = await _run_connector_healthcheck(row, connector_cls)
        return EndpointHealthOut(
            endpoint=row.name,
            state=result.state,
            reason=result.reason,
            checked_at=result.checked_at or datetime.now(UTC),
            details=result.details,
        )

    return router
