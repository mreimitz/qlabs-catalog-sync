"""Sync-pair CRUD routes (WP12/T12.4): register, edit, delete a source-to-target pair.

A sync pair names a source endpoint, a target endpoint, the Qlik space new/updated data
products belong to, which neutral entity types to sync, a poll cadence, the manual-edit
policy (decision.md guardrail 2), and the two explicit opt-ins v1 never flips on by
default: ``activation_opt_in`` (D7) and ``enabled`` itself. Selection -- *which* objects
a pair actually syncs -- is not a pair field at all as of C3/T10.1: it lives in the
ordered ``selection_rules``/``selection_overrides`` tables this module's sibling,
``routes/selection.py``, exposes.

Every write goes through :class:`~qlabs_catalog_sync.configstore.service.ConfigService`
(T10.3) -- this module never touches an ORM row directly, and never invents a status
code: a route calls the service and lets its typed exceptions propagate to the handlers
:func:`~qlabs_catalog_sync.api.errors.install_error_handlers` already installed (T12.1).
``manual_edit_policy``/``entity_types`` are the *same* types
(:class:`~qlabs_catalog_sync.config.ManualEditPolicy`,
:class:`~qlabs_catalog_sync_sdk.models.EntityType`) the engine's environment-loaded
config and the ``sync_pairs`` schema itself already use (``configstore/models.py``,
``configstore/types.py``) -- reused here as request/response field types rather than
redescribed, so a pair written through this API and one written through
``ConfigService`` directly mean exactly the same thing.

**The v1 upstream-only direction guardrail (Qlik is the sole write target; source must
never be it) is enforced by** ``ConfigService.create_sync_pair``/``update_sync_pair``
**itself** (``configstore/service.py``'s ``_check_pair_direction``), which raises
:class:`~qlabs_catalog_sync.configstore.service.SyncPairEndpointError` -- already mapped
by ``api/errors.py`` to a 422 naming the offending endpoint and connector. This module
adds nothing of its own for that case; it just never intercepts it.

**A pair referencing a disabled endpoint is also rejected here**, one layer further out
than a missing one. ``ConfigService`` itself only checks that ``source``/``target`` name
a *configured* endpoint (existence, checked inside its own write transaction); it does
not check ``enabled``, and this module owns no path under ``configstore/`` to add that
check there. :func:`_reject_disabled_endpoint` closes the gap at the route instead: a
read (``ConfigService.get_endpoint``, never a raw ORM row) immediately before the write,
raising the *same* typed :class:`~qlabs_catalog_sync.configstore.service.SyncPairEndpointError`
a missing endpoint would -- an operator sees one consistent error family for "this
endpoint reference does not work yet", not two. Being a pre-write read rather than a
check inside the service's own transaction, it is not airtight against a concurrent
disable racing the pair write (C7's single-administrator-session model makes that a
narrow, low-value window to close) -- worth folding into ``ConfigService`` itself in a
future task that owns that file, not papered over here by reaching into an ORM session
this module has no business opening.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from qlabs_catalog_sync.config import ManualEditPolicy
from qlabs_catalog_sync.configstore.models import SyncPairRow
from qlabs_catalog_sync.configstore.service import (
    UNSET,
    ConfigService,
    SyncPairEndpointError,
    SyncPairNotFoundError,
)
from qlabs_catalog_sync_sdk.models import EntityType

from ..auth import require_session
from ..errors import API_ERROR_RESPONSES

__all__ = ["build_pairs_router"]


# --------------------------------------------------------------------------------------
# Response / request models
# --------------------------------------------------------------------------------------


class SyncPairOut(BaseModel):
    """One configured sync pair."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    source: str
    target: str
    target_space: str
    entity_types: list[EntityType]
    cadence_seconds: int
    jitter_seconds: float | None
    manual_edit_policy: ManualEditPolicy
    activation_opt_in: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: SyncPairRow) -> SyncPairOut:
        return cls(
            id=row.id,
            name=row.name,
            source=row.source,
            target=row.target,
            target_space=row.target_space,
            entity_types=list(row.entity_types),
            cadence_seconds=row.cadence_seconds,
            jitter_seconds=row.jitter_seconds,
            manual_edit_policy=row.manual_edit_policy,
            activation_opt_in=row.activation_opt_in,
            enabled=row.enabled,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SyncPairCreateRequest(BaseModel):
    """Create a sync pair. ``source``/``target`` name already-registered endpoints
    (``routes/endpoints.py``, T12.3); the v1 direction guardrail and the disabled-endpoint
    check (see the module docstring) both apply here."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)
    target_space: str = Field(min_length=1, max_length=255)
    entity_types: list[EntityType] = Field(default_factory=list)
    cadence_seconds: int = Field(default=900, gt=0)
    jitter_seconds: float | None = Field(default=None, ge=0)
    manual_edit_policy: ManualEditPolicy = Field(default_factory=ManualEditPolicy)
    activation_opt_in: bool = False
    enabled: bool = False


class SyncPairUpdateRequest(BaseModel):
    """A partial update. A field omitted from the request body keeps its current value.
    ``jitter_seconds`` is the one nullable field: sending it explicitly as ``null``
    clears the per-pair override back to "use the scheduler's computed default";
    omitting it leaves whatever is currently set alone -- same convention as
    ``EndpointUpdateRequest.secret_ref`` (``routes/endpoints.py``).

    Touching ``source`` and/or ``target`` re-runs both the v1 direction guardrail
    (``ConfigService``) and the disabled-endpoint check (this module) against the
    *effective* endpoints, mirroring ``ConfigService.update_sync_pair``'s own "only
    re-check what changed" behavior.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    source: str | None = Field(default=None, min_length=1, max_length=128)
    target: str | None = Field(default=None, min_length=1, max_length=128)
    target_space: str | None = Field(default=None, min_length=1, max_length=255)
    entity_types: list[EntityType] | None = None
    cadence_seconds: int | None = Field(default=None, gt=0)
    jitter_seconds: float | None = Field(default=None, ge=0)
    manual_edit_policy: ManualEditPolicy | None = None
    activation_opt_in: bool | None = None
    enabled: bool | None = None


# --------------------------------------------------------------------------------------
# The disabled-endpoint pre-check -- see the module docstring for why it lives here
# --------------------------------------------------------------------------------------


async def _reject_disabled_endpoint(
    config_service: ConfigService,
    *,
    pair_name: str,
    role_label: str,
    endpoint_name: str,
) -> None:
    """Raise :class:`SyncPairEndpointError` when ``endpoint_name`` is configured but
    disabled. A missing endpoint is left alone here -- ``ConfigService`` itself raises
    the same error type for that case, inside its own write transaction."""
    endpoint = await config_service.get_endpoint(endpoint_name)
    if endpoint is not None and not endpoint.enabled:
        raise SyncPairEndpointError(
            f"sync pair {pair_name!r}: {role_label} endpoint {endpoint_name!r} is "
            "registered but disabled; enable it (or point the pair at an already-enabled "
            "endpoint) before it can be used by a sync pair"
        )


# --------------------------------------------------------------------------------------
# The router
# --------------------------------------------------------------------------------------


def build_pairs_router(config_service: ConfigService) -> APIRouter:
    """Build the ``/pairs`` router over an already-built ``config_service``.

    Mirrors ``api.routes.endpoints.build_endpoints_router``'s shape: a factory taking its
    one dependency explicitly, called once from
    :func:`~qlabs_catalog_sync.api.app.create_app`.
    """
    router = APIRouter(prefix="/pairs", tags=["pairs"])

    @router.get(
        "",
        response_model=list[SyncPairOut],
        responses=API_ERROR_RESPONSES,
        summary="List configured sync pairs",
    )
    async def list_pairs() -> list[SyncPairOut]:
        rows = await config_service.list_sync_pairs()
        return [SyncPairOut.of(row) for row in rows]

    @router.post(
        "",
        response_model=SyncPairOut,
        status_code=status.HTTP_201_CREATED,
        responses=API_ERROR_RESPONSES,
        summary="Create a sync pair",
    )
    async def create_pair(payload: SyncPairCreateRequest, request: Request) -> SyncPairOut:
        session = require_session(request)
        await _reject_disabled_endpoint(
            config_service,
            pair_name=payload.name,
            role_label="source",
            endpoint_name=payload.source,
        )
        await _reject_disabled_endpoint(
            config_service,
            pair_name=payload.name,
            role_label="target",
            endpoint_name=payload.target,
        )
        row = await config_service.create_sync_pair(
            name=payload.name,
            source=payload.source,
            target=payload.target,
            target_space=payload.target_space,
            entity_types=payload.entity_types,
            cadence_seconds=payload.cadence_seconds,
            jitter_seconds=payload.jitter_seconds,
            manual_edit_policy=payload.manual_edit_policy,
            activation_opt_in=payload.activation_opt_in,
            enabled=payload.enabled,
            actor=session.username,
            now=datetime.now(UTC),
        )
        return SyncPairOut.of(row)

    @router.get(
        "/{pair_id}",
        response_model=SyncPairOut,
        responses=API_ERROR_RESPONSES,
        summary="Read one sync pair",
    )
    async def get_pair(pair_id: uuid.UUID) -> SyncPairOut:
        row = await config_service.get_sync_pair(pair_id)
        if row is None:
            raise SyncPairNotFoundError(pair_id)
        return SyncPairOut.of(row)

    @router.patch(
        "/{pair_id}",
        response_model=SyncPairOut,
        responses=API_ERROR_RESPONSES,
        summary="Edit a sync pair",
    )
    async def update_pair(
        pair_id: uuid.UUID, payload: SyncPairUpdateRequest, request: Request
    ) -> SyncPairOut:
        session = require_session(request)
        supplied = payload.model_fields_set

        if "source" in supplied or "target" in supplied:
            current = await config_service.get_sync_pair(pair_id)
            if current is None:
                raise SyncPairNotFoundError(pair_id)
            effective_name = (
                payload.name if "name" in supplied and payload.name is not None else current.name
            )
            effective_source = (
                payload.source
                if "source" in supplied and payload.source is not None
                else current.source
            )
            effective_target = (
                payload.target
                if "target" in supplied and payload.target is not None
                else current.target
            )
            await _reject_disabled_endpoint(
                config_service,
                pair_name=effective_name,
                role_label="source",
                endpoint_name=effective_source,
            )
            await _reject_disabled_endpoint(
                config_service,
                pair_name=effective_name,
                role_label="target",
                endpoint_name=effective_target,
            )

        row = await config_service.update_sync_pair(
            pair_id,
            name=payload.name if "name" in supplied and payload.name is not None else UNSET,
            source=payload.source if "source" in supplied and payload.source is not None else UNSET,
            target=payload.target if "target" in supplied and payload.target is not None else UNSET,
            target_space=(
                payload.target_space
                if "target_space" in supplied and payload.target_space is not None
                else UNSET
            ),
            entity_types=(
                payload.entity_types
                if "entity_types" in supplied and payload.entity_types is not None
                else UNSET
            ),
            cadence_seconds=(
                payload.cadence_seconds
                if "cadence_seconds" in supplied and payload.cadence_seconds is not None
                else UNSET
            ),
            # jitter_seconds is nullable: an explicit `null` in the request clears it.
            jitter_seconds=payload.jitter_seconds if "jitter_seconds" in supplied else UNSET,
            manual_edit_policy=(
                payload.manual_edit_policy
                if "manual_edit_policy" in supplied and payload.manual_edit_policy is not None
                else UNSET
            ),
            activation_opt_in=(
                payload.activation_opt_in
                if "activation_opt_in" in supplied and payload.activation_opt_in is not None
                else UNSET
            ),
            enabled=(
                payload.enabled if "enabled" in supplied and payload.enabled is not None else UNSET
            ),
            actor=session.username,
            now=datetime.now(UTC),
        )
        return SyncPairOut.of(row)

    @router.delete(
        "/{pair_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=API_ERROR_RESPONSES,
        summary="Delete a sync pair (cascades its selection rules and overrides)",
    )
    async def delete_pair(pair_id: uuid.UUID, request: Request) -> Response:
        session = require_session(request)
        await config_service.delete_sync_pair(
            pair_id, actor=session.username, now=datetime.now(UTC)
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
