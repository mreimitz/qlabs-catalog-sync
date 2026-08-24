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
  :class:`EndpointConfigInvalidError` names only field names and connector-authored
  validator prose (see :func:`_config_validation_reason`), and anything
  else unrecognized gets a generic, value-free reason with the real exception logged
  server-side only (mirrors ``api.errors``'s own generic-500 tier, which this route never
  reaches for a healthcheck precisely because it is caught here first).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final, cast

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from qlabs_catalog_sync.config import SecretBackend, SecretNotFoundError
from qlabs_catalog_sync.configstore.models import EndpointRow
from qlabs_catalog_sync.configstore.secrets import (
    SecretRef,
    SecretRefFormatError,
    resolve_connector_kwargs,
    resolve_status,
    secret_field_names,
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
from .connectors import CapabilityManifestOut, capability_manifest_out

__all__ = ["EndpointConfigInvalidError", "build_endpoints_router"]

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


class StoredSecretOut(BaseModel):
    """Whether one secret-typed field has a stored credential -- never which one.

    Mirrors :class:`~qlabs_catalog_sync.configstore.service.StoredSecretStatus` field for
    field, and like it has nothing capable of holding a value. ``updated_at`` is what lets
    the console say "saved 3 minutes ago" without ever reading the credential back, which
    is the only feedback a write-only field can honestly give.
    """

    model_config = ConfigDict(extra="forbid")

    field: str
    is_set: bool
    updated_at: datetime | None = None
    key_id: str | None = None


class StoredSecretWriteRequest(BaseModel):
    """The one request body in this API that carries a credential.

    It travels in a request body over the console's own session-authenticated,
    CSRF-protected origin and is never echoed back: there is no route that returns a
    stored credential, so the value is write-only from the moment it arrives.
    ``extra="forbid"`` so a client cannot smuggle extra fields alongside it.
    """

    model_config = ConfigDict(extra="forbid")

    value: str = Field(
        min_length=1,
        description=(
            "The credential itself. Write-only: no route ever returns it, and it is "
            "sealed before it reaches the database."
        ),
    )


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


class EndpointManifestOut(BaseModel):
    """What this endpoint's connector reports it supports, once configured (C6).

    ``GET /connectors`` lists connector *classes*, which have no configuration, and a
    connector whose manifest depends on its resolved config is entitled to refuse there
    (see ``routes/connectors.py``'s module docstring -- the Databricks connector does
    exactly that, because D6 makes ``tags`` readable only when a SQL warehouse is
    configured). This route is the other half: a *configured* endpoint can always be
    asked, because ``setup()`` has run.

    Always a 200, exactly like a red healthcheck is: ``manifest`` is set when the
    connector answered, ``unavailable_reason`` when it could not. An unreachable tenant is
    a fact about the endpoint, not an error in the request that asked. Never both, never
    neither.
    """

    model_config = ConfigDict(frozen=True)

    endpoint: str
    manifest: CapabilityManifestOut | None = None
    unavailable_reason: str | None = None
    """Human-readable and safe to render as-is -- never carries credential material; see
    :func:`_read_connector_manifest` for how each failure class is narrowed."""


# --------------------------------------------------------------------------------------
# Healthcheck: build a throwaway connector instance, run setup()+healthcheck(), and
# always return a HealthStatus -- never let an exception escape (see module docstring).
# --------------------------------------------------------------------------------------


class EndpointConfigInvalidError(Exception):
    """An endpoint's stored settings plus its resolved secrets do not make a valid
    ``ConnectorConfig`` -- e.g. no credential route is configured at all.

    Exists so this route can tell "the operator has not finished configuring this
    endpoint" apart from "something unexpected blew up", which the generic
    ``except Exception`` tier cannot: a pydantic ``ValidationError`` reaching that tier
    becomes a correlation id and a type name, which is the correct treatment for an
    unknown failure and a useless one for the commonest configuration mistake there is.
    ``reason`` is built by :func:`_config_validation_reason` and is safe to surface.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _config_validation_reason(exc: ValidationError, config_model: type[ConnectorConfig]) -> str:
    """Render ``exc`` into an operator-facing reason that cannot carry a secret value.

    Three things keep a credential out of the result:

    * ``include_input=False`` drops pydantic's ``input`` from every error, which is the
      one part of a ``ValidationError`` guaranteed to be the raw value -- the specific
      leak ``_run_connector_healthcheck``'s generic tier exists to prevent.
    * A **field-level** error naming one of ``config_model``'s own secret-typed fields
      (:func:`~qlabs_catalog_sync.configstore.secrets.secret_field_names`, the same
      single definition the configuration service refuses inline secrets by) never
      surfaces its ``msg`` at all: a connector's own field validator is free to write
      ``f"bad token {value!r}"``, and this is the one place that would be echoed.
    * A **model-level** error (empty ``loc``, i.e. a cross-field validator) does surface
      its ``msg``, which is hand-written connector prose -- the same trust already placed
      in ``ConnectorError.message`` (``qlabs_catalog_sync_sdk.exceptions``'s module
      docstring: a connector must never put credential material in it). This is the
      branch that carries the useful sentence, e.g. Databricks' "configure exactly one
      credential route".
    """
    secret_fields = secret_field_names(config_model)
    problems: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        loc = error["loc"]
        message = error["msg"].removeprefix("Value error, ")
        if not loc:
            problems.append(message)
            continue
        name = ".".join(str(part) for part in loc)
        if loc[0] in secret_fields:
            problems.append(f"{name}: invalid value (withheld -- this field is a secret)")
        else:
            problems.append(f"{name}: {message}")
    return "this endpoint is not fully configured: " + "; ".join(problems)


def _connector_config_for(
    row: EndpointRow,
    connector_cls: type[Connector],
    *,
    backend_factory: Callable[[SecretRef], SecretBackend],
) -> ConnectorConfig:
    """Resolve ``row``'s credential reference and build its connector's own
    ``ConnectorConfig``. Shared by every route that needs a live connector instance --
    :func:`_run_connector_healthcheck` and :func:`_read_connector_manifest` -- because
    this is the security-sensitive step and a second copy of it is a second chance to get
    credential handling subtly wrong.

    Credential resolution follows ``configstore.secrets``'s own documented pattern exactly
    (see that module's docstring): ``resolve_connector_kwargs(ref,
    connector_cls.ConfigModel, settings=row.settings)`` keyed on ``ref.locator``, the
    secret reference's own addressing -- decoupled from ``row.name`` on purpose, so
    renaming an endpoint or pointing two endpoints at one shared credential never touches
    an environment variable. An endpoint with no ``secret_ref`` bound yet (C6: naming and
    settings can be registered before a credential is) has nothing to resolve, so its own
    ``name`` is used as the (irrelevant, since no secret-typed field is required to have a
    value) environment prefix instead.

    Raises whatever resolution or validation raises -- each caller maps those to its own
    surface, and neither ever lets one reach the client carrying a resolved value. The one
    translation done here rather than by a caller is pydantic's ``ValidationError``, which
    becomes an :class:`EndpointConfigInvalidError` carrying a reason
    :func:`_config_validation_reason` has already made safe: doing it here means both
    callers get the same treatment from the same code, which is the same argument that put
    credential resolution itself in this one function.
    """
    config_model_cls = cast(type[ConnectorConfig], connector_cls.ConfigModel)
    kwargs: dict[str, Any]
    if row.secret_ref is not None:
        ref = SecretRef.parse(row.secret_ref)
        kwargs = resolve_connector_kwargs(
            ref, config_model_cls, settings=row.settings, backend=backend_factory(ref)
        )
        locator = ref.locator
    else:
        kwargs = dict(row.settings)
        locator = row.name
    try:
        return config_model_cls.for_endpoint(locator, **kwargs)
    except ValidationError as exc:
        raise EndpointConfigInvalidError(_config_validation_reason(exc, config_model_cls)) from exc


async def _run_connector_healthcheck(
    row: EndpointRow,
    connector_cls: type[Connector],
    *,
    backend_factory: Callable[[SecretRef], SecretBackend],
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
    connector: Connector | None = None
    try:
        connector_config = _connector_config_for(
            row, connector_cls, backend_factory=backend_factory
        )

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
    except EndpointConfigInvalidError as exc:
        # An endpoint nobody has finished configuring -- the commonest reason a
        # healthcheck cannot run at all. Its reason is built value-free by
        # _config_validation_reason; without this branch it lands in the generic tier
        # below and the operator is told to go read a correlation id in the server log
        # to learn that they never set a credential.
        return HealthStatus.unhealthy(row.name, exc.reason, checked_at=now)
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


async def _read_connector_manifest(
    row: EndpointRow,
    connector_cls: type[Connector],
    *,
    backend_factory: Callable[[SecretRef], SecretBackend],
) -> tuple[CapabilityManifestOut | None, str | None]:
    """``setup()`` a throwaway instance of ``connector_cls`` for ``row`` and return its
    ``capabilities()``, or ``(None, reason)`` -- never raising.

    The failure taxonomy is deliberately identical to
    :func:`_run_connector_healthcheck`'s, and for the same reasons, which are spelled out
    there: ``ConnectorError.message`` and the two secret-reference errors are documented
    safe to surface, while **anything else gets a generic, value-free reason** and a
    correlation id, because e.g. a pydantic ``ValidationError`` over a ``SecretStr`` field
    can echo its raw input. Reproducing that taxonomy rather than simplifying it is the
    point: this route resolves the same credentials the healthcheck does, so it needs the
    same care.
    """
    connector: Connector | None = None
    try:
        connector_config = _connector_config_for(
            row, connector_cls, backend_factory=backend_factory
        )
        connector = connector_cls()
        ctx = ConnectorContext.build(config=connector_config, endpoint=row.name)
        async with asyncio.timeout(HEALTHCHECK_TIMEOUT_SECONDS):
            await connector.setup(ctx)
            return capability_manifest_out(connector.capabilities()), None
    except TimeoutError:
        return None, (f"connector did not respond within {HEALTHCHECK_TIMEOUT_SECONDS:.0f}s")
    except ConnectorError as exc:
        return None, exc.message
    except (SecretNotFoundError, SecretRefFormatError) as exc:
        return None, str(exc)
    except EndpointConfigInvalidError as exc:
        return None, exc.reason
    except Exception as exc:
        correlation_id = str(uuid.uuid4())
        _LOG.error(
            "endpoint.manifest.unexpected_error",
            endpoint=row.name,
            connector=row.connector,
            correlation_id=correlation_id,
            error_type=type(exc).__name__,
        )
        return None, (
            f"reading the capability manifest failed unexpectedly ({type(exc).__name__}); "
            f"see server logs (correlation id {correlation_id})"
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
        result = resolve_status(
            ref, config_model_cls, backend=config_service.secret_backend_for(ref)
        )
        return SecretResolveOut(resolvable=result.resolvable, reason=result.reason)

    @router.get(
        "/{name}/secrets",
        response_model=list[StoredSecretOut],
        responses=API_ERROR_RESPONSES,
        summary="Which of this endpoint's secret-typed fields have a stored credential",
    )
    async def list_endpoint_secrets(name: str) -> list[StoredSecretOut]:
        """Every secret-typed field this endpoint's connector declares, and whether a
        credential is stored for it.

        Fields with nothing stored are listed too: "this connector wants a client secret
        and none has been entered" is precisely what the console has to render, and a
        response that only listed what exists could not say it.
        """
        statuses = await config_service.list_endpoint_secrets(name)
        return [
            StoredSecretOut(
                field=status_row.field,
                is_set=status_row.is_set,
                updated_at=status_row.updated_at,
                key_id=status_row.key_id,
            )
            for status_row in statuses
        ]

    @router.put(
        "/{name}/secrets/{field}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=API_ERROR_RESPONSES,
        summary="Store or replace one credential for this endpoint",
    )
    async def put_endpoint_secret(
        name: str, field: str, payload: StoredSecretWriteRequest, request: Request
    ) -> Response:
        """Seal a credential and store it (amended C2).

        ``PUT`` rather than ``POST`` because it is idempotent in the way that matters:
        submitting the same credential twice leaves the endpoint in the same state, and
        re-submitting is exactly what an operator does after a typo. 204 with no body,
        because the only thing this route could return is the value it was just given.
        """
        session = require_session(request)
        await config_service.set_endpoint_secret(
            name, field, payload.value, actor=session.username, now=datetime.now(UTC)
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.delete(
        "/{name}/secrets/{field}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=API_ERROR_RESPONSES,
        summary="Remove one stored credential from this endpoint",
    )
    async def delete_endpoint_secret(name: str, field: str, request: Request) -> Response:
        session = require_session(request)
        await config_service.clear_endpoint_secret(
            name, field, actor=session.username, now=datetime.now(UTC)
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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
        result = await _run_connector_healthcheck(
            row, connector_cls, backend_factory=config_service.secret_backend_for
        )
        return EndpointHealthOut(
            endpoint=row.name,
            state=result.state,
            reason=result.reason,
            checked_at=result.checked_at or datetime.now(UTC),
            details=result.details,
        )

    @router.get(
        "/{name}/manifest",
        response_model=EndpointManifestOut,
        responses=API_ERROR_RESPONSES,
        summary="What this configured endpoint's connector supports (C6)",
    )
    async def read_manifest(name: str) -> EndpointManifestOut:
        """The capability manifest for a *configured* endpoint.

        Real I/O against the tenant, exactly like ``/healthcheck`` -- ``setup()`` runs, so
        this is a deliberate action an operator takes, never something a list screen fires
        for every row.
        """
        row = await config_service.get_endpoint(name)
        if row is None:
            raise EndpointNotFoundError(name)
        # Same reasoning as the healthcheck route: an endpoint naming a connector that is
        # not installed is a registration problem, not a manifest result.
        connector_cls = registry.get_connector(row.connector)
        manifest, reason = await _read_connector_manifest(
            row, connector_cls, backend_factory=config_service.secret_backend_for
        )
        return EndpointManifestOut(endpoint=row.name, manifest=manifest, unavailable_reason=reason)

    return router
