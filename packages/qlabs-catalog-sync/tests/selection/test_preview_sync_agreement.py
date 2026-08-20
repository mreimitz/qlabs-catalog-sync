"""The preview and the sync must reach the same decision, or the preview is a lie (C4).

Certification, not unit testing. T11.1 owns the evaluator, T11.2 owns the source tree,
T11.3 owns the sync loop -- and the property that matters spans all three, so no single
task's test suite is the right place for it. This module is that place: it drives the
**preview path** (:func:`~qlabs_catalog_sync.selection.source_tree.walk_source_tree`, what
the console's preview route calls) and the **sync path**
(:func:`~qlabs_catalog_sync.selection.source_tree.select_dataset_change`, what the sync
loop calls per change) over the *same* source and the *same* rule set, and asserts they
agree -- on the decision, and on which rule produced it.

Decision C4: *"A preview that can disagree with the run it predicts is worse than no
preview, so there is exactly one code path."* One shared code path is necessary but not
sufficient: the two callers reach it with differently-obtained arguments, and that is
where they can still come apart.

The one place they genuinely can, and the constraint that closes it
-------------------------------------------------------------------

Both paths compose a dataset's decision from its parent schema's decision (C5). They
obtain the parent differently, and cannot do otherwise:

* the **preview** walks schemas first and uses each schema's *real* evaluated result --
  built from the schema's own change, so it carries the schema's real stable id;
* the **sync** sees one dataset change at a time with no tree and no browse API, so it
  derives the parent from the dataset's qualified name (``catalog.schema.table`` ->
  ``catalog.schema``). The derived parent's ``object_id`` *is* that name; there is no
  stable schema id to be had from a lone dataset change.

:meth:`~qlabs_catalog_sync.selection.rules.SelectionRuleSet.override_for` tries a
candidate's stable id first and its qualified name second. So an object-scope override
pinned on a schema's **qualified name** is found by both paths, and one pinned on a
schema's **opaque stable id** is found only by the preview.

**The constraint this pins: an object-scope override must be pinned by qualified name.**
That is what ``configstore/models.py`` already documents ``selection_overrides.object_id``
as holding for object scope ("a ``catalog.schema`` for object scope"), so honouring it
costs nothing -- but nothing enforced it, and the failure would have been silent and
one-sided. :func:`test_an_object_scope_override_pinned_by_opaque_id_is_the_divergence`
holds that divergence still and fails the day it stops being the *only* one, so whoever
adds an override-creation route (T12.4) or an override UI (T13.5) writes the qualified
name and does not have to rediscover this the hard way.
"""

from __future__ import annotations

from selection_helpers import exclude, include

from qlabs_catalog_sync.configstore.types import RuleScope, SelectionDecision
from qlabs_catalog_sync.selection import SelectionOverride, SelectionRuleSet
from qlabs_catalog_sync.selection.source_tree import (
    DatasetNode,
    DatasetSelection,
    SchemaNode,
    iter_object_changes,
    select_dataset_change,
    walk_source_tree,
)
from qlabs_catalog_sync_sdk.models import DataProduct, Dataset, EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector

#: A metastore shaped like the C3 worked example, plus a second catalog that must stay out.
_SCHEMAS = [
    "analytics.sales",
    "analytics.staging",
    "analytics.prod_staging",
    "finance.reporting",
]
_DATASETS = [
    "analytics.sales.orders",
    "analytics.sales.returns",
    "analytics.staging.scratch",
    "analytics.prod_staging.snapshots",
    "finance.reporting.ledger",
]


def _seeded_source() -> FakeConnector:
    source = FakeConnector.read_only_source()
    for name in _SCHEMAS:
        source.seed(DataProduct(name=name.rsplit(".", 1)[-1]), native_key=name)
    for name in _DATASETS:
        source.seed(Dataset(name=name.rsplit(".", 1)[-1]), native_key=name)
    return source


def _c3_worked_example(*overrides: SelectionOverride) -> SelectionRuleSet:
    """C3's own example: everything in analytics, except staging, but keep prod_staging."""
    return SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="all-analytics"),
            exclude(1, "analytics.staging*", rule_id="no-staging"),
            include(2, "analytics.prod_staging", rule_id="keep-prod-staging"),
        ],
        overrides=list(overrides),
    )


async def _preview(source: FakeConnector, rule_set: SelectionRuleSet) -> dict[str, DatasetNode]:
    """The console's path: walk the tree, keep the dataset nodes by qualified name."""
    return {
        node.candidate.qualified_name: node
        async for node in walk_source_tree(source, rule_set)
        if isinstance(node, DatasetNode) and node.candidate.qualified_name is not None
    }


async def _sync(
    source: FakeConnector, rule_set: SelectionRuleSet
) -> dict[str, DatasetSelection]:
    """The sync loop's path: one dataset change at a time, no tree."""
    decisions: dict[str, DatasetSelection] = {}
    async for change in iter_object_changes(source, EntityType.DATASET):
        selection = select_dataset_change(rule_set, change)
        decisions[change.ref.native_key] = selection
    return decisions


async def test_preview_and_sync_agree_on_every_dataset_in_the_c3_worked_example() -> None:
    """The headline property: same source, same rules, same answers, same reasons."""
    source = _seeded_source()
    rule_set = _c3_worked_example()

    preview = await _preview(source, rule_set)
    sync = await _sync(_seeded_source(), rule_set)

    assert set(preview) == set(sync) == set(_DATASETS)
    for name in _DATASETS:
        previewed = preview[name].selection
        synced = sync[name]
        assert previewed.included == synced.included, name
        assert previewed.dataset.rule_id == synced.dataset.rule_id, name
        assert previewed.parent.decision == synced.parent.decision, name


async def test_the_agreement_is_not_vacuous_the_example_actually_splits_the_metastore() -> None:
    """A test asserting two empty sets agree proves nothing. Pin the real split."""
    source = _seeded_source()
    preview = await _preview(source, _c3_worked_example())

    included = {name for name, node in preview.items() if node.selection.included}
    assert included == {
        "analytics.sales.orders",
        "analytics.sales.returns",
        "analytics.prod_staging.snapshots",
    }
    # excluded by its parent's rule, not by any dataset rule
    scratch = preview["analytics.staging.scratch"].selection
    assert not scratch.included
    assert scratch.parent.rule_id == "no-staging"
    # a different catalog entirely: no rule matched, so the default (exclude) decided
    ledger = preview["finance.reporting.ledger"].selection
    assert not ledger.included
    assert ledger.parent.rule_id is None


async def test_preview_and_sync_agree_when_a_schema_is_pinned_by_qualified_name() -> None:
    """An object-scope override pinned the documented way is honoured by BOTH paths.

    ``configstore/models.py`` documents ``selection_overrides.object_id`` as holding "a
    ``catalog.schema`` for object scope". Pinned that way, the preview finds it on the
    schema candidate's qualified name and the sync finds it on the derived parent's
    ``object_id`` -- so carving a whole schema back in (or out) predicts the run exactly.
    """
    pin = SelectionOverride(
        scope=RuleScope.OBJECT,
        object_id="finance.reporting",
        decision=SelectionDecision.INCLUDE,
        reason="regulatory reporting is in scope by exception",
    )
    rule_set = _c3_worked_example(pin)

    preview = await _preview(_seeded_source(), rule_set)
    sync = await _sync(_seeded_source(), rule_set)

    ledger = "finance.reporting.ledger"
    assert preview[ledger].selection.included
    assert sync[ledger].included
    assert preview[ledger].selection.parent.override == pin
    assert sync[ledger].parent.override == pin


async def test_an_object_scope_override_pinned_by_opaque_id_is_the_divergence() -> None:
    """The one place the two paths still differ, held still so it cannot spread.

    A lone dataset change carries no reference to its schema's stable id, so the sync path
    can only ever pin a parent by name. If an override is pinned on an opaque schema id,
    the preview honours it and the sync does not -- the preview would promise a sync that
    does not happen.

    This test asserts that divergence *exists* rather than pretending it does not, and it
    is the reason the module docstring states the constraint: object-scope overrides are
    written by qualified name. If a future change makes both paths agree here, this test
    fails and should be deleted along with the constraint -- a failure here is good news.
    """
    # One schema is keyed the way Databricks really keys: an opaque, rename-proof id in
    # native_key rather than the dotted name.
    opaque = FakeConnector.read_only_source()
    for name in _SCHEMAS:
        key = "8f14e45f-ea8d-4b1c-9f6a-2c3d4e5f6a7b" if name == "finance.reporting" else name
        opaque.seed(DataProduct(name=name.rsplit(".", 1)[-1]), native_key=key)
    for name in _DATASETS:
        opaque.seed(Dataset(name=name.rsplit(".", 1)[-1]), native_key=name)

    pin = SelectionOverride(
        scope=RuleScope.OBJECT,
        object_id="8f14e45f-ea8d-4b1c-9f6a-2c3d4e5f6a7b",
        decision=SelectionDecision.INCLUDE,
    )
    rule_set = _c3_worked_example(pin)

    schemas = {
        node.candidate.object_id: node
        async for node in walk_source_tree(
            opaque, rule_set, entity_types=(EntityType.DATA_PRODUCT,)
        )
        if isinstance(node, SchemaNode)
    }
    # The preview DOES honour the opaque pin, because it evaluates the schema's own change.
    assert schemas["8f14e45f-ea8d-4b1c-9f6a-2c3d4e5f6a7b"].result.included

    # The sync path, given only the dataset, cannot see it -- and falls back to the rules,
    # which exclude finance entirely. This is the documented, contained divergence.
    sync = await _sync(_seeded_source(), rule_set)
    assert not sync["finance.reporting.ledger"].included
