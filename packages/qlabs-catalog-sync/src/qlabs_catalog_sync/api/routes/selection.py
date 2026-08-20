"""Selection-rule and per-object-override routes (WP12/T12.4, C3/C4/C5).

Nested under one pair (``/pairs/{pair_id}/rules``, ``/pairs/{pair_id}/overrides``): a
rule's and an override's whole meaning is scoped to one pair, and reordering in
particular only makes sense within one pair's one scope. Both resources share this
module because they share one vocabulary (:class:`~qlabs_catalog_sync.configstore.
types.RuleScope`, :class:`~qlabs_catalog_sync.configstore.types.SelectionDecision`) and
one write discipline: every mutation goes through
:class:`~qlabs_catalog_sync.configstore.service.ConfigService` (T10.3), never an ORM row
touched directly, and every typed error it raises is left to propagate to the handlers
``api/errors.py`` already installed (T12.1).

**Rule order is the whole meaning of a rule set (C3: last match wins).**
:class:`SelectionRuleOut` therefore always carries its own ``ordinal`` -- evaluation
order is never left to be inferred from JSON array position alone, which a client
library, a diff tool, or a careless re-serialization could silently scramble. Every
route that *lists* rules also returns them ordinal-ascending
(``ConfigService.list_selection_rules`` already orders the query), so the two signals
--the field and the array position -- agree by construction; a test in this task's suite
is written to fail if they ever stopped agreeing.

**Reordering is one explicit, all-or-nothing operation** -- :func:`SelectionRuleReorderRequest`
takes the *complete* ordered list of a ``(pair, scope)``'s rule ids, never a "move rule X
to index N" delta. That shape was chosen deliberately over the alternative:

* A delta op ("move this one rule to index N") would need *this route* to fetch the
  current order, splice the moved rule in, and hand the service the resulting full list --
  business logic and a read-then-write race that has no business being here when the
  service's own contract is already "give me the complete new order or nothing changes".
* The full-list shape needs no route-level reasoning at all: it is passed straight to
  :meth:`~qlabs_catalog_sync.configstore.service.ConfigService.reorder_selection_rules`,
  which is where the real coherence check already lives -- ``ordered_rule_ids`` must name
  exactly the existing rule ids for that ``(pair, scope)``, each once, or nothing is
  written (:class:`~qlabs_catalog_sync.configstore.service.SelectionRuleReorderMismatchError`,
  already mapped to a 422 by ``api/errors.py``). A reorder naming an unknown rule id, or
  omitting one that exists, is refused whole -- there is no partial application to worry
  about, because this route never asks the service for anything short of the complete
  target order.

**Pattern validation is reused, not reinvented.**
:func:`~qlabs_catalog_sync.selection.rules.validate_pattern` is the same function
``SelectionRule`` itself calls on construction (T11.1) and the same grammar
``config.py``'s D1 ``catalog_schema_patterns`` uses for glob/object-scope patterns --
calling it here, before a create or an update reaches
:class:`~qlabs_catalog_sync.configstore.service.ConfigService` (whose ``SelectionRuleRow``
write path stores ``pattern`` as opaque text with no shape check of its own), is what
keeps a rule an operator saves through the console and a rule they hand-write in YAML
meaning the same thing, and is what turns "invalid pattern" into the precise, actionable
reason :func:`validate_pattern` already computes (e.g. *"a dataset-scope pattern needs
three dot-separated segments (catalog.schema.table), got 'analytics.sales'"*) rather than
a generic complaint.

**The override constraint that matters most in this task.**
``tests/selection/test_preview_sync_agreement.py`` (T11.1-T11.3's certification test)
pins the one place the console's preview and the sync loop can disagree: the sync loop
sees one dataset change at a time, with no tree and no browse API, so it can only derive
a dataset's *parent schema* candidate from the dataset's own qualified name -- the
derived candidate's ``object_id`` **is** that qualified name, never a stable schema id,
because a lone dataset change carries no reference to one. An object-scope override
pinned on the schema's real, opaque stable id is therefore honoured by the preview (which
evaluates every schema from its own real change) and silently invisible to the sync loop
-- exactly the promise-the-console-cannot-keep C4 forbids. ``configstore/models.py``
already documents ``selection_overrides.object_id`` as holding "a ``catalog.schema`` for
object scope, a ``catalog.schema.table`` for dataset scope" -- a plain qualified name,
never an opaque id, for *either* scope. :func:`_validate_override_object_id` makes that
the API's actual behavior rather than only its comment: it refuses an ``object_id`` that
is not shaped like a literal qualified name for the override's scope (the right number of
non-empty, non-wildcard dot-separated segments -- an override pins one exact object, so a
glob's wildcard characters are refused too, precisely so a pattern can never be typed into
a field that only ever compares by exact string equality). A schema's opaque stable id --
whatever shape a given connector's ids take, but never dotted the way a qualified name
is -- fails the segment-count check outright, with a message naming exactly why. This is
enforced for both scopes uniformly (matching what the schema already documents), even
though the sync-loop divergence this exists to prevent is specific to object scope: a
dataset-scope override pinned by the dataset's own stable id would not actually diverge
(a single dataset change already carries its own real stable id, no derivation involved),
but there is no *documented* meaning for a non-qualified-name ``object_id`` at either
scope, so this module does not invent a second, scope-conditional rule the schema itself
does not describe.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from qlabs_catalog_sync.configstore.models import SelectionOverrideRow, SelectionRuleRow
from qlabs_catalog_sync.configstore.service import (
    UNSET,
    ConfigService,
    SelectionOverrideNotFoundError,
    SelectionRuleNotFoundError,
    SyncPairNotFoundError,
)
from qlabs_catalog_sync.configstore.types import MatcherKind, RuleScope, SelectionDecision
from qlabs_catalog_sync.selection.rules import SEGMENTS_BY_SCOPE, validate_pattern

from ..auth import require_session
from ..errors import API_ERROR_RESPONSES, APIError

__all__ = ["build_selection_router"]

#: The documented shape of an override's ``object_id`` per scope (``configstore/models.py``).
_QUALIFIED_NAME_SHAPE_BY_SCOPE: Final[dict[RuleScope, str]] = {
    RuleScope.OBJECT: "catalog.schema",
    RuleScope.DATASET: "catalog.schema.table",
}

#: A literal qualified-name segment: identifier characters only -- deliberately excludes
#: the glob wildcards ``qlabs_catalog_sync.selection.rules``'s own segment grammar allows
#: (``* ? [ ] !``), because an override pins one *exact* object, never a pattern.
_QUALIFIED_NAME_SEGMENT: Final = re.compile(r"^[A-Za-z0-9_]+$")

#: A module-level singleton rather than a ``Query(...)`` call in an argument default
#: (ruff B008) -- reused by every ``scope`` query parameter below.
_SCOPE_QUERY: Final = Query(..., description="object or dataset")


# --------------------------------------------------------------------------------------
# Response / request models -- rules
# --------------------------------------------------------------------------------------


class SelectionRuleOut(BaseModel):
    """One ordered include/exclude rule (C3). ``ordinal`` is the explicit evaluation-order
    signal -- see the module docstring."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    pair_id: uuid.UUID
    ordinal: int
    scope: RuleScope
    decision: SelectionDecision
    matcher_kind: MatcherKind
    pattern: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: SelectionRuleRow) -> SelectionRuleOut:
        return cls(
            id=row.id,
            pair_id=row.pair_id,
            ordinal=row.ordinal,
            scope=row.scope,
            decision=row.decision,
            matcher_kind=row.matcher_kind,
            pattern=row.pattern,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SelectionRuleCreateRequest(BaseModel):
    """Create one rule. ``ordinal`` defaults to "append at the end" of this pair's
    ``(scope)`` rule set -- omit it unless placing the rule at a specific position
    matters."""

    model_config = ConfigDict(extra="forbid")

    scope: RuleScope
    decision: SelectionDecision
    matcher_kind: MatcherKind
    pattern: str = Field(min_length=1, max_length=2000)
    ordinal: int | None = Field(default=None, ge=0)


class SelectionRuleUpdateRequest(BaseModel):
    """Update a rule's ``decision``/``matcher_kind``/``pattern``. Deliberately excludes
    ``ordinal`` and ``scope`` -- position is :class:`SelectionRuleReorderRequest`'s job,
    never an implicit side effect of an unrelated field edit (mirrors
    ``ConfigService.update_selection_rule``'s own contract)."""

    model_config = ConfigDict(extra="forbid")

    decision: SelectionDecision | None = None
    matcher_kind: MatcherKind | None = None
    pattern: str | None = Field(default=None, min_length=1, max_length=2000)


class SelectionRuleReorderRequest(BaseModel):
    """Rewrite one ``(pair, scope)``'s rule order to exactly ``rule_ids`` -- see the
    module docstring for why this is a complete list, never a delta."""

    model_config = ConfigDict(extra="forbid")

    scope: RuleScope
    rule_ids: list[uuid.UUID] = Field(min_length=1)


# --------------------------------------------------------------------------------------
# Response / request models -- overrides
# --------------------------------------------------------------------------------------


class SelectionOverrideOut(BaseModel):
    """One per-object pin that beats every rule (C3)."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    pair_id: uuid.UUID
    scope: RuleScope
    object_id: str
    decision: SelectionDecision
    reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: SelectionOverrideRow) -> SelectionOverrideOut:
        return cls(
            id=row.id,
            pair_id=row.pair_id,
            scope=row.scope,
            object_id=row.object_id,
            decision=row.decision,
            reason=row.reason,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SelectionOverrideCreateRequest(BaseModel):
    """Pin one object's decision. ``object_id`` must be a literal qualified name for
    ``scope`` (``catalog.schema``/``catalog.schema.table``) -- see the module docstring
    for why an opaque stable id is refused here rather than silently accepted."""

    model_config = ConfigDict(extra="forbid")

    scope: RuleScope
    object_id: str = Field(min_length=1, max_length=1000)
    decision: SelectionDecision
    reason: str | None = Field(default=None, max_length=2000)


class SelectionOverrideUpdateRequest(BaseModel):
    """Update an override's ``decision``/``reason``. Excludes ``scope``/``object_id``:
    together with the pair, they are this override's identity -- moving a pin to a
    different object is a delete-and-recreate (mirrors
    ``ConfigService.update_selection_override``'s own contract)."""

    model_config = ConfigDict(extra="forbid")

    decision: SelectionDecision | None = None
    reason: str | None = None


# --------------------------------------------------------------------------------------
# Validation reused from qlabs_catalog_sync.selection.rules, and the override constraint
# -- see the module docstring for both.
# --------------------------------------------------------------------------------------


def _validate_rule_pattern(*, scope: RuleScope, matcher_kind: MatcherKind, pattern: str) -> None:
    """Raise a 422 :class:`APIError` naming exactly what is wrong with ``pattern``,
    reusing :func:`~qlabs_catalog_sync.selection.rules.validate_pattern`'s own message
    rather than a generic "invalid pattern"."""
    try:
        validate_pattern(pattern, matcher_kind=matcher_kind, scope=scope)
    except ValueError as exc:
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "selection_rule_pattern_invalid",
            str(exc),
            field="pattern",
        ) from exc


def _validate_override_object_id(object_id: str, *, scope: RuleScope) -> None:
    """Raise a 422 :class:`APIError` unless ``object_id`` is a literal qualified name
    shaped for ``scope`` -- see the module docstring for the full rationale."""
    expected = SEGMENTS_BY_SCOPE[scope]
    shape = _QUALIFIED_NAME_SHAPE_BY_SCOPE[scope]
    segments = object_id.split(".")

    if len(segments) != expected or not all(segment.strip() for segment in segments):
        divergence = (
            "the sync loop derives a dataset's parent schema purely from the dataset's "
            "own qualified name (there is no browse API, and a lone dataset change "
            "carries no reference to its schema's stable id), so a pin on a schema's "
            "opaque id is honoured by the console preview and silently invisible to the "
            "sync loop -- the console would promise a sync that never happens"
            if scope is RuleScope.OBJECT
            else "selection_overrides.object_id is documented as holding the object's "
            "qualified name for every scope, never an opaque id"
        )
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "selection_override_object_id_invalid",
            f"{object_id!r} is not a {shape} qualified name ({expected} non-empty "
            f"dot-separated segments) -- a {scope.value}-scope override must be pinned "
            f"by qualified name, never by a connector's opaque stable id: {divergence}",
            field="object_id",
        )

    for segment in segments:
        if not _QUALIFIED_NAME_SEGMENT.match(segment):
            raise APIError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "selection_override_object_id_invalid",
                f"{object_id!r} has an invalid segment {segment!r} -- an override pins "
                "one exact object by its literal qualified name, not a glob pattern "
                "(letters, digits and underscore only; no * ? [ ] ! wildcards)",
                field="object_id",
            )


# --------------------------------------------------------------------------------------
# Pair-scoped lookups -- a rule/override id is a global UUID, but every route here is
# reached through one pair's path, so a mismatched pair_id is treated as "not found"
# rather than silently acting on a rule/override that belongs to a different pair.
# --------------------------------------------------------------------------------------


async def _require_pair(config_service: ConfigService, pair_id: uuid.UUID) -> None:
    if await config_service.get_sync_pair(pair_id) is None:
        raise SyncPairNotFoundError(pair_id)


async def _get_rule_for_pair(
    config_service: ConfigService, pair_id: uuid.UUID, rule_id: uuid.UUID
) -> SelectionRuleRow:
    row = await config_service.get_selection_rule(rule_id)
    if row is None or row.pair_id != pair_id:
        raise SelectionRuleNotFoundError(rule_id)
    return row


async def _get_override_for_pair(
    config_service: ConfigService, pair_id: uuid.UUID, override_id: uuid.UUID
) -> SelectionOverrideRow:
    row = await config_service.get_selection_override(override_id)
    if row is None or row.pair_id != pair_id:
        raise SelectionOverrideNotFoundError(override_id)
    return row


# --------------------------------------------------------------------------------------
# The router
# --------------------------------------------------------------------------------------


def build_selection_router(config_service: ConfigService) -> APIRouter:
    """Build the ``/pairs/{pair_id}/rules`` and ``/pairs/{pair_id}/overrides`` router
    over an already-built ``config_service``.

    Mirrors ``api.routes.pairs.build_pairs_router``'s shape: a factory taking its one
    dependency explicitly, called once from
    :func:`~qlabs_catalog_sync.api.app.create_app`. Both routers share the ``/pairs``
    path prefix without conflict -- ``pairs.py`` owns ``/pairs`` and ``/pairs/{pair_id}``,
    this module owns everything under ``/pairs/{pair_id}/rules`` and
    ``/pairs/{pair_id}/overrides``; the two never register the same path.
    """
    router = APIRouter(prefix="/pairs", tags=["selection"])

    # ==== Selection rules ================================================================

    @router.get(
        "/{pair_id}/rules",
        response_model=list[SelectionRuleOut],
        responses=API_ERROR_RESPONSES,
        summary="List one pair's selection rules for one scope, in evaluation order",
    )
    async def list_rules(
        pair_id: uuid.UUID, scope: RuleScope = _SCOPE_QUERY
    ) -> list[SelectionRuleOut]:
        await _require_pair(config_service, pair_id)
        rows = await config_service.list_selection_rules(pair_id, scope)
        return [SelectionRuleOut.of(row) for row in rows]

    @router.post(
        "/{pair_id}/rules",
        response_model=SelectionRuleOut,
        status_code=status.HTTP_201_CREATED,
        responses=API_ERROR_RESPONSES,
        summary="Create one selection rule",
    )
    async def create_rule(
        pair_id: uuid.UUID, payload: SelectionRuleCreateRequest, request: Request
    ) -> SelectionRuleOut:
        session = require_session(request)
        _validate_rule_pattern(
            scope=payload.scope, matcher_kind=payload.matcher_kind, pattern=payload.pattern
        )
        row = await config_service.create_selection_rule(
            pair_id=pair_id,
            scope=payload.scope,
            decision=payload.decision,
            matcher_kind=payload.matcher_kind,
            pattern=payload.pattern,
            ordinal=payload.ordinal,
            actor=session.username,
            now=datetime.now(UTC),
        )
        return SelectionRuleOut.of(row)

    @router.post(
        "/{pair_id}/rules/reorder",
        response_model=list[SelectionRuleOut],
        responses=API_ERROR_RESPONSES,
        summary="Rewrite one (pair, scope)'s rule order atomically (C3)",
    )
    async def reorder_rules(
        pair_id: uuid.UUID, payload: SelectionRuleReorderRequest, request: Request
    ) -> list[SelectionRuleOut]:
        session = require_session(request)
        rows = await config_service.reorder_selection_rules(
            pair_id,
            payload.scope,
            payload.rule_ids,
            actor=session.username,
            now=datetime.now(UTC),
        )
        return [SelectionRuleOut.of(row) for row in rows]

    @router.get(
        "/{pair_id}/rules/{rule_id}",
        response_model=SelectionRuleOut,
        responses=API_ERROR_RESPONSES,
        summary="Read one selection rule",
    )
    async def get_rule(pair_id: uuid.UUID, rule_id: uuid.UUID) -> SelectionRuleOut:
        row = await _get_rule_for_pair(config_service, pair_id, rule_id)
        return SelectionRuleOut.of(row)

    @router.patch(
        "/{pair_id}/rules/{rule_id}",
        response_model=SelectionRuleOut,
        responses=API_ERROR_RESPONSES,
        summary="Edit a selection rule's decision, matcher kind, or pattern",
    )
    async def update_rule(
        pair_id: uuid.UUID,
        rule_id: uuid.UUID,
        payload: SelectionRuleUpdateRequest,
        request: Request,
    ) -> SelectionRuleOut:
        session = require_session(request)
        current = await _get_rule_for_pair(config_service, pair_id, rule_id)
        supplied = payload.model_fields_set

        if "matcher_kind" in supplied or "pattern" in supplied:
            effective_matcher_kind = (
                payload.matcher_kind
                if "matcher_kind" in supplied and payload.matcher_kind is not None
                else current.matcher_kind
            )
            effective_pattern = (
                payload.pattern
                if "pattern" in supplied and payload.pattern is not None
                else current.pattern
            )
            _validate_rule_pattern(
                scope=current.scope, matcher_kind=effective_matcher_kind, pattern=effective_pattern
            )

        row = await config_service.update_selection_rule(
            rule_id,
            decision=(
                payload.decision
                if "decision" in supplied and payload.decision is not None
                else UNSET
            ),
            matcher_kind=(
                payload.matcher_kind
                if "matcher_kind" in supplied and payload.matcher_kind is not None
                else UNSET
            ),
            pattern=(
                payload.pattern if "pattern" in supplied and payload.pattern is not None else UNSET
            ),
            actor=session.username,
            now=datetime.now(UTC),
        )
        return SelectionRuleOut.of(row)

    @router.delete(
        "/{pair_id}/rules/{rule_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=API_ERROR_RESPONSES,
        summary="Delete a selection rule",
    )
    async def delete_rule(pair_id: uuid.UUID, rule_id: uuid.UUID, request: Request) -> Response:
        session = require_session(request)
        await _get_rule_for_pair(config_service, pair_id, rule_id)
        await config_service.delete_selection_rule(
            rule_id, actor=session.username, now=datetime.now(UTC)
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ==== Selection overrides =============================================================

    @router.get(
        "/{pair_id}/overrides",
        response_model=list[SelectionOverrideOut],
        responses=API_ERROR_RESPONSES,
        summary="List one pair's per-object overrides for one scope",
    )
    async def list_overrides(
        pair_id: uuid.UUID, scope: RuleScope = _SCOPE_QUERY
    ) -> list[SelectionOverrideOut]:
        await _require_pair(config_service, pair_id)
        rows = await config_service.list_selection_overrides(pair_id, scope)
        return [SelectionOverrideOut.of(row) for row in rows]

    @router.post(
        "/{pair_id}/overrides",
        response_model=SelectionOverrideOut,
        status_code=status.HTTP_201_CREATED,
        responses=API_ERROR_RESPONSES,
        summary="Pin one object's selection decision (C3)",
    )
    async def create_override(
        pair_id: uuid.UUID, payload: SelectionOverrideCreateRequest, request: Request
    ) -> SelectionOverrideOut:
        session = require_session(request)
        _validate_override_object_id(payload.object_id, scope=payload.scope)
        row = await config_service.create_selection_override(
            pair_id=pair_id,
            scope=payload.scope,
            object_id=payload.object_id,
            decision=payload.decision,
            reason=payload.reason,
            actor=session.username,
            now=datetime.now(UTC),
        )
        return SelectionOverrideOut.of(row)

    @router.get(
        "/{pair_id}/overrides/{override_id}",
        response_model=SelectionOverrideOut,
        responses=API_ERROR_RESPONSES,
        summary="Read one override",
    )
    async def get_override(pair_id: uuid.UUID, override_id: uuid.UUID) -> SelectionOverrideOut:
        row = await _get_override_for_pair(config_service, pair_id, override_id)
        return SelectionOverrideOut.of(row)

    @router.patch(
        "/{pair_id}/overrides/{override_id}",
        response_model=SelectionOverrideOut,
        responses=API_ERROR_RESPONSES,
        summary="Edit an override's decision or reason",
    )
    async def update_override(
        pair_id: uuid.UUID,
        override_id: uuid.UUID,
        payload: SelectionOverrideUpdateRequest,
        request: Request,
    ) -> SelectionOverrideOut:
        session = require_session(request)
        await _get_override_for_pair(config_service, pair_id, override_id)
        supplied = payload.model_fields_set
        row = await config_service.update_selection_override(
            override_id,
            decision=(
                payload.decision
                if "decision" in supplied and payload.decision is not None
                else UNSET
            ),
            # reason is nullable: an explicit `null` in the request clears it.
            reason=payload.reason if "reason" in supplied else UNSET,
            actor=session.username,
            now=datetime.now(UTC),
        )
        return SelectionOverrideOut.of(row)

    @router.delete(
        "/{pair_id}/overrides/{override_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=API_ERROR_RESPONSES,
        summary="Delete an override",
    )
    async def delete_override(
        pair_id: uuid.UUID, override_id: uuid.UUID, request: Request
    ) -> Response:
        session = require_session(request)
        await _get_override_for_pair(config_service, pair_id, override_id)
        await config_service.delete_selection_override(
            override_id, actor=session.username, now=datetime.now(UTC)
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
