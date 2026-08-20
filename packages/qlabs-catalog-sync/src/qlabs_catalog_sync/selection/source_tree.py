"""Lazy source-tree provider: enumerate source candidates and decorate each with its
selection decision and the deciding rule (C4, C5).

WP11 / T11.2. This module is the only place that turns the connector contract's flat,
per-entity-type ``list_changed`` streams into the two-level *tree* decision C5 describes:
object scope decides which ``catalog.schema`` become data products, dataset scope decides
which tables and views *inside a selected schema* become that product's members. Neither
:mod:`.rules` nor :mod:`.evaluator` know about a parent -- :func:`~.evaluator.evaluate`
answers exactly one candidate at a time, by design (see ``evaluator.py``'s module
docstring, "composition across scopes is the caller's job"). This is that caller, and the
only one: the sync loop (T11.3) goes through the functions here rather than joining schema
and dataset decisions a second time, which is what would let a preview and a run disagree
even though the evaluator itself cannot.

There is no browse API
----------------------

:class:`~qlabs_catalog_sync_sdk.contract.Connector` has no "list children of this schema"
method. Its read path is exactly ``list_changed(entity_type, since) -> ListChangedResult``
(paged, watermark-driven) and ``read(ref) -> NeutralEntity``. So "everything currently at
the source" means calling ``list_changed`` from ``Watermark.initial(endpoint,
entity_type)`` and following ``has_more`` -- there is no other way to enumerate a schema's
tables than to list *every* table the connector reports and recognize the ones whose
qualified name starts with that schema's.

**This module never touches the state store, and cannot even if it tried** -- it does not
import anything from ``qlabs_catalog_sync.state``. Every ``since`` it hands a connector is
built fresh, in-process, from :meth:`Watermark.initial`; every ``next_watermark`` a
connector returns is used purely as this walk's own local resume token to fetch the next
page and is discarded the moment paging finishes. The sync pair's real watermark -- the one
that controls what the next real run processes -- lives in the state store and is never
read or written here. A console preview built on this module can run at any time, as often
as an operator likes, without disturbing what the next real sync cycle will do.
``test_source_tree.py`` pins this two ways: a structural check that this module's imports
never reach the state store, and a behavioral one -- walking the same source twice yields
the same nodes both times, which would not be true if the first walk had consumed
something.

Laziness
--------

:func:`iter_object_changes` and :func:`walk_source_tree` are async generators that fetch a
page only when the consumer asks for the node after the one most recently fetched. Browsing
a large catalog therefore costs one page, not the whole catalog, until the consumer actually
asks for more.

The schema-then-dataset composition, and the two shapes that need it
----------------------------------------------------------------------

:func:`compose_dataset_selection` is the one join C5 requires: a dataset under an excluded
parent is excluded no matter what dataset-scope rules say (:class:`DatasetSelection.included`
checks the parent first; :meth:`DatasetSelection.explain` names the parent's exclusion, never
a dataset rule, when that is what decided). Under an *included* parent, a dataset-scope
override or matching rule decides as normal, and a dataset that matches no dataset-scope rule
at all **inherits its parent's inclusion** rather than falling to dataset scope's own default
-- see :class:`DatasetSelection`'s docstring for why this is required by C3 (D1's flat
selector synced every table under a selected schema with no dataset-level configuration) and
why it does not reopen object scope's own default-exclude.

Two callers need this join and they do not hold the same information, so they cannot supply
its ``parent`` argument the same way:

* :func:`walk_source_tree` walks every schema first (a full, lazy pass over
  ``list_changed(DATA_PRODUCT, ...)``), evaluates each with :func:`~.evaluator.evaluate`,
  and keeps the *real* :class:`~.evaluator.SelectionResult` for every qualified name it saw
  -- real stable id, real tags/owners if resolved. It then walks datasets and looks each
  one's parent up in that map before calling :func:`compose_dataset_selection`.
* T11.3's sync loop processes one entity type per cycle (RS-07) and, for a ``DATASET``
  cycle, sees one :class:`~qlabs_catalog_sync_sdk.contract.ChangeRef` at a time with no tree
  at all -- it never walked the schema stream in this cycle and has nothing to look up.
  :func:`select_dataset_change` is its entry point: it derives a *parent schema candidate*
  from the dataset's own qualified name (``catalog.schema.table`` -> ``catalog.schema``,
  via :func:`parent_schema_candidate`), evaluates that, and calls the exact same
  :func:`compose_dataset_selection`.

The join itself -- :func:`compose_dataset_selection` -- is one function, called by both.
What legitimately differs is how each caller obtains its ``parent`` argument, because their
inputs differ in kind (a tree has parents to walk; a lone ``ChangeRef`` does not), not
because the join was reimplemented. This has one honest, unavoidable consequence worth
knowing before relying on it: :func:`parent_schema_candidate`'s derived candidate has no
stable id for the schema (nothing in a dataset's own change reveals it) and no tags/owners
(nothing in a dataset's own change reveals those either), so from :func:`select_dataset_change`
an object-scope override pinned on the schema's *stable id* -- the kind that survives a
rename -- will not be found, only one pinned by qualified name will, and an object-scope
tag/owner rule is reported undetermined rather than guessed. :func:`walk_source_tree` does
not share this limitation, because it evaluates every schema from its own real change.

Tag and owner availability
---------------------------

:func:`tags_offered` / :func:`owners_offered` ask the source's capability manifest whether a
field is declared at all for an entity type and, if so, whether its mode is anything other
than ``na`` (RM-01 decision D6: Databricks ``tags`` is ``ro`` with a SQL warehouse
configured, ``na`` without one). This is the *only* way availability is decided -- never by
reading one object and seeing whether tags come back, which would be exactly the kind of
guess the whole selection design exists to avoid. When a field is not offered, every
candidate for that entity type carries :data:`~.rules.UNKNOWN` for it, which is what makes
every tag/owner rule against it land in :attr:`~.evaluator.SelectionResult.undetermined`
rather than silently reading as "no".

Neither ``ChangeRef`` nor ``list_changed`` carries field values -- only ``read()`` does. So
even when a field *is* offered, this module reports :data:`~.rules.UNKNOWN` for it **by
default**: getting a real value would mean a ``read()`` per node, and doing that
unconditionally would defeat the laziness the DoD requires (a preview over a large catalog
would turn into reading the entire catalog). ``walk_source_tree``'s ``resolve_tags`` /
``resolve_owners`` parameters make that cost explicit and opt-in: when set, and only for
entity types whose manifest actually offers the field, one ``read()`` per node fills in both
facts (never two reads for one node when both are requested). Left at their default
(``False``), no ``read()`` is ever issued by a walk -- only ``list_changed``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from qlabs_catalog_sync.selection import (
    SEGMENTS_BY_SCOPE,
    UNKNOWN,
    DecisionSource,
    OwnerFacts,
    QualifiedName,
    RuleScope,
    SelectionCandidate,
    SelectionResult,
    SelectionRuleSet,
    TagFacts,
    evaluate,
)
from qlabs_catalog_sync_sdk.contract import (
    CapabilityManifestBase,
    ChangeRef,
    Connector,
    Watermark,
)
from qlabs_catalog_sync_sdk.exceptions import NotFound
from qlabs_catalog_sync_sdk.manifest import CapabilityManifest, FieldCapability, FieldCapabilityMode
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    Dataset,
    EntityType,
    IdentityRef,
    NeutralEntity,
)

__all__ = [
    "DatasetNode",
    "DatasetSelection",
    "SchemaNode",
    "SourceTreeNode",
    "candidate_for_change",
    "compose_dataset_selection",
    "iter_object_changes",
    "owners_offered",
    "parent_schema_candidate",
    "select_dataset_change",
    "tags_offered",
    "walk_source_tree",
]


# --------------------------------------------------------------------------------------
# Building a candidate from one ChangeRef (the full_name rule, rules.py's boundary note)
# --------------------------------------------------------------------------------------


def _is_shaped(name: str, expected_segments: int) -> bool:
    """True when ``name`` splits on ``.`` into exactly ``expected_segments`` non-empty parts."""
    parts = name.split(".")
    return len(parts) == expected_segments and all(parts)


def _qualified_name_from_ref(ref: IdentityRef, *, scope: RuleScope) -> QualifiedName:
    """The dotted name a candidate of ``scope`` should match rules against, or :data:`UNKNOWN`.

    ``secondary_keys["full_name"]`` first, ``native_key`` only as a fallback and only when
    it is actually shaped like a ``scope`` qualified name -- never a guess. This is the
    fix for the defect ``rules.py``'s module docstring describes: Databricks keys a schema
    on the stable ``schema_id`` UUID and carries the dotted ``catalog.schema`` name in
    ``secondary_keys["full_name"]``; matching the pattern against ``native_key`` alone found
    no dot and silently selected everything. Preferring ``full_name`` and falling back to
    ``native_key`` only when it is genuinely dot-shaped for this scope means a UUID native
    key is never mistaken for a name, and a connector that *does* key on the qualified name
    (or a test double with no ``secondary_keys`` at all) still works.
    """
    expected = SEGMENTS_BY_SCOPE[scope]
    full_name = ref.secondary_keys.get("full_name")
    if full_name and _is_shaped(full_name, expected):
        return full_name
    if _is_shaped(ref.native_key, expected):
        return ref.native_key
    return UNKNOWN


def candidate_for_change(
    change: ChangeRef,
    *,
    scope: RuleScope,
    tags: TagFacts = UNKNOWN,
    owners: OwnerFacts = UNKNOWN,
) -> SelectionCandidate:
    """Build the :class:`~.rules.SelectionCandidate` for one ``change``.

    ``object_id`` is always ``change.ref.native_key`` -- the connector's own stable
    identifier, per :class:`~.rules.SelectionCandidate`'s contract, regardless of whether it
    happens to look like a dotted name. ``qualified_name`` is derived separately by
    :func:`_qualified_name_from_ref`, which is the only place that decides between
    ``full_name`` and ``native_key``.
    """
    return SelectionCandidate(
        scope=scope,
        object_id=change.ref.native_key,
        qualified_name=_qualified_name_from_ref(change.ref, scope=scope),
        tags=tags,
        owners=owners,
        display_name=change.display_name,
    )


# --------------------------------------------------------------------------------------
# Tag / owner availability -- manifest-driven, never probed by reading (D6)
# --------------------------------------------------------------------------------------


def _field_capability(
    manifest: CapabilityManifestBase, entity_type: EntityType, field: str
) -> FieldCapability | None:
    """The declared capability for one field, or ``None`` when it is unknowable.

    :class:`~qlabs_catalog_sync_sdk.contract.CapabilityManifestBase` -- the type
    ``Connector.capabilities()`` is actually typed to return -- only answers the three
    questions the contract itself needs (``supports``, ``is_writable``,
    ``requires_full_replace``); it does not expose per-field read availability at all. The
    concrete :class:`~qlabs_catalog_sync_sdk.manifest.CapabilityManifest` every real and fake
    connector actually builds does, via ``field_capability``. Narrowing with ``isinstance``
    rather than ``getattr`` keeps this fully typed -- no bare manifest ever answers "yes,
    offered" by construction.
    """
    if not isinstance(manifest, CapabilityManifest):
        return None
    return manifest.field_capability(entity_type, field)


def _field_offered(manifest: CapabilityManifestBase, entity_type: EntityType, field: str) -> bool:
    if not manifest.supports(entity_type):
        return False
    capability = _field_capability(manifest, entity_type, field)
    return capability is not None and capability.mode is not FieldCapabilityMode.NA


def tags_offered(manifest: CapabilityManifestBase, entity_type: EntityType) -> bool:
    """True when ``manifest`` declares ``tags`` readable (``ro``/``rw``) for ``entity_type``.

    False when the entity type is unsupported, the field is never mentioned, or it is
    declared ``na`` -- RM-01 decision D6's no-SQL-warehouse case for Databricks ``tags``.
    This is the *only* signal a tag matcher's availability is based on; see the module
    docstring.
    """
    return _field_offered(manifest, entity_type, "tags")


def owners_offered(manifest: CapabilityManifestBase, entity_type: EntityType) -> bool:
    """True when ``manifest`` declares ``owners`` readable (``ro``/``rw``) for ``entity_type``."""
    return _field_offered(manifest, entity_type, "owners")


def _entity_tags(entity: NeutralEntity) -> TagFacts:
    if isinstance(entity, DataProduct | Dataset):
        return tuple(entity.tags)
    return UNKNOWN


def _entity_owners(entity: NeutralEntity) -> OwnerFacts:
    if isinstance(entity, DataProduct | Dataset):
        return tuple(entity.owners)
    return UNKNOWN


async def _resolve_facts(
    source: Connector, change: ChangeRef, *, need_tags: bool, need_owners: bool
) -> tuple[TagFacts, OwnerFacts]:
    """One ``read()`` when a fact is actually wanted and obtainable, ``UNKNOWN`` otherwise.

    Never called to *detect* availability -- ``need_tags``/``need_owners`` are already
    gated on :func:`tags_offered`/:func:`owners_offered` by the caller, so this function
    only ever reads when the manifest already said the field exists. A deleted change has
    nothing to read. One call serves both facts, so asking for tags and owners together on
    the same node costs exactly one ``read()``, not two.

    A full scan lists, then reads, two separate moments -- an object can vanish between
    them even though its own change was not itself reported as a deletion (a poll-based
    listing has no way to report a race it has not observed yet). :class:`~qlabs_catalog_sync_sdk.
    exceptions.NotFound` from that ``read()`` is therefore not this function's business to
    raise onward: it means "nothing to resolve after all", exactly like a change already
    known to be a deletion, so it is treated the same way -- :data:`UNKNOWN`, not a crashed
    walk. Orphan detection for a source object that vanished is the sync loop's job, not a
    read-only preview's.
    """
    if change.is_delete or not (need_tags or need_owners):
        return (UNKNOWN, UNKNOWN)
    try:
        entity = await source.read(change.ref)
    except NotFound:
        return (UNKNOWN, UNKNOWN)
    tags = _entity_tags(entity) if need_tags else UNKNOWN
    owners = _entity_owners(entity) if need_owners else UNKNOWN
    return (tags, owners)


# --------------------------------------------------------------------------------------
# The C5 join -- one function, shared by the tree walk and T11.3's sync loop
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetSelection:
    """C5's two-step decision for one dataset: its parent schema's object-scope result,
    composed with the dataset's own dataset-scope result.

    Three cases, checked in this order:

    1. **The parent schema is excluded.** This dataset is excluded here regardless of
       what ``dataset`` says -- see :attr:`included` and :meth:`explain`, which check
       ``parent`` first, no matter which dataset-scope rule (if any) matched.
    2. **The parent is included and a dataset-scope override or rule decided** --
       ``dataset.source`` is :attr:`~.evaluator.DecisionSource.OVERRIDE` or
       :attr:`~.evaluator.DecisionSource.RULE`. That decision stands as-is.
    3. **The parent is included and nothing dataset-scope matched at all** --
       ``dataset.source`` is :attr:`~.evaluator.DecisionSource.DEFAULT`. The dataset
       *inherits* its parent's inclusion rather than falling to
       :data:`~.rules.DEFAULT_DECISION` a second time.

    Case 3 is what makes decision C3 true for datasets, not just schemas: D1's flat
    ``catalog.schema`` selector synced every table under a selected schema with zero
    dataset-level configuration, so ``object_rules_from_catalog_schema_patterns`` -- one
    include rule per pattern, object scope only -- must keep doing that. Without
    inheritance, a rule set built purely from that converter would select every schema
    and *no* datasets at all: :attr:`~.rules.DEFAULT_DECISION` is exclude, and an
    object-only rule set never matches a dataset-scope candidate, so every dataset would
    fall to the default. That is a real behavior change to the locked mapping, which C3
    forbids, and it is silent -- nothing about "the pattern list still parses" would
    reveal that every table stopped syncing.

    This does **not** reopen dataset scope's own default-exclude, and it does not touch
    :data:`~.rules.DEFAULT_DECISION` at all -- object scope still fails closed, which is
    what keeps the blast radius bounded (the original D1 defect was an *unbounded*
    default-*include* across an entire metastore, not a bounded one). A dataset is only
    ever composed here because its parent schema was already, explicitly included by an
    object-scope rule or override; inheriting *inside* that already-selected schema costs
    nothing outside of it. A dataset-scope rule that actually fires -- an include or an
    exclude -- still decides over the inherited default, exactly like any other override
    of a default: one ``exclude analytics.sales.tmp*`` rule removes only what it matches,
    not every other dataset in the schema (there is no "if this rule set has any
    dataset-scope rules, stop inheriting" special case -- that would make adding one
    exclude rule to one schema silently flip every unrelated dataset everywhere else to
    excluded, which is a far worse cliff than the one this fixes).

    ``dataset`` is always the real evaluated result (never withheld), so a caller that
    wants to know what the dataset's *own* rules concluded, independent of inheritance,
    can still ask.
    """

    parent: SelectionResult
    dataset: SelectionResult

    def __post_init__(self) -> None:
        if self.parent.candidate.scope is not RuleScope.OBJECT:
            raise ValueError("DatasetSelection.parent must be an object-scope SelectionResult")
        if self.dataset.candidate.scope is not RuleScope.DATASET:
            raise ValueError("DatasetSelection.dataset must be a dataset-scope SelectionResult")

    @property
    def _inherits_parent(self) -> bool:
        """True when nothing dataset-scope (no override, no rule) reached a verdict."""
        return self.dataset.source is DecisionSource.DEFAULT

    @property
    def included(self) -> bool:
        """Whether this dataset is selected.

        The parent must be included, full stop. Given that, an override or a matching
        dataset-scope rule decides; with neither, the dataset inherits the parent's
        inclusion (which, having passed the first check, is ``True``) rather than falling
        to dataset scope's own default. See the class docstring for why.
        """
        if not self.parent.included:
            return False
        if self._inherits_parent:
            return True
        return self.dataset.included

    def explain(self) -> str:
        """A one-line, truthful reason for every case the class docstring describes --
        naming the parent's exclusion rather than a dataset rule whenever the parent is
        what decided, and naming the inheritance rather than claiming a default excluded
        it when nothing dataset-scope matched."""
        if not self.parent.included:
            return f"excluded: parent schema not selected ({self.parent.explain()})"
        if self._inherits_parent:
            return f"included: inherits parent schema's selection ({self.parent.explain()})"
        return self.dataset.explain()


def compose_dataset_selection(
    rule_set: SelectionRuleSet,
    dataset: SelectionCandidate,
    *,
    parent: SelectionResult,
) -> DatasetSelection:
    """The one C5 join. Both :func:`walk_source_tree` and T11.3's sync loop (via
    :func:`select_dataset_change`, or directly when it already has ``parent``) call this --
    see the module docstring for why they cannot supply ``parent`` the same way, and why
    that is not a second implementation of the join.
    """
    if dataset.scope is not RuleScope.DATASET:
        raise ValueError(
            "compose_dataset_selection needs a dataset-scope candidate, got "
            f"{dataset.scope.value!r}"
        )
    return DatasetSelection(parent=parent, dataset=evaluate(rule_set, dataset))


def parent_schema_candidate(dataset: SelectionCandidate) -> SelectionCandidate:
    """The best-effort object-scope candidate for ``dataset``'s parent schema, derived
    purely from ``dataset``'s own qualified name (``catalog.schema.table`` ->
    ``catalog.schema``).

    This is what a caller with no tree -- T11.3's sync loop, processing one dataset-scope
    :class:`~qlabs_catalog_sync_sdk.contract.ChangeRef` at a time -- uses to obtain the
    ``parent`` argument :func:`compose_dataset_selection` needs, because a lone dataset
    change carries no reference to its schema's own stable id, tags or owners: there is no
    browse API (see the module docstring), so nothing else supplies them.

    Two honest limitations follow, both worth knowing before relying on this path:

    * the derived candidate's ``object_id`` *is* its qualified name -- there is no stable
      schema id available here -- so a per-object override pinned on the schema's real
      stable id (the kind that survives a rename) will not be found; only one pinned by the
      schema's qualified name will. :func:`walk_source_tree` does not share this limitation:
      it evaluates every schema from its own real change.
    * tags and owners are :data:`UNKNOWN`, because a dataset's own change reveals nothing
      about its schema's tags or owners. Any object-scope tag/owner rule against this
      derived candidate is reported undetermined, honestly, rather than guessed.

    When ``dataset.qualified_name`` is itself :data:`UNKNOWN` (an opaque key with no
    dot-shaped name anywhere), no parent can be derived either; the returned candidate keeps
    :data:`UNKNOWN` as its own qualified name, so every glob rule against it is reported
    undetermined and it falls to the default decision, exactly like any other nameless
    candidate (see ``rules.py``'s module docstring) -- failing closed rather than guessing.
    """
    if dataset.scope is not RuleScope.DATASET:
        raise ValueError(
            f"parent_schema_candidate needs a dataset-scope candidate, got {dataset.scope.value!r}"
        )
    name = dataset.qualified_name
    if name is UNKNOWN:
        return SelectionCandidate(
            scope=RuleScope.OBJECT,
            object_id=f"{dataset.object_id}::unresolved-parent",
            qualified_name=UNKNOWN,
        )
    catalog, schema, _table = name.split(".")
    parent_name = f"{catalog}.{schema}"
    return SelectionCandidate(
        scope=RuleScope.OBJECT, object_id=parent_name, qualified_name=parent_name
    )


def select_dataset_change(
    rule_set: SelectionRuleSet,
    change: ChangeRef,
    *,
    tags: TagFacts = UNKNOWN,
    owners: OwnerFacts = UNKNOWN,
) -> DatasetSelection:
    """T11.3's entry point for one dataset-scope change: the exact call the sync loop makes.

    Builds the dataset's own candidate (:func:`candidate_for_change`, the same ``full_name``
    rule the tree walk uses), derives its parent purely from that candidate's qualified name
    (:func:`parent_schema_candidate`), evaluates the parent, and composes both through
    :func:`compose_dataset_selection` -- the same join :func:`walk_source_tree` uses for
    every dataset node. ``tags``/``owners`` default to :data:`UNKNOWN`; pass real values
    only when the loop already read the entity for another reason -- this function never
    reads anything itself.
    """
    dataset = candidate_for_change(change, scope=RuleScope.DATASET, tags=tags, owners=owners)
    parent = evaluate(rule_set, parent_schema_candidate(dataset))
    return compose_dataset_selection(rule_set, dataset, parent=parent)


# --------------------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemaNode:
    """One object-scope (``catalog.schema``) node of the source tree, decision attached."""

    change: ChangeRef
    candidate: SelectionCandidate
    result: SelectionResult

    @property
    def included(self) -> bool:
        return self.result.included


@dataclass(frozen=True, slots=True)
class DatasetNode:
    """One dataset-scope (table/view) node of the source tree, C5-composed decision attached."""

    change: ChangeRef
    candidate: SelectionCandidate
    selection: DatasetSelection

    @property
    def included(self) -> bool:
        return self.selection.included


SourceTreeNode = SchemaNode | DatasetNode


# --------------------------------------------------------------------------------------
# Lazy paging over one entity type -- the only enumeration primitive the contract offers
# --------------------------------------------------------------------------------------


async def iter_object_changes(
    source: Connector, entity_type: EntityType
) -> AsyncIterator[ChangeRef]:
    """Lazily enumerate every object of ``entity_type`` currently at ``source``.

    Pages through :meth:`~qlabs_catalog_sync_sdk.contract.Connector.list_changed`, starting
    from :meth:`~qlabs_catalog_sync_sdk.contract.Watermark.initial` -- a full scan, which is
    what "browse the source" means when the only read path is a change feed (see the module
    docstring). Fetches page *N* + 1 only once the consumer has asked for the node after the
    last one page *N* yielded.

    ``since`` never comes from, and ``next_watermark`` never goes to, the state store: this
    function does not import it. ``next_watermark`` lives only in this generator's local
    ``since`` variable for as long as paging continues, and is discarded the moment
    ``has_more`` is false. ``source`` must already be set up (``Connector.setup``) --
    lifecycle is the caller's responsibility, not this provider's.
    """
    since = Watermark.initial(source.name, entity_type)
    while True:
        result = await source.list_changed(entity_type, since=since)
        for change in result.changes:
            yield change
        if not result.has_more:
            return
        since = result.next_watermark


async def _schema_nodes(
    source: Connector,
    rule_set: SelectionRuleSet,
    manifest: CapabilityManifestBase,
    *,
    resolve_tags: bool,
    resolve_owners: bool,
) -> AsyncIterator[SchemaNode]:
    need_tags = resolve_tags and tags_offered(manifest, EntityType.DATA_PRODUCT)
    need_owners = resolve_owners and owners_offered(manifest, EntityType.DATA_PRODUCT)
    async for change in iter_object_changes(source, EntityType.DATA_PRODUCT):
        tags, owners = await _resolve_facts(
            source, change, need_tags=need_tags, need_owners=need_owners
        )
        candidate = candidate_for_change(change, scope=RuleScope.OBJECT, tags=tags, owners=owners)
        yield SchemaNode(change=change, candidate=candidate, result=evaluate(rule_set, candidate))


def _parent_result_for(
    dataset: SelectionCandidate,
    schema_results: Mapping[str, SelectionResult],
    rule_set: SelectionRuleSet,
) -> SelectionResult:
    """The parent schema's real :class:`~.evaluator.SelectionResult` when it was seen while
    walking the schema stream, else the best-effort derived one (see
    :func:`parent_schema_candidate`) -- the same fallback :func:`select_dataset_change` uses,
    for a schema this walk's own listing never reported (a data inconsistency at the source,
    not something this module invents an answer for).
    """
    name = dataset.qualified_name
    if name is not UNKNOWN:
        catalog, schema, _table = name.split(".")
        known = schema_results.get(f"{catalog}.{schema}")
        if known is not None:
            return known
    return evaluate(rule_set, parent_schema_candidate(dataset))


async def _dataset_nodes(
    source: Connector,
    rule_set: SelectionRuleSet,
    manifest: CapabilityManifestBase,
    schema_results: Mapping[str, SelectionResult],
    *,
    resolve_tags: bool,
    resolve_owners: bool,
) -> AsyncIterator[DatasetNode]:
    need_tags = resolve_tags and tags_offered(manifest, EntityType.DATASET)
    need_owners = resolve_owners and owners_offered(manifest, EntityType.DATASET)
    async for change in iter_object_changes(source, EntityType.DATASET):
        tags, owners = await _resolve_facts(
            source, change, need_tags=need_tags, need_owners=need_owners
        )
        candidate = candidate_for_change(change, scope=RuleScope.DATASET, tags=tags, owners=owners)
        parent = _parent_result_for(candidate, schema_results, rule_set)
        selection = compose_dataset_selection(rule_set, candidate, parent=parent)
        yield DatasetNode(change=change, candidate=candidate, selection=selection)


# --------------------------------------------------------------------------------------
# The tree
# --------------------------------------------------------------------------------------

_DEFAULT_ENTITY_TYPES: Final[tuple[EntityType, EntityType]] = (
    EntityType.DATA_PRODUCT,
    EntityType.DATASET,
)


async def walk_source_tree(
    source: Connector,
    rule_set: SelectionRuleSet,
    *,
    entity_types: Iterable[EntityType] = _DEFAULT_ENTITY_TYPES,
    resolve_tags: bool = False,
    resolve_owners: bool = False,
) -> AsyncIterator[SourceTreeNode]:
    """The lazy, decorated walk: every schema, then every dataset, each carrying its
    decision and the deciding rule.

    Schemas are walked to completion first, because :func:`compose_dataset_selection`'s
    ``parent`` argument is most accurate when it is the schema's *real* evaluated result
    (real stable id, real tags/owners if resolved) rather than the name-only approximation
    :func:`parent_schema_candidate` falls back to -- see the module docstring on why this
    walk and :func:`select_dataset_change` cannot obtain ``parent`` the same way. Within
    each half, paging is fully lazy (:func:`iter_object_changes`); a consumer that stops
    after the first few schema nodes never pages the dataset stream at all, and never pages
    further into either stream than it asked for.

    ``entity_types`` restricts which half(s) to walk -- pass ``(EntityType.DATA_PRODUCT,)``
    to browse schemas only. ``resolve_tags``/``resolve_owners`` default to ``False``: with
    both left off, this walk never calls anything but ``list_changed`` (see the module
    docstring's tag/owner section for the cost of turning either on).
    """
    wanted = frozenset(entity_types)
    manifest = source.capabilities()
    schema_results: dict[str, SelectionResult] = {}

    if EntityType.DATA_PRODUCT in wanted:
        async for schema_node in _schema_nodes(
            source, rule_set, manifest, resolve_tags=resolve_tags, resolve_owners=resolve_owners
        ):
            if schema_node.candidate.qualified_name is not UNKNOWN:
                schema_results[schema_node.candidate.qualified_name] = schema_node.result
            yield schema_node

    if EntityType.DATASET in wanted:
        async for dataset_node in _dataset_nodes(
            source,
            rule_set,
            manifest,
            schema_results,
            resolve_tags=resolve_tags,
            resolve_owners=resolve_owners,
        ):
            yield dataset_node
