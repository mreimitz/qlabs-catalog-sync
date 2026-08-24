"""Source-tree and preview routes (C4, C5, WP12/T12.5): browse a live source lazily, and
evaluate a rule set -- saved or a draft that has never been saved -- to counts and a
sample, without writing anything.

Two routes, one shared code path underneath both
------------------------------------------------

* ``GET {API_PREFIX}/pairs/{pair_id}/source-tree`` -- one page of the pair's source tree,
  each node carrying its decision and the deciding rule, evaluated against the pair's
  **stored** selection rules and overrides -- "what the next real sync would see right
  now".
* ``POST {API_PREFIX}/pairs/{pair_id}/preview`` -- included/excluded counts plus a
  bounded sample, evaluated against either the pair's stored rules (the default) or a
  **draft** rule list supplied in the request body that has never been saved -- "what
  would happen if I saved this edit". See :class:`PreviewRequest` for why only ``rules``
  is draftable and overrides always come from storage.

Both routes go through **exactly** the one code path decision C4 requires and nothing
else: :func:`~qlabs_catalog_sync.selection.source_tree.walk_source_tree`, which itself
calls :func:`~qlabs_catalog_sync.selection.evaluator.evaluate` per candidate and
:func:`~qlabs_catalog_sync.selection.source_tree.compose_dataset_selection` for the C5
schema/dataset join -- the same functions T11.3's sync loop calls. This module contains
no rule-matching, no scope composition and no decision logic of its own: every field in
every response model below is copied off a real
:class:`~qlabs_catalog_sync.selection.evaluator.SelectionResult` /
:class:`~qlabs_catalog_sync.selection.source_tree.DatasetSelection`, never recomputed. A
route that started reimplementing any part of that would be exactly the second
implementation C4 forbids, and ``tests/selection/test_preview_sync_agreement.py``
(T11.1-T11.3's certification test) is what a caller of this module's functions is
implicitly certified against; ``tests/api/test_preview.py`` re-proves the same property
one layer up, through real HTTP.

Nothing here writes
--------------------

Neither route calls a single ``ConfigService.create_*``/``update_*``/``delete_*``
method -- both call only ``get_sync_pair``, ``get_endpoint``, ``list_selection_rules``
and ``list_selection_overrides``, every one a plain read. Neither route imports
``qlabs_catalog_sync.state`` (the watermark store) at all, for the same reason
``source_tree.py``'s own module docstring gives: a preview that advanced the pair's real
watermark would make the next real sync cycle skip exactly what the operator just
previewed. ``tests/api/test_preview.py`` pins both properties: a structural check that
neither this module nor its imports reach the state store, and a behavioral one -- two
consecutive requests against the same seeded source return identical results.

Stored rules for browsing, stored *or* draft for previewing
-------------------------------------------------------------

The browse route always evaluates the pair's **stored, saved** configuration:
"browse the source" is naturally read as "show me what will happen right now", and
a tree endpoint is a poor fit for carrying a whole draft rule list on every page
request as the operator scrolls. The preview route is built for the opposite case --
"show me what would happen if I saved this" -- so :class:`PreviewRequest.rules`, left
unset, previews the stored rules exactly like the browse route does, and set to an
explicit (possibly empty) list previews that unsaved draft instead, with the pair's
**stored overrides** joined in either way. Overrides are deliberately never draftable
here: each one is already a single, near-atomically-saved per-object pin (T12.4's
``/pairs/{pair_id}/overrides`` routes), unlike a rule list an operator is actively
reordering and re-wording in the console before committing -- so there is no realistic
"unsaved override edit" to preview, and folding a second draft concept into this route
would only add a code path nothing asks for. A draft rule is validated exactly the way
T12.4's own rule-create route validates one: constructing a
:class:`~qlabs_catalog_sync.selection.rules.SelectionRule` calls
:func:`~qlabs_catalog_sync.selection.rules.validate_pattern` itself, in its
``__post_init__`` -- the *same* function, not a second copy of its grammar -- so a
pattern this route accepts for preview is exactly one T12.4's routes would accept to
save, and one this route rejects could never be saved either.

Laziness, and what "large source" means for each route
---------------------------------------------------------

``walk_source_tree`` pages a connector's own ``list_changed`` on demand; this module
never turns it into a list before it is done being asked for it, and neither route holds
more than one bounded page/sample in memory at once (``tests/api/test_preview.py``
proves this by counting the fake connector's own ``list_changed`` calls, not just
inspecting the response shape).

* **Browse** is capped by ``limit``/``offset`` query parameters
  (:data:`DEFAULT_SOURCE_TREE_PAGE_SIZE` default, :data:`MAX_SOURCE_TREE_PAGE_SIZE`
  ceiling). Because a walk cannot be resumed
  across two separate HTTP requests without holding a connector and a live generator
  open server-side between them -- fragile, stateful, and a poor fit for the
  request-scoped connector lifecycle every other route in this package already uses --
  each page request re-walks from the beginning and discards everything before
  ``offset`` one item at a time, never buffering more than one page. This costs
  ``O(offset + limit)`` real work per request rather than ``O(limit)``, a known,
  accepted trade-off for a stateless page-at-a-time browse over a live source; an
  operator paging forward through a tree pays it, a script requesting page 10,000
  should not. The response never claims completeness it cannot back up:
  :attr:`SourceTreePageOut.has_more` is set by fetching one extra node past ``limit``,
  and :attr:`SourceTreePageOut.next_offset` is ``None`` exactly when there is nothing
  more -- never a silent truncation that looks like the whole tree.
* **Preview** needs a total count, which means walking to the end (or to a cap) either
  way -- there is no page boundary to hand back to the caller. Its boundedness instead
  comes from :attr:`PreviewRequest.max_candidates` (:data:`DEFAULT_PREVIEW_MAX_CANDIDATES`
  default): once that many candidates have been examined, the walk stops and
  :attr:`PreviewOut.truncated` is set, with :attr:`PreviewOut.candidates_examined`
  naming exactly how far it got -- an honest partial count, never a silent one presented
  as whole. Because the cap applies to :func:`~qlabs_catalog_sync.selection.source_tree.
  walk_source_tree`'s own schema-then-dataset order, a source with more schemas than
  the cap allows will show a zero dataset total with ``truncated=True`` -- ``tests/api/
  test_preview.py`` exercises this rather than only mentioning it. Both routes also
  bound wall-clock time (:data:`SOURCE_TREE_TIMEOUT_SECONDS` /
  :data:`PREVIEW_TIMEOUT_SECONDS`), covering connector setup and the walk together, so an
  unreachable or merely very slow source becomes a clear timeout response rather than a
  hung request.

Undetermined is surfaced, never folded into excluded
--------------------------------------------------------

:attr:`SelectionResult.has_undetermined` (RM-01 D6: a tag rule against a source with no
SQL warehouse configured is neither a match nor a considered non-match) is carried
through as its own field everywhere a decision appears, and every
:class:`~qlabs_catalog_sync.selection.evaluator.UndeterminedRule` for a node is listed,
never summarized away. ``included``/``excluded`` in :class:`ScopeCountsOut` are a strict
partition of ``total`` (every candidate is exactly one of the two); ``undetermined`` is
a separate, possibly overlapping tally over that same ``total`` -- a candidate excluded
by :data:`~qlabs_catalog_sync.selection.rules.DEFAULT_DECISION` because a tag rule could
not be evaluated is counted in *both* ``excluded`` (the decision the run would actually
make) and ``undetermined`` (a flag that the decision might have gone the other way with
the missing fact) -- never diverted out of ``excluded`` into a third bucket, which would
misreport what the sync loop is actually going to do.

A source that will not connect is a 4xx/5xx naming the endpoint, never a 500
-------------------------------------------------------------------------------

:func:`_open_source_connector` builds and ``setup()``s a source connector exactly the
way ``cli/wiring.py``'s ``build_connector_pool`` and ``routes/endpoints.py``'s
``_run_connector_healthcheck`` each do (``registry.get_connector(name)()``, then
``ConfigModel.for_endpoint(...)``, then ``ConnectorContext.build(...)``, then
``setup()``), for the pair's **source** endpoint only -- this module never looks at,
resolves credentials for, or connects to a pair's target, matching the upstream-only
v1 guardrail (browsing/previewing reads, it never even considers the write side).
Unlike a healthcheck, a preview or a browse cannot proceed at all once the source is
unreachable, so failure here is not folded into a 200 body: a
:class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError` becomes a 502 naming the
endpoint with its own safe ``message``; an unresolvable secret reference becomes a 422
naming the ``secret_ref`` field; a disabled source endpoint becomes a 422 before any
connector is even built; and a wall-clock timeout (covering setup and the walk
together) becomes a 504. Anything else unrecognized is logged server-side with a
correlation id and answered with a generic, credential-free 502 -- never a leaked
traceback, mirroring ``routes/endpoints.py``'s own tier-3 handling. A connector *lookup*
failure (not installed / installed but broken) is left to propagate to the handlers
``api/errors.py`` already installs for :class:`~qlabs_catalog_sync.discovery.
ConnectorNotRegisteredError`/``ConnectorBrokenError`` -- this module adds nothing of its
own for that case.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, Literal, cast

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field

from qlabs_catalog_sync.config import SecretBackend, SecretNotFoundError
from qlabs_catalog_sync.configstore.models import EndpointRow, SyncPairRow
from qlabs_catalog_sync.configstore.secrets import (
    SecretRef,
    SecretRefFormatError,
    resolve_connector_kwargs,
)
from qlabs_catalog_sync.configstore.service import (
    ConfigService,
    SyncPairEndpointError,
    SyncPairNotFoundError,
)
from qlabs_catalog_sync.configstore.types import MatcherKind, RuleScope, SelectionDecision
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.observability import get_logger
from qlabs_catalog_sync.selection import (
    UNKNOWN,
    CandidateFact,
    DecisionSource,
    QualifiedName,
    SelectionOverride,
    SelectionResult,
    SelectionRule,
    SelectionRuleSet,
)
from qlabs_catalog_sync.selection.source_tree import (
    DatasetSelection,
    SchemaNode,
    SourceTreeNode,
    walk_source_tree,
)
from qlabs_catalog_sync_sdk.config import ConnectorConfig, ConnectorContext
from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.exceptions import ConnectorError
from qlabs_catalog_sync_sdk.models import EntityType

from ..errors import API_ERROR_RESPONSES, APIError

__all__ = ["build_preview_router"]

_LOG = get_logger("qlabs.catalog_sync.api.routes.preview")

# --------------------------------------------------------------------------------------
# Bounds -- see the module docstring's "Laziness" section for what each one protects
# --------------------------------------------------------------------------------------

#: One ``GET /source-tree`` page, default and ceiling.
DEFAULT_SOURCE_TREE_PAGE_SIZE: Final[int] = 200
MAX_SOURCE_TREE_PAGE_SIZE: Final[int] = 1000

#: A preview's sample, default and ceiling -- large enough that a realistic worked
#: example (a handful of schemas and datasets) never gets silently cut, small enough
#: that a console table stays renderable.
DEFAULT_PREVIEW_SAMPLE_LIMIT: Final[int] = 100
MAX_PREVIEW_SAMPLE_LIMIT: Final[int] = 500

#: Total candidates (schemas + datasets, in ``walk_source_tree``'s own order) one
#: preview call examines before stopping and reporting ``truncated=True``.
DEFAULT_PREVIEW_MAX_CANDIDATES: Final[int] = 20_000
MAX_PREVIEW_MAX_CANDIDATES: Final[int] = 200_000

#: Wall-clock ceiling on one browse page: connector setup plus walking to ``offset +
#: limit``. Generous for an operator paging a console tree, bounded so an unreachable
#: source cannot hang the request. Mirrors ``routes/endpoints.py``'s
#: ``HEALTHCHECK_TIMEOUT_SECONDS`` in spirit, longer because a page can be many pages of
#: the connector's own ``list_changed``.
SOURCE_TREE_TIMEOUT_SECONDS: Final[float] = 30.0

#: Wall-clock ceiling on one whole preview: connector setup plus walking the entire tree
#: (or to ``max_candidates``). Longer than the browse timeout on purpose -- a preview's
#: job is to walk to completion, not to stop after one page.
PREVIEW_TIMEOUT_SECONDS: Final[float] = 60.0

_ENTITY_TYPES_BY_SCOPE: Final[dict[RuleScope, tuple[EntityType, ...]]] = {
    RuleScope.OBJECT: (EntityType.DATA_PRODUCT,),
    RuleScope.DATASET: (EntityType.DATASET,),
}


# --------------------------------------------------------------------------------------
# Response models -- every field copied off a real SelectionResult/DatasetSelection,
# never recomputed (see the module docstring)
# --------------------------------------------------------------------------------------


class UndeterminedRuleOut(BaseModel):
    """One rule that could not be evaluated against a candidate (RM-01 D6) -- never
    folded into the decision, always listed."""

    model_config = ConfigDict(frozen=True)

    matcher_kind: MatcherKind
    pattern: str
    decision: SelectionDecision
    missing: CandidateFact
    explain: str


class SelectionResultOut(BaseModel):
    """One candidate's decision and what produced it -- the wire form of
    :class:`~qlabs_catalog_sync.selection.evaluator.SelectionResult`."""

    model_config = ConfigDict(frozen=True)

    decision: SelectionDecision
    included: bool
    source: DecisionSource
    rule_id: str | None
    explain: str
    undetermined: list[UndeterminedRuleOut]


class DatasetSelectionOut(BaseModel):
    """The C5 join for one dataset -- the wire form of
    :class:`~qlabs_catalog_sync.selection.source_tree.DatasetSelection`. ``parent`` and
    ``dataset`` are each the real, complete result (never withheld), so a console that
    wants to know exactly which one decided can always tell from ``explain`` -- or from
    ``parent.source``/``dataset.source`` directly -- rather than from a second,
    separately-computed field that could drift from it."""

    model_config = ConfigDict(frozen=True)

    included: bool
    explain: str
    parent: SelectionResultOut
    dataset: SelectionResultOut


class SchemaNodeOut(BaseModel):
    """One object-scope (``catalog.schema``) tree node."""

    model_config = ConfigDict(frozen=True)

    scope: Literal[RuleScope.OBJECT] = RuleScope.OBJECT
    object_id: str
    qualified_name: str | None
    display_name: str | None
    result: SelectionResultOut


class DatasetNodeOut(BaseModel):
    """One dataset-scope (table/view) tree node."""

    model_config = ConfigDict(frozen=True)

    scope: Literal[RuleScope.DATASET] = RuleScope.DATASET
    object_id: str
    qualified_name: str | None
    display_name: str | None
    selection: DatasetSelectionOut


SourceTreeNodeOut = Annotated[SchemaNodeOut | DatasetNodeOut, Field(discriminator="scope")]


class SourceTreePageOut(BaseModel):
    """One lazily-fetched page of a pair's source tree, evaluated against its stored
    rules -- see the module docstring's "Laziness" section for what ``has_more`` /
    ``next_offset`` promise and what they do not."""

    model_config = ConfigDict(frozen=True)

    nodes: list[SourceTreeNodeOut]
    offset: int
    limit: int
    has_more: bool
    next_offset: int | None


class PreviewSampleItemOut(BaseModel):
    """One example candidate from a preview, decision and deciding rule attached. For a
    dataset, ``rule_id``/``explain`` name whichever of the parent schema's or the
    dataset's own rule actually decided (see :func:`_dataset_deciding_rule_id`) -- the
    same three cases :class:`~qlabs_catalog_sync.selection.source_tree.DatasetSelection`
    documents, never the dataset's own rule when the parent is what decided."""

    model_config = ConfigDict(frozen=True)

    scope: RuleScope
    object_id: str
    qualified_name: str | None
    display_name: str | None
    included: bool
    explain: str
    rule_id: str | None
    has_undetermined: bool


class ScopeCountsOut(BaseModel):
    """Included/excluded/undetermined tallies for one scope.

    ``included + excluded == total`` always -- every candidate decides one way or the
    other. ``undetermined`` is a separate, possibly-overlapping count over that same
    ``total`` (a candidate can be excluded *and* undetermined at once): never a third
    partition member, and never a way for an undetermined result to disappear from
    ``excluded`` -- see the module docstring's "Undetermined" section.
    """

    model_config = ConfigDict(frozen=True)

    total: int
    included: int
    excluded: int
    undetermined: int


class PreviewCountsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    object: ScopeCountsOut
    dataset: ScopeCountsOut


class PreviewOut(BaseModel):
    """Counts plus a bounded sample for one rule set (stored or draft) over one pair's
    live source. ``rule_set_source`` names which one was actually evaluated, so a
    console cannot mistake a draft preview for the stored configuration's own numbers
    or vice versa."""

    model_config = ConfigDict(frozen=True)

    rule_set_source: Literal["stored", "draft"]
    counts: PreviewCountsOut
    sample: list[PreviewSampleItemOut]
    candidates_examined: int
    truncated: bool


# --------------------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------------------


class DraftSelectionRuleIn(BaseModel):
    """One rule of an unsaved draft rule list. Deliberately excludes ``rule_id`` and
    ``ordinal``: the list's own order *is* the ordinal (C3: evaluated low-to-high, last
    match wins), and a fresh, request-scoped id (``draft-<position>``) is synthesized by
    the route -- a draft has nothing durable to name a rule by yet. Every other field
    mirrors ``routes/selection.py``'s own ``SelectionRuleCreateRequest`` exactly, so a
    rule this model accepts is shaped exactly like one that route would accept to save.
    """

    model_config = ConfigDict(extra="forbid")

    scope: RuleScope
    decision: SelectionDecision
    matcher_kind: MatcherKind
    pattern: str = Field(min_length=1, max_length=2000)


class PreviewRequest(BaseModel):
    """What to preview, and how much of it. ``rules`` left unset (the default) previews
    the pair's stored rules; an explicit list (including ``[]``, "no rules at all")
    previews that draft instead. Overrides are never draftable here -- see the module
    docstring for why."""

    model_config = ConfigDict(extra="forbid")

    rules: list[DraftSelectionRuleIn] | None = Field(
        default=None,
        description=(
            "An unsaved draft rule list, evaluated in list order. Omit to preview the "
            "pair's stored, saved rules instead."
        ),
    )
    resolve_tags: bool = Field(
        default=False,
        description=(
            "Read each candidate's real tags (one extra read() per node, only where the "
            "source's manifest offers tags at all) instead of leaving them unknown."
        ),
    )
    resolve_owners: bool = Field(default=False, description="Same trade-off as resolve_tags.")
    sample_limit: int = Field(
        default=DEFAULT_PREVIEW_SAMPLE_LIMIT, ge=1, le=MAX_PREVIEW_SAMPLE_LIMIT
    )
    max_candidates: int = Field(
        default=DEFAULT_PREVIEW_MAX_CANDIDATES, ge=1, le=MAX_PREVIEW_MAX_CANDIDATES
    )


# --------------------------------------------------------------------------------------
# Rule set construction -- stored rows, or a validated draft, joined with stored
# overrides either way. See the module docstring's "Stored rules for browsing..." and
# CLAUDE.md's C4: this is composition of already-computed pieces, never a second
# implementation of validate_pattern or the evaluator.
# --------------------------------------------------------------------------------------


async def _stored_overrides(
    config_service: ConfigService, pair_id: uuid.UUID
) -> list[SelectionOverride]:
    rows = [
        *await config_service.list_selection_overrides(pair_id, RuleScope.OBJECT),
        *await config_service.list_selection_overrides(pair_id, RuleScope.DATASET),
    ]
    return [SelectionOverride.from_row(row) for row in rows]


async def _stored_rules(config_service: ConfigService, pair_id: uuid.UUID) -> list[SelectionRule]:
    rows = [
        *await config_service.list_selection_rules(pair_id, RuleScope.OBJECT),
        *await config_service.list_selection_rules(pair_id, RuleScope.DATASET),
    ]
    return [SelectionRule.from_row(row) for row in rows]


async def _stored_rule_set(config_service: ConfigService, pair_id: uuid.UUID) -> SelectionRuleSet:
    """The pair's stored, saved rule set -- what the browse route always evaluates, and
    what the preview route evaluates when ``PreviewRequest.rules`` is unset."""
    rules = await _stored_rules(config_service, pair_id)
    overrides = await _stored_overrides(config_service, pair_id)
    return SelectionRuleSet.build(rules, overrides)


def _draft_rules(draft: list[DraftSelectionRuleIn]) -> list[SelectionRule]:
    """Build in-memory rules from an unsaved draft. Constructing ``SelectionRule``
    itself calls :func:`~qlabs_catalog_sync.selection.rules.validate_pattern` in its own
    ``__post_init__`` -- the same validation T12.4's create-rule route runs, not a
    second copy of it (see the module docstring)."""
    return [
        SelectionRule(
            rule_id=f"draft-{index}",
            ordinal=index,
            scope=item.scope,
            decision=item.decision,
            matcher_kind=item.matcher_kind,
            pattern=item.pattern,
        )
        for index, item in enumerate(draft)
    ]


async def _build_preview_rule_set(
    config_service: ConfigService, pair_id: uuid.UUID, payload: PreviewRequest
) -> tuple[SelectionRuleSet, Literal["stored", "draft"]]:
    overrides = await _stored_overrides(config_service, pair_id)
    rule_set_source: Literal["stored", "draft"] = "stored" if payload.rules is None else "draft"

    try:
        rules = (
            await _stored_rules(config_service, pair_id)
            if payload.rules is None
            else _draft_rules(payload.rules)
        )
        return SelectionRuleSet.build(rules, overrides), rule_set_source
    except ValueError as exc:
        # Constructing a draft SelectionRule already runs validate_pattern in its own
        # __post_init__ (see _draft_rules's docstring) -- this is what turns that same
        # validation into the 422 T12.4's create-rule route would give the identical
        # pattern, rather than an unhandled 500.
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "preview_rule_set_invalid",
            str(exc),
        ) from exc


# --------------------------------------------------------------------------------------
# The source connector -- built and torn down once per request, exactly the way
# routes/endpoints.py's _run_connector_healthcheck and cli/wiring.py's
# build_connector_pool each do (see the module docstring)
# --------------------------------------------------------------------------------------


async def _require_source_endpoint(config_service: ConfigService, pair: SyncPairRow) -> EndpointRow:
    endpoint = await config_service.get_endpoint(pair.source)
    if endpoint is None:
        # Guarded against by the endpoints.source foreign key (ondelete="RESTRICT"); kept
        # as a defensive, honest error rather than an unguarded KeyError if that
        # invariant is ever weakened.
        raise SyncPairEndpointError(
            f"sync pair {pair.name!r}: source endpoint {pair.source!r} no longer exists"
        )
    if not endpoint.enabled:
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "source_endpoint_disabled",
            f"source endpoint {endpoint.name!r} is registered but disabled; enable it "
            "before browsing or previewing this pair",
            entity=endpoint.name,
        )
    return endpoint


@contextlib.asynccontextmanager
async def _open_source_connector(
    registry: ConnectorRegistry,
    endpoint: EndpointRow,
    *,
    backend_factory: Callable[[SecretRef], SecretBackend],
) -> AsyncIterator[Connector]:
    """``setup()`` a fresh connector instance for ``endpoint`` and yield it, closing it
    on the way out either way. Never returns a partially-usable connector: every failure
    from here on is a mapped :class:`~qlabs_catalog_sync.api.errors.APIError`, never a
    bare exception -- see the module docstring's error-mapping section. A
    :class:`~qlabs_catalog_sync.discovery.ConnectorLookupError` is deliberately *not*
    caught here: ``registry.get_connector`` runs before the ``try`` block below starts,
    so a lookup failure reaches ``api/errors.py``'s own handlers unchanged rather than
    being folded into this function's "the source would not connect" mapping.
    """
    connector_cls = registry.get_connector(endpoint.connector)
    config_model_cls = cast(type[ConnectorConfig], connector_cls.ConfigModel)

    connector: Connector | None = None
    try:
        kwargs: dict[str, Any]
        if endpoint.secret_ref is not None:
            ref = SecretRef.parse(endpoint.secret_ref)
            kwargs = resolve_connector_kwargs(
                ref, config_model_cls, settings=endpoint.settings, backend=backend_factory(ref)
            )
            locator = ref.locator
        else:
            kwargs = dict(endpoint.settings)
            locator = endpoint.name
        connector_config = config_model_cls.for_endpoint(locator, **kwargs)

        connector = connector_cls()
        ctx = ConnectorContext.build(config=connector_config, endpoint=endpoint.name)
        await connector.setup(ctx)
        yield connector
    except ConnectorError as exc:
        # exc.message is documented safe to surface (qlabs_catalog_sync_sdk.exceptions's
        # own module docstring) -- mirrors routes/endpoints.py's own handling exactly.
        raise APIError(
            status.HTTP_502_BAD_GATEWAY,
            "source_unreachable",
            exc.message,
            entity=endpoint.name,
        ) from exc
    except (SecretNotFoundError, SecretRefFormatError) as exc:
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "source_credential_invalid",
            str(exc),
            field="secret_ref",
            entity=endpoint.name,
        ) from exc
    except APIError:
        raise
    except (asyncio.CancelledError, TimeoutError):
        # A wall-clock timeout (see SOURCE_TREE_TIMEOUT_SECONDS/PREVIEW_TIMEOUT_SECONDS)
        # cancels whichever await is in flight, possibly right here. Never intercepted
        # into a 502 -- the caller's own asyncio.timeout() converts this into a 504 with
        # an actionable message; re-raising unchanged is what lets that happen.
        raise
    except Exception as exc:
        correlation_id = str(uuid.uuid4())
        _LOG.error(
            "preview.source_setup_unexpected_error",
            endpoint=endpoint.name,
            connector=endpoint.connector,
            correlation_id=correlation_id,
            error_type=type(exc).__name__,
        )
        raise APIError(
            status.HTTP_502_BAD_GATEWAY,
            "source_unreachable",
            f"could not use source endpoint {endpoint.name!r}: unexpected error "
            f"(correlation id {correlation_id}); see server logs",
            entity=endpoint.name,
        ) from exc
    finally:
        if connector is not None:
            with contextlib.suppress(Exception):
                await connector.close()


# --------------------------------------------------------------------------------------
# Node -> wire-shape conversion -- copies fields off real SelectionResult/
# DatasetSelection objects, computes nothing (see the module docstring)
# --------------------------------------------------------------------------------------


def _none_if_unknown(name: QualifiedName) -> str | None:
    return None if name is UNKNOWN else name


def _selection_result_out(result: SelectionResult) -> SelectionResultOut:
    return SelectionResultOut(
        decision=result.decision,
        included=result.included,
        source=result.source,
        rule_id=result.rule_id,
        explain=result.explain(),
        undetermined=[
            UndeterminedRuleOut(
                matcher_kind=item.rule.matcher_kind,
                pattern=item.rule.pattern,
                decision=item.rule.decision,
                missing=item.missing,
                explain=item.explain(),
            )
            for item in result.undetermined
        ],
    )


def _node_out(node: SourceTreeNode) -> SchemaNodeOut | DatasetNodeOut:
    if isinstance(node, SchemaNode):
        return SchemaNodeOut(
            object_id=node.candidate.object_id,
            qualified_name=_none_if_unknown(node.candidate.qualified_name),
            display_name=node.candidate.display_name,
            result=_selection_result_out(node.result),
        )
    return DatasetNodeOut(
        object_id=node.candidate.object_id,
        qualified_name=_none_if_unknown(node.candidate.qualified_name),
        display_name=node.candidate.display_name,
        selection=DatasetSelectionOut(
            included=node.selection.included,
            explain=node.selection.explain(),
            parent=_selection_result_out(node.selection.parent),
            dataset=_selection_result_out(node.selection.dataset),
        ),
    )


def _dataset_deciding_rule_id(selection: DatasetSelection) -> str | None:
    """The rule id that actually decided this dataset -- the same three cases
    :class:`~qlabs_catalog_sync.selection.source_tree.DatasetSelection` documents (and
    ``explain()`` narrates), so the id shown always names the same rule the text does:
    the parent's, when the parent excluded it or the dataset merely inherited the
    parent's inclusion; the dataset's own, only when a dataset-scope override or rule
    actually fired."""
    if not selection.parent.included:
        return selection.parent.rule_id
    if selection.dataset.source is DecisionSource.DEFAULT:
        return selection.parent.rule_id
    return selection.dataset.rule_id


# --------------------------------------------------------------------------------------
# Browse: one lazy page over the stored rule set
# --------------------------------------------------------------------------------------


@dataclass
class _Page:
    nodes: list[SourceTreeNode] = field(default_factory=list)
    has_more: bool = False


async def _collect_page(
    source: Connector,
    rule_set: SelectionRuleSet,
    *,
    entity_types: tuple[EntityType, ...] | None,
    resolve_tags: bool,
    resolve_owners: bool,
    offset: int,
    limit: int,
) -> _Page:
    page = _Page()
    seen = 0
    node_iter = (
        walk_source_tree(source, rule_set, resolve_tags=resolve_tags, resolve_owners=resolve_owners)
        if entity_types is None
        else walk_source_tree(
            source,
            rule_set,
            entity_types=entity_types,
            resolve_tags=resolve_tags,
            resolve_owners=resolve_owners,
        )
    )
    async for node in node_iter:
        if seen < offset:
            seen += 1
            continue
        if len(page.nodes) >= limit:
            page.has_more = True
            break
        page.nodes.append(node)
        seen += 1
    return page


# --------------------------------------------------------------------------------------
# Preview: counts plus a bounded sample over the whole tree (or up to max_candidates)
# --------------------------------------------------------------------------------------


@dataclass
class _ScopeTally:
    total: int = 0
    included: int = 0
    excluded: int = 0
    undetermined: int = 0

    def to_out(self) -> ScopeCountsOut:
        return ScopeCountsOut(
            total=self.total,
            included=self.included,
            excluded=self.excluded,
            undetermined=self.undetermined,
        )


@dataclass
class _PreviewRun:
    counts: PreviewCountsOut
    sample: list[PreviewSampleItemOut]
    candidates_examined: int
    truncated: bool


async def _run_preview(
    source: Connector,
    rule_set: SelectionRuleSet,
    *,
    resolve_tags: bool,
    resolve_owners: bool,
    sample_limit: int,
    max_candidates: int,
) -> _PreviewRun:
    tallies: dict[RuleScope, _ScopeTally] = {scope: _ScopeTally() for scope in RuleScope}
    sample: list[PreviewSampleItemOut] = []
    examined = 0
    truncated = False

    async for node in walk_source_tree(
        source, rule_set, resolve_tags=resolve_tags, resolve_owners=resolve_owners
    ):
        if examined >= max_candidates:
            truncated = True
            break
        examined += 1

        scope: RuleScope
        included: bool
        has_undetermined: bool
        explain: str
        rule_id: str | None
        if isinstance(node, SchemaNode):
            scope = RuleScope.OBJECT
            included = node.result.included
            has_undetermined = node.result.has_undetermined
            explain = node.result.explain()
            rule_id = node.result.rule_id
        else:
            scope = RuleScope.DATASET
            included = node.selection.included
            has_undetermined = (
                node.selection.dataset.has_undetermined or node.selection.parent.has_undetermined
            )
            explain = node.selection.explain()
            rule_id = _dataset_deciding_rule_id(node.selection)

        tally = tallies[scope]
        tally.total += 1
        if included:
            tally.included += 1
        else:
            tally.excluded += 1
        if has_undetermined:
            tally.undetermined += 1

        if len(sample) < sample_limit:
            sample.append(
                PreviewSampleItemOut(
                    scope=scope,
                    object_id=node.candidate.object_id,
                    qualified_name=_none_if_unknown(node.candidate.qualified_name),
                    display_name=node.candidate.display_name,
                    included=included,
                    explain=explain,
                    rule_id=rule_id,
                    has_undetermined=has_undetermined,
                )
            )

    return _PreviewRun(
        counts=PreviewCountsOut(
            object=tallies[RuleScope.OBJECT].to_out(),
            dataset=tallies[RuleScope.DATASET].to_out(),
        ),
        sample=sample,
        candidates_examined=examined,
        truncated=truncated,
    )


# --------------------------------------------------------------------------------------
# The router
# --------------------------------------------------------------------------------------

#: A module-level singleton rather than a ``Query(...)`` call in an argument default
#: (ruff B008), mirroring ``routes/selection.py``'s own ``_SCOPE_QUERY``.
_OPTIONAL_SCOPE_QUERY: Final = Query(
    default=None, description="Restrict to one scope; omit for both, schemas before datasets"
)
_OFFSET_QUERY: Final = Query(default=0, ge=0)
_LIMIT_QUERY: Final = Query(
    default=DEFAULT_SOURCE_TREE_PAGE_SIZE, ge=1, le=MAX_SOURCE_TREE_PAGE_SIZE
)
_RESOLVE_TAGS_QUERY: Final = Query(
    default=False, description="Read each node's real tags (one extra read() per node)"
)
_RESOLVE_OWNERS_QUERY: Final = Query(
    default=False, description="Read each node's real owners (one extra read() per node)"
)


def build_preview_router(config_service: ConfigService, registry: ConnectorRegistry) -> APIRouter:
    """Build the ``/pairs/{pair_id}/source-tree`` and ``/pairs/{pair_id}/preview``
    router over an already-built ``config_service``/``registry``.

    Mirrors ``api.routes.endpoints.build_endpoints_router``'s shape: a factory taking
    its dependencies explicitly, called once from
    :func:`~qlabs_catalog_sync.api.app.create_app`. Shares the ``/pairs`` path prefix
    with ``routes/pairs.py`` and ``routes/selection.py`` without conflict -- this module
    owns only ``/pairs/{pair_id}/source-tree`` and ``/pairs/{pair_id}/preview``.
    """
    router = APIRouter(prefix="/pairs", tags=["preview"])

    @router.get(
        "/{pair_id}/source-tree",
        response_model=SourceTreePageOut,
        responses=API_ERROR_RESPONSES,
        summary="Browse one page of a pair's source tree against its stored rules (C4, C5)",
    )
    async def browse_source_tree(
        pair_id: uuid.UUID,
        scope: RuleScope | None = _OPTIONAL_SCOPE_QUERY,
        offset: int = _OFFSET_QUERY,
        limit: int = _LIMIT_QUERY,
        resolve_tags: bool = _RESOLVE_TAGS_QUERY,
        resolve_owners: bool = _RESOLVE_OWNERS_QUERY,
    ) -> SourceTreePageOut:
        pair = await config_service.get_sync_pair(pair_id)
        if pair is None:
            raise SyncPairNotFoundError(pair_id)
        endpoint = await _require_source_endpoint(config_service, pair)
        rule_set = await _stored_rule_set(config_service, pair_id)
        entity_types = _ENTITY_TYPES_BY_SCOPE[scope] if scope is not None else None

        try:
            async with asyncio.timeout(SOURCE_TREE_TIMEOUT_SECONDS):
                async with _open_source_connector(
                    registry, endpoint, backend_factory=config_service.secret_backend_for
                ) as source:
                    page = await _collect_page(
                        source,
                        rule_set,
                        entity_types=entity_types,
                        resolve_tags=resolve_tags,
                        resolve_owners=resolve_owners,
                        offset=offset,
                        limit=limit,
                    )
        except TimeoutError as exc:
            raise APIError(
                status.HTTP_504_GATEWAY_TIMEOUT,
                "source_tree_timeout",
                f"browsing source endpoint {endpoint.name!r} did not complete within "
                f"{SOURCE_TREE_TIMEOUT_SECONDS:.0f}s",
                entity=endpoint.name,
            ) from exc

        return SourceTreePageOut(
            nodes=[_node_out(node) for node in page.nodes],
            offset=offset,
            limit=limit,
            has_more=page.has_more,
            next_offset=(offset + len(page.nodes)) if page.has_more else None,
        )

    @router.post(
        "/{pair_id}/preview",
        response_model=PreviewOut,
        responses=API_ERROR_RESPONSES,
        summary="Evaluate stored or draft rules to counts and a sample (C3, C4)",
    )
    async def preview(pair_id: uuid.UUID, payload: PreviewRequest) -> PreviewOut:
        pair = await config_service.get_sync_pair(pair_id)
        if pair is None:
            raise SyncPairNotFoundError(pair_id)
        endpoint = await _require_source_endpoint(config_service, pair)
        rule_set, rule_set_source = await _build_preview_rule_set(config_service, pair_id, payload)

        try:
            async with asyncio.timeout(PREVIEW_TIMEOUT_SECONDS):
                async with _open_source_connector(
                    registry, endpoint, backend_factory=config_service.secret_backend_for
                ) as source:
                    run = await _run_preview(
                        source,
                        rule_set,
                        resolve_tags=payload.resolve_tags,
                        resolve_owners=payload.resolve_owners,
                        sample_limit=payload.sample_limit,
                        max_candidates=payload.max_candidates,
                    )
        except TimeoutError as exc:
            raise APIError(
                status.HTTP_504_GATEWAY_TIMEOUT,
                "preview_timeout",
                f"previewing source endpoint {endpoint.name!r} did not complete within "
                f"{PREVIEW_TIMEOUT_SECONDS:.0f}s",
                entity=endpoint.name,
            ) from exc

        return PreviewOut(
            rule_set_source=rule_set_source,
            counts=run.counts,
            sample=run.sample,
            candidates_examined=run.candidates_examined,
            truncated=run.truncated,
        )

    return router
