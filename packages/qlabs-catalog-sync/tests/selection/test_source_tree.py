"""T11.2 -- the lazy source-tree provider: paging, the full_name rule, and the one C5 join.

Every test that exercises paging or reading goes through a real
:class:`~qlabs_catalog_sync_sdk.testing.FakeConnector`, never a hand-rolled mock, so the
contract's actual watermark/paging semantics are what is being tested. Tests about the
`full_name`-vs-`native_key` rule build a :class:`~qlabs_catalog_sync_sdk.contract.ChangeRef`
by hand instead: ``FakeConnector.seed`` has no way to populate ``secondary_keys`` (it builds
every ``IdentityRef`` itself), so the one thing a real connector actually does that
triggered the original defect -- carrying the dotted name in ``secondary_keys["full_name"]``
while keying on an unrelated stable id -- has to be constructed directly here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from selection_helpers import dataset_candidate, exclude, include, schema_candidate

from qlabs_catalog_sync.selection import (
    UNKNOWN,
    MatcherKind,
    RuleScope,
    SelectionRuleSet,
    evaluate,
    source_tree,
)
from qlabs_catalog_sync.selection.source_tree import (
    DatasetNode,
    DatasetSelection,
    SchemaNode,
    candidate_for_change,
    compose_dataset_selection,
    iter_object_changes,
    owners_offered,
    parent_schema_candidate,
    select_dataset_change,
    tags_offered,
    walk_source_tree,
)
from qlabs_catalog_sync_sdk.contract import ChangeRef
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    Dataset,
    EntityType,
    IdentityRef,
    Party,
    PartyRole,
    Tag,
)
from qlabs_catalog_sync_sdk.testing import FakeConnector
from qlabs_catalog_sync_sdk.testing.manifests import databricks_shaped_manifest

#: Restricts a walk to schema (object-scope) nodes only -- used throughout to keep tests
#: that do not care about datasets from having to page the (empty) dataset stream too.
_SCHEMA_ONLY = (EntityType.DATA_PRODUCT,)

# --------------------------------------------------------------------------------------
# candidate_for_change: the full_name rule (the defect this design exists to prevent)
# --------------------------------------------------------------------------------------


def test_candidate_for_change_prefers_full_name_over_a_uuid_native_key() -> None:
    """The pilot defect, as a test on this module's own candidate-building.

    Databricks keys a schema on ``schema_id`` -- a UUID, because names are renameable --
    and carries the dotted ``catalog.schema`` name in ``secondary_keys["full_name"]``. The
    old D1 filter matched the pattern against ``native_key`` alone, found no dot in the
    UUID, and silently selected everything. Here the UUID never becomes the qualified name.
    """
    change = ChangeRef(
        ref=IdentityRef(
            endpoint="fake-source",
            entity_type=EntityType.DATA_PRODUCT,
            native_key="6f6b8e0e-uuid",
            tenant_id="fake-tenant",
            secondary_keys={"full_name": "analytics.sales"},
        )
    )
    candidate = candidate_for_change(change, scope=RuleScope.OBJECT)

    assert candidate.object_id == "6f6b8e0e-uuid"
    assert candidate.qualified_name == "analytics.sales"

    rule_set = SelectionRuleSet.build([include(0, "analytics.*", rule_id="analytics-only")])
    result = evaluate(rule_set, candidate)
    assert result.included
    assert result.rule_id == "analytics-only"


def test_candidate_for_change_never_selects_on_a_pattern_matched_against_the_uuid() -> None:
    """The blast-radius version: a pair scoped to one catalog must not sync another one
    just because a UUID native key happened not to look like the excluded catalog."""
    change = ChangeRef(
        ref=IdentityRef(
            endpoint="fake-source",
            entity_type=EntityType.DATA_PRODUCT,
            native_key="9a1c2d3e-uuid",
            tenant_id="fake-tenant",
            secondary_keys={"full_name": "other_catalog.sales"},
        )
    )
    candidate = candidate_for_change(change, scope=RuleScope.OBJECT)
    rule_set = SelectionRuleSet.build([include(0, "analytics.*", rule_id="analytics-only")])
    assert not evaluate(rule_set, candidate).included


def test_candidate_for_change_falls_back_to_native_key_when_it_is_already_dot_shaped() -> None:
    """A connector with no ``secondary_keys`` at all (or a test double) still works, as
    long as its own ``native_key`` is genuinely shaped like this scope's qualified name."""
    change = ChangeRef(
        ref=IdentityRef(
            endpoint="fake-source",
            entity_type=EntityType.DATASET,
            native_key="analytics.sales.orders",
            tenant_id="fake-tenant",
        )
    )
    candidate = candidate_for_change(change, scope=RuleScope.DATASET)
    assert candidate.qualified_name == "analytics.sales.orders"


def test_candidate_for_change_is_unknown_not_guessed_when_neither_key_is_dot_shaped() -> None:
    """An opaque key -- no ``full_name``, and a ``native_key`` with the wrong shape for this
    scope -- must become :data:`UNKNOWN`, never a best-effort guess."""
    change = ChangeRef(
        ref=IdentityRef(
            endpoint="fake-source",
            entity_type=EntityType.DATASET,
            native_key="tbl-9f2c",
            tenant_id="fake-tenant",
            secondary_keys={"full_name": "not-a-three-part-name"},
        )
    )
    candidate = candidate_for_change(change, scope=RuleScope.DATASET)
    assert candidate.qualified_name is UNKNOWN
    assert candidate.object_id == "tbl-9f2c"


# --------------------------------------------------------------------------------------
# parent_schema_candidate + compose_dataset_selection: the one C5 join, at the unit level
# --------------------------------------------------------------------------------------


def test_parent_schema_candidate_derives_catalog_dot_schema_from_the_dataset_name() -> None:
    parent = parent_schema_candidate(dataset_candidate("analytics.sales.orders"))
    assert parent.scope is RuleScope.OBJECT
    assert parent.qualified_name == "analytics.sales"
    assert parent.object_id == "analytics.sales"


def test_parent_schema_candidate_stays_unknown_for_an_unnamed_dataset() -> None:
    opaque = dataset_candidate(UNKNOWN, object_id="qlik-opaque-handle")
    parent = parent_schema_candidate(opaque)
    assert parent.qualified_name is UNKNOWN
    # Still a usable candidate: evaluate() fails it closed rather than raising.
    result = evaluate(SelectionRuleSet.build([include(0, "*.*", rule_id="all")]), parent)
    assert not result.included
    assert result.has_undetermined


def test_parent_schema_candidate_rejects_an_object_scope_candidate() -> None:
    with pytest.raises(ValueError, match="dataset-scope"):
        parent_schema_candidate(schema_candidate("analytics.sales"))


def test_compose_dataset_selection_rejects_an_object_scope_candidate() -> None:
    rule_set = SelectionRuleSet.build()
    candidate = schema_candidate("analytics.sales")
    with pytest.raises(ValueError, match="dataset-scope"):
        compose_dataset_selection(rule_set, candidate, parent=evaluate(rule_set, candidate))


def test_dataset_selection_rejects_a_parent_of_the_wrong_scope() -> None:
    rule_set = SelectionRuleSet.build()
    dataset_result = evaluate(rule_set, dataset_candidate("analytics.sales.orders"))
    with pytest.raises(ValueError, match="object-scope"):
        DatasetSelection(parent=dataset_result, dataset=dataset_result)


def test_a_dataset_whose_parent_is_included_obeys_its_own_dataset_scope_rules() -> None:
    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="schemas"),
            include(0, "analytics.sales.*", scope=RuleScope.DATASET, rule_id="tables"),
        ]
    )
    parent = evaluate(rule_set, schema_candidate("analytics.sales"))
    selection = compose_dataset_selection(
        rule_set, dataset_candidate("analytics.sales.orders"), parent=parent
    )
    assert selection.included
    assert selection.dataset.rule_id == "tables"


def test_a_dataset_whose_parent_is_excluded_is_excluded_no_matter_what_its_own_rule_says() -> None:
    rule_set = SelectionRuleSet.build(
        [
            exclude(0, "analytics.staging", rule_id="no-staging"),
            include(0, "analytics.staging.*", scope=RuleScope.DATASET, rule_id="would-include"),
        ]
    )
    parent = evaluate(rule_set, schema_candidate("analytics.staging"))
    selection = compose_dataset_selection(
        rule_set, dataset_candidate("analytics.staging.audit_log"), parent=parent
    )
    assert not parent.included
    # The dataset's own rule really did match -- proving the exclusion is the join
    # overriding it, not a coincidence of the dataset rules being absent.
    assert selection.dataset.included
    assert selection.dataset.rule_id == "would-include"
    assert not selection.included
    # The reported reason names the *schema's* exclusion -- its rule's id and pattern
    # text -- never the dataset rule that actually matched.
    assert selection.parent.rule_id == "no-staging"
    explanation = selection.explain()
    assert "excluded by rule #0 exclude glob 'analytics.staging'" in explanation
    assert "'analytics.staging.*'" not in explanation


def test_select_dataset_change_is_compose_dataset_selection_plus_the_derived_parent() -> None:
    """T11.3's exact entry point is not a parallel implementation of the join: it is
    candidate_for_change + parent_schema_candidate + evaluate + compose_dataset_selection,
    called in that order and nothing else."""
    change = ChangeRef(
        ref=IdentityRef(
            endpoint="fake-source",
            entity_type=EntityType.DATASET,
            native_key="analytics.sales.orders",
            tenant_id="fake-tenant",
        )
    )
    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="schemas"),
            include(0, "analytics.sales.*", scope=RuleScope.DATASET, rule_id="tables"),
        ]
    )

    via_wrapper = select_dataset_change(rule_set, change)

    dataset = candidate_for_change(change, scope=RuleScope.DATASET)
    parent = evaluate(rule_set, parent_schema_candidate(dataset))
    via_pieces = compose_dataset_selection(rule_set, dataset, parent=parent)

    assert via_wrapper == via_pieces
    assert via_wrapper.included


def test_select_dataset_change_excludes_a_dataset_under_an_excluded_parent() -> None:
    change = ChangeRef(
        ref=IdentityRef(
            endpoint="fake-source",
            entity_type=EntityType.DATASET,
            native_key="analytics.staging.audit_log",
            tenant_id="fake-tenant",
        )
    )
    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="all-analytics"),
            exclude(1, "analytics.staging", rule_id="no-staging"),
            include(0, "analytics.staging.*", scope=RuleScope.DATASET, rule_id="would-include"),
        ]
    )
    selection = select_dataset_change(rule_set, change)
    assert not selection.included
    assert not selection.parent.included
    assert selection.parent.rule_id == "no-staging"


# --------------------------------------------------------------------------------------
# tags_offered / owners_offered -- manifest-driven availability
# --------------------------------------------------------------------------------------


def test_tags_offered_follows_the_manifests_d6_gate() -> None:
    assert tags_offered(databricks_shaped_manifest(has_sql_warehouse=True), EntityType.DATA_PRODUCT)
    assert not tags_offered(
        databricks_shaped_manifest(has_sql_warehouse=False), EntityType.DATA_PRODUCT
    )


def test_owners_offered_is_false_for_an_entity_type_that_never_declares_the_field() -> None:
    manifest = databricks_shaped_manifest()
    assert owners_offered(manifest, EntityType.DATA_PRODUCT)
    # The Databricks-shaped manifest never mentions `owners` for DATASET at all.
    assert not owners_offered(manifest, EntityType.DATASET)


def test_field_availability_is_false_for_an_unsupported_entity_type() -> None:
    manifest = databricks_shaped_manifest()
    assert not tags_offered(manifest, EntityType.GLOSSARY_TERM)


# --------------------------------------------------------------------------------------
# Laziness: paging only ever fetches what the consumer actually asked for
# --------------------------------------------------------------------------------------


async def test_iter_object_changes_only_fetches_the_page_a_consumer_needs() -> None:
    source = FakeConnector.read_only_source(list_changed_page_size=2)
    for i in range(5):
        source.seed(DataProduct(name=f"Product {i}"))

    stream = iter_object_changes(source, EntityType.DATA_PRODUCT)
    await anext(stream)
    await anext(stream)
    assert source.call_count("list_changed") == 1  # two nodes, one (of three) pages

    await anext(stream)
    assert source.call_count("list_changed") == 2


async def test_walk_source_tree_schema_half_is_lazy() -> None:
    source = FakeConnector.read_only_source(list_changed_page_size=2)
    for i in range(5):
        source.seed(DataProduct(name=f"Product {i}"))
    rule_set = SelectionRuleSet.build()

    stream = walk_source_tree(source, rule_set, entity_types=_SCHEMA_ONLY)
    await anext(stream)
    await anext(stream)
    assert source.call_count("list_changed") == 1


async def test_walk_source_tree_never_reads_by_default() -> None:
    source = FakeConnector.read_only_source()
    source.seed(DataProduct(name="Sales"), native_key="analytics.sales")
    source.seed(Dataset(name="Orders"), native_key="analytics.sales.orders")
    rule_set = SelectionRuleSet.build([include(0, "analytics.*", rule_id="all")])

    nodes = [node async for node in walk_source_tree(source, rule_set)]

    assert len(nodes) == 2
    assert source.call_count("read") == 0


# --------------------------------------------------------------------------------------
# No watermark is ever persisted or advanced in shared state
# --------------------------------------------------------------------------------------


def test_source_tree_module_never_imports_the_state_store() -> None:
    """Structural guarantee: this module cannot reach a StateStore because it never
    imports one -- there is no code path left to persist a watermark from."""
    tree = ast.parse(Path(source_tree.__file__).read_text())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any("state" in name.split(".") for name in imported_modules), imported_modules


async def test_walking_the_tree_twice_yields_the_same_nodes_both_times() -> None:
    """If a walk had persisted or advanced a watermark, a second walk over the same
    connector would see less (or nothing) the second time. It must see exactly the same
    set both times, because it always starts from Watermark.initial and never writes."""
    source = FakeConnector.read_only_source()
    source.seed(DataProduct(name="Sales"), native_key="analytics.sales")
    source.seed(DataProduct(name="Finance"), native_key="analytics.finance")
    rule_set = SelectionRuleSet.build([include(0, "analytics.*", rule_id="all")])

    def _ids(nodes: list[source_tree.SourceTreeNode]) -> set[str]:
        return {node.candidate.object_id for node in nodes}

    first_pass = [
        node
        async for node in walk_source_tree(source, rule_set, entity_types=_SCHEMA_ONLY)
    ]
    second_pass = [
        node
        async for node in walk_source_tree(source, rule_set, entity_types=_SCHEMA_ONLY)
    ]

    assert _ids(first_pass) == _ids(second_pass) == {"analytics.sales", "analytics.finance"}
    assert [n.included for n in first_pass] == [n.included for n in second_pass]


# --------------------------------------------------------------------------------------
# Composition end-to-end, through the real tree (C5)
# --------------------------------------------------------------------------------------


def _rule_set_for_composition() -> SelectionRuleSet:
    return SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="obj-all-analytics"),
            exclude(1, "analytics.staging", rule_id="obj-no-staging"),
            include(0, "analytics.sales.*", scope=RuleScope.DATASET, rule_id="ds-sales-all"),
            exclude(1, "analytics.sales.tmp*", scope=RuleScope.DATASET, rule_id="ds-no-tmp"),
            include(2, "analytics.staging.*", scope=RuleScope.DATASET, rule_id="ds-staging-all"),
        ]
    )


async def _walk_composition_fixture() -> list[SchemaNode | DatasetNode]:
    source = FakeConnector.read_only_source()
    source.seed(DataProduct(name="Sales"), native_key="analytics.sales")
    source.seed(DataProduct(name="Staging"), native_key="analytics.staging")
    source.seed(Dataset(name="Orders"), native_key="analytics.sales.orders")
    source.seed(Dataset(name="Tmp Orders"), native_key="analytics.sales.tmp_orders")
    source.seed(Dataset(name="Audit Log"), native_key="analytics.staging.audit_log")

    rule_set = _rule_set_for_composition()
    return [node async for node in walk_source_tree(source, rule_set)]


async def test_a_dataset_inside_an_included_schema_obeys_its_own_dataset_scope_rules() -> None:
    nodes = await _walk_composition_fixture()
    by_id = {n.candidate.object_id: n for n in nodes}

    orders = by_id["analytics.sales.orders"]
    assert isinstance(orders, DatasetNode)
    assert orders.included
    assert orders.selection.dataset.rule_id == "ds-sales-all"

    tmp_orders = by_id["analytics.sales.tmp_orders"]
    assert isinstance(tmp_orders, DatasetNode)
    assert not tmp_orders.included
    assert tmp_orders.selection.parent.included  # parent (analytics.sales) is included
    assert tmp_orders.selection.dataset.rule_id == "ds-no-tmp"  # its own rule excluded it


async def test_a_dataset_inside_an_excluded_schema_is_excluded_regardless_of_its_own_rule() -> None:
    nodes = await _walk_composition_fixture()
    by_id = {n.candidate.object_id: n for n in nodes}

    audit_log = by_id["analytics.staging.audit_log"]
    assert isinstance(audit_log, DatasetNode)

    # The dataset's own rule really does match -- if the join were skipped this would be
    # included. It must not be.
    assert audit_log.selection.dataset.included
    assert audit_log.selection.dataset.rule_id == "ds-staging-all"
    assert not audit_log.included

    # The explanation names the schema's exclusion -- its rule's id and pattern text --
    # never the dataset rule that actually matched.
    assert audit_log.selection.parent.rule_id == "obj-no-staging"
    explanation = audit_log.selection.explain()
    assert "excluded by rule #1 exclude glob 'analytics.staging'" in explanation
    assert "'analytics.staging.*'" not in explanation


# --------------------------------------------------------------------------------------
# The dishonest case: the tree cannot disagree with itself, or with evaluate() directly
# --------------------------------------------------------------------------------------


async def test_walking_the_tree_agrees_with_calling_evaluate_directly() -> None:
    source = FakeConnector.read_only_source()
    names = ["analytics.sales", "analytics.staging", "finance.reporting"]
    for name in names:
        source.seed(DataProduct(name=name), native_key=name)

    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="all-analytics"),
            exclude(1, "analytics.staging*", rule_id="no-staging"),
        ]
    )
    nodes = [
        node
        async for node in walk_source_tree(source, rule_set, entity_types=_SCHEMA_ONLY)
    ]
    assert len(nodes) == 3

    for node in nodes:
        assert isinstance(node, SchemaNode)
        # Reconstruct the candidate independently, rather than reusing node.candidate, so
        # this also exercises candidate_for_change's own derivation, not just evaluate().
        object_id = node.candidate.object_id
        independent = schema_candidate(object_id, object_id=object_id)
        direct = evaluate(rule_set, independent)
        assert node.result.decision == direct.decision
        assert node.result.rule_id == direct.rule_id


async def test_a_datasets_parent_decision_matches_the_schemas_own_real_decision() -> None:
    """The failure this design exists to prevent: a dataset's parent decision drifting
    from what the tree's own schema node -- built for the very same schema, in the very
    same walk -- actually decided.

    Set up so the two paths would disagree if the tree ever derived a dataset's parent
    purely from the dataset's own name (the no-tree fallback ``select_dataset_change`` must
    use) instead of the schema's real, already-evaluated result: the schema carries a real
    tag that only a resolved read reveals, and only the real schema candidate can see it.
    """
    source = FakeConnector.read_only_source()
    source.seed(
        DataProduct(name="Sales", tags=[Tag(key="restricted")]), native_key="analytics.sales"
    )
    source.seed(Dataset(name="Orders"), native_key="analytics.sales.orders")

    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="all-schemas"),
            exclude(1, "restricted", matcher_kind=MatcherKind.TAG, rule_id="no-restricted"),
        ]
    )

    nodes = [node async for node in walk_source_tree(source, rule_set, resolve_tags=True)]
    schema_node = next(n for n in nodes if isinstance(n, SchemaNode))
    dataset_node = next(n for n in nodes if isinstance(n, DatasetNode))

    assert not schema_node.included
    assert schema_node.result.rule_id == "no-restricted"

    # The dataset's parent must be *exactly* the schema's own real result, not a
    # re-derived approximation that never saw the real tag.
    assert dataset_node.selection.parent == schema_node.result
    assert not dataset_node.included
    assert dataset_node.selection.parent.rule_id == "no-restricted"
    assert "exclude tag 'restricted'" in dataset_node.selection.explain()


# --------------------------------------------------------------------------------------
# Tags unavailable: manifest-driven, not probed, and honestly reported (D6)
# --------------------------------------------------------------------------------------


def _tag_rule_set() -> SelectionRuleSet:
    return SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="all-analytics"),
            exclude(1, "pii", matcher_kind=MatcherKind.TAG, rule_id="no-pii"),
        ]
    )


def _only_schema_node(nodes: list[source_tree.SourceTreeNode]) -> SchemaNode:
    """Unpack a single-node walk result, asserting it is a schema node -- every walk in
    this section is restricted to ``_SCHEMA_ONLY``, so this also narrows the type for
    mypy without every call site repeating the same isinstance check."""
    (node,) = nodes
    assert isinstance(node, SchemaNode)
    return node


async def test_a_manifest_with_no_tags_makes_the_tag_rule_undetermined_not_a_non_match() -> None:
    manifest = databricks_shaped_manifest(has_sql_warehouse=False)
    source = FakeConnector.read_only_source(manifest=manifest)
    source.seed(DataProduct(name="Sales", tags=[Tag(key="pii")]), native_key="analytics.sales")

    nodes = [
        node
        async for node in walk_source_tree(
            source, _tag_rule_set(), entity_types=_SCHEMA_ONLY, resolve_tags=True
        )
    ]
    node = _only_schema_node(nodes)

    # Same decision a rule set with no tag rule at all would reach (the broad include).
    assert node.included
    assert node.result.rule_id == "all-analytics"
    # But the *result* says the tag rule could not be evaluated -- it did not silently
    # answer "no pii".
    assert node.result.has_undetermined
    assert "tags unknown" in node.result.explain()
    # D6: never probed by reading, because the manifest already said `na`.
    assert source.call_count("read") == 0


async def test_a_manifest_offering_tags_lets_resolve_tags_reach_a_real_decision() -> None:
    manifest = databricks_shaped_manifest(has_sql_warehouse=True)
    source = FakeConnector.read_only_source(manifest=manifest)
    source.seed(DataProduct(name="Sales", tags=[Tag(key="pii")]), native_key="analytics.sales")

    nodes = [
        node
        async for node in walk_source_tree(
            source, _tag_rule_set(), entity_types=_SCHEMA_ONLY, resolve_tags=True
        )
    ]
    node = _only_schema_node(nodes)

    assert not node.included
    assert node.result.rule_id == "no-pii"
    assert not node.result.has_undetermined
    assert source.call_count("read") == 1


async def test_resolve_tags_is_opt_in_default_walk_leaves_the_rule_undetermined() -> None:
    manifest = databricks_shaped_manifest(has_sql_warehouse=True)
    source = FakeConnector.read_only_source(manifest=manifest)
    source.seed(DataProduct(name="Sales", tags=[Tag(key="pii")]), native_key="analytics.sales")

    nodes = [
        node
        async for node in walk_source_tree(source, _tag_rule_set(), entity_types=_SCHEMA_ONLY)
    ]
    node = _only_schema_node(nodes)

    assert node.included  # same decision either way
    assert node.result.has_undetermined  # but never read, so still undetermined
    assert source.call_count("read") == 0


# --------------------------------------------------------------------------------------
# Owners: how they are learned at all -- opt-in, manifest-gated, same mechanism as tags
# --------------------------------------------------------------------------------------


async def test_owner_resolution_is_opt_in_and_manifest_gated() -> None:
    manifest = databricks_shaped_manifest()
    source = FakeConnector.read_only_source(manifest=manifest)
    source.seed(
        DataProduct(name="Sales", owners=[Party(email="a@acme.com", role=PartyRole.OWNER)]),
        native_key="analytics.sales",
    )
    rule_set = SelectionRuleSet.build(
        [include(0, "*@acme.com", matcher_kind=MatcherKind.OWNER, rule_id="acme-owned")]
    )

    without_resolution = _only_schema_node(
        [node async for node in walk_source_tree(source, rule_set, entity_types=_SCHEMA_ONLY)]
    )
    assert not without_resolution.included
    assert without_resolution.result.has_undetermined

    with_resolution = _only_schema_node(
        [
            node
            async for node in walk_source_tree(
                source, rule_set, entity_types=_SCHEMA_ONLY, resolve_owners=True
            )
        ]
    )
    assert with_resolution.included
    assert with_resolution.result.rule_id == "acme-owned"


async def test_owner_resolution_never_reads_when_the_manifest_never_declares_the_field() -> None:
    """Databricks-shaped datasets never declare `owners` at all -- opting in must not
    trigger a read that cannot possibly produce the fact."""
    manifest = databricks_shaped_manifest()
    source = FakeConnector.read_only_source(manifest=manifest)
    source.seed(Dataset(name="Orders"), native_key="analytics.sales.orders")

    nodes = [
        node
        async for node in walk_source_tree(
            source,
            SelectionRuleSet.build(),
            entity_types=(EntityType.DATASET,),
            resolve_owners=True,
        )
    ]
    assert len(nodes) == 1
    assert source.call_count("read") == 0


async def test_resolving_both_tags_and_owners_for_one_node_costs_exactly_one_read() -> None:
    manifest = databricks_shaped_manifest(has_sql_warehouse=True)
    source = FakeConnector.read_only_source(manifest=manifest)
    source.seed(
        DataProduct(
            name="Sales",
            tags=[Tag(key="pii")],
            owners=[Party(email="a@acme.com", role=PartyRole.OWNER)],
        ),
        native_key="analytics.sales",
    )

    nodes = [
        node
        async for node in walk_source_tree(
            source,
            SelectionRuleSet.build(),
            entity_types=_SCHEMA_ONLY,
            resolve_tags=True,
            resolve_owners=True,
        )
    ]
    assert len(nodes) == 1
    assert source.call_count("read") == 1
    assert nodes[0].candidate.tags == (Tag(key="pii"),)
    assert nodes[0].candidate.owners == (Party(email="a@acme.com", role=PartyRole.OWNER),)


# --------------------------------------------------------------------------------------
# A vanished object: deleted changes are never read, and a read that races one is absorbed
# --------------------------------------------------------------------------------------


async def test_a_vanished_object_yields_unknown_facts_without_crashing_the_walk() -> None:
    """FakeConnector's changelog is append-only, so a seed-then-vanish sequence, listed
    from initial, surfaces *two* changes for the same key: the original create (now stale
    -- the object is already gone by the time this walk tries to read it) and the deletion
    itself. Neither is ever successfully read: the deletion is skipped outright (nothing to
    read, per :func:`~qlabs_catalog_sync.selection.source_tree._resolve_facts`), and the
    stale create races a ``NotFound`` this module must absorb rather than let crash the
    whole walk -- a real full scan over a real source can hit exactly the same race if an
    object is dropped between listing it and reading it.
    """
    manifest = databricks_shaped_manifest(has_sql_warehouse=True)
    source = FakeConnector.read_only_source(manifest=manifest)
    ref = source.seed(
        DataProduct(name="Sales", tags=[Tag(key="pii")]), native_key="analytics.sales"
    )
    source.vanish(ref)

    nodes = [
        node
        async for node in walk_source_tree(
            source,
            SelectionRuleSet.build(),
            entity_types=_SCHEMA_ONLY,
            resolve_tags=True,
        )
    ]

    assert len(nodes) == 2
    assert all(node.candidate.tags is UNKNOWN for node in nodes)
    # One attempted (and failed) read for the stale create; the deletion itself is never
    # attempted at all.
    assert source.call_count("read") == 1
