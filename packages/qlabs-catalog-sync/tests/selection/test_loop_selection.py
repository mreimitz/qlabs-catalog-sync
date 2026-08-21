"""T11.3 -- the sync loop resolves scope through the shared evaluator, end to end.

Every test here runs a **real cycle**: a migrated SQLite state store on a temp file, the
real :class:`~qlabs_catalog_sync.identity.IdentityResolver` over it, the real diff engine,
and two :class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` instances whose paging,
watermarks and write semantics are genuine behaviour. Nothing about selection is asserted
by calling :meth:`SyncLoop._not_selected_reason` directly -- the property under test is
which objects a *cycle* writes and which it reports as filtered, and a test that inspected
the predicate could pass while the cycle still synced the wrong catalog.

What is being pinned
--------------------

* **C3's worked example, through a cycle.** Include ``analytics.*``, exclude
  ``analytics.staging*``, keep ``analytics.prod_staging`` -- last match wins, and the
  filtered records name what decided them.
* **D1 equivalence.** C3 says the selector is *widened, not replaced*, so a pair carrying
  only ``catalog_schema_patterns`` must still select exactly what
  :meth:`~qlabs_catalog_sync.config.SyncPairConfig.matches` selects -- asserted against
  that predicate itself, for data products *and* for datasets. The dataset half is the
  loop-level guard for the defect T11.2 found and fixed: an object-scope-only rule set must
  still sync every table under a selected schema (C5's inheritance), and losing that would
  silently stop syncing datasets while nothing about the configuration changed.
* **The ``full_name`` defect cannot come back.** A source keying on an opaque UUID and
  carrying the dotted name in ``secondary_keys["full_name"]`` -- what Databricks really
  does -- scoped to one catalog, does not sync another one.
* **The deliberate behaviour change.** An object whose qualified name cannot be read
  anywhere is now *filtered* where the superseded ``SyncLoop._selects`` included it, and
  the record says the name is what excluded it rather than letting it look like somebody's
  rule.
* **A pair matching nothing is a clean no-op** -- committed, no writes, no errors, and the
  watermark still advances past everything the cycle saw.
* **The dishonest cases.** One test fails if the cycle and the console's preview could
  reach different answers for one rule set (decision C4); one fails if the rule set were
  compiled per change rather than once; one fails if the superseded glob path were still
  reachable at all.

``FakeConnector.seed`` builds every ``IdentityRef`` itself and has no way to populate
``secondary_keys``, so the one thing a real connector does that triggered the original
defect is built by :class:`_OpaqueKeyedSource` below -- a real ``FakeConnector`` with the
listing decorated, not a mock of one.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from selection_helpers import exclude, include

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.selection import (
    MatcherKind,
    RuleScope,
    SelectionDecision,
    SelectionRuleSet,
)
from qlabs_catalog_sync.selection import rules as selection_rules_module
from qlabs_catalog_sync.selection.source_tree import walk_source_tree
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync import loop as loop_module
from qlabs_catalog_sync.sync.loop import (
    RecordOutcome,
    RunStatus,
    SkipReason,
    SyncLoop,
    rule_set_for_pair,
)
from qlabs_catalog_sync_sdk.contract import ListChangedResult, Watermark
from qlabs_catalog_sync_sdk.manifest import CapabilityManifest, EntityCapability, FieldCapability
from qlabs_catalog_sync_sdk.models import Category, DataProduct, Dataset, EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector
from qlabs_catalog_sync_sdk.testing.manifests import (
    databricks_shaped_manifest,
    qlik_shaped_manifest,
)

SOURCE = "fake-source"
TARGET = "fake-target"

WRITE_METHODS = ("create", "update", "delete")

#: A metastore shaped like C3's worked example, plus a catalog that must stay out of scope.
_C3_SCHEMAS = (
    "analytics.sales",
    "analytics.staging",
    "analytics.prod_staging",
    "finance.reporting",
)
_C3_DATASETS = (
    "analytics.sales.orders",
    "analytics.staging.scratch",
    "analytics.prod_staging.snapshots",
    "finance.reporting.ledger",
)


# ======================================================================================
# Fixtures -- everything real except the temp database's location
# ======================================================================================


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[StateStore]:
    """A :class:`StateStore` on a fresh, fully migrated temp-file SQLite database."""
    url = f"sqlite:///{tmp_path / 'state.db'}"
    upgrade_to_head(url)
    state_store = StateStore.from_url(url)
    try:
        yield state_store
    finally:
        await state_store.aclose()


@pytest.fixture
def resolver(store: StateStore, tmp_path: Path) -> IdentityResolver:
    return IdentityResolver(store, review_path=tmp_path / "identity-review.json")


@pytest.fixture
def source() -> FakeConnector:
    """A Databricks-shaped, read-only source connector."""
    return FakeConnector.read_only_source(name=SOURCE)


@pytest.fixture
def target() -> FakeConnector:
    """A Qlik-shaped write target -- the sole write connector in v1."""
    return FakeConnector.write_target(name=TARGET)


def make_pair(
    *,
    patterns: Sequence[str] = ("analytics.*",),
    entity_types: Sequence[EntityType] = (EntityType.DATA_PRODUCT,),
) -> SyncPairConfig:
    """One sync pair. ``patterns`` is D1's flat glob list, which C3 keeps as a special case."""
    return SyncPairConfig(
        name="db-to-qlik",
        source=SOURCE,
        target=TARGET,
        catalog_schema_patterns=list(patterns),
        target_space="Analytics Space",
        entity_types=list(entity_types),
    )


@pytest.fixture
def make_loop(
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
    resolver: IdentityResolver,
) -> Callable[..., SyncLoop]:
    """Build a :class:`SyncLoop` over the fixtures, with any constructor override applied."""

    def factory(**overrides: Any) -> SyncLoop:
        kwargs: dict[str, Any] = {
            "pair": make_pair(),
            "source": source,
            "target": target,
            "store": store,
            "resolver": resolver,
        }
        kwargs.update(overrides)
        return SyncLoop(**kwargs)

    return factory


# ======================================================================================
# Builders
# ======================================================================================


def seed_schema(connector: FakeConnector, full_name: str) -> None:
    """Seed one schema-as-data-product keyed on its ``catalog.schema`` name."""
    connector.seed(DataProduct(name=full_name.rsplit(".", 1)[-1]), native_key=full_name)


def seed_table(connector: FakeConnector, full_name: str) -> None:
    """Seed one table-as-dataset keyed on its ``catalog.schema.table`` name."""
    connector.seed(Dataset(name=full_name.rsplit(".", 1)[-1]), native_key=full_name)


def c3_worked_example() -> SelectionRuleSet:
    """C3's own example: everything in analytics, except staging, but keep prod_staging."""
    return SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="all-analytics"),
            exclude(1, "analytics.staging*", rule_id="no-staging"),
            include(2, "analytics.prod_staging", rule_id="keep-prod-staging"),
        ]
    )


def outcomes(report: Any) -> dict[str, RecordOutcome]:
    """``native key -> outcome`` for every record in a run report."""
    return {record.native_key: record.outcome for record in report.records}


def details(report: Any) -> dict[str, str | None]:
    """``native key -> detail`` for every record in a run report."""
    return {record.native_key: record.detail for record in report.records}


def considered(*reports: Any) -> set[str]:
    """Every native key the cycle did *not* filter out -- what selection let through."""
    return {
        record.native_key
        for report in reports
        for record in report.records
        if record.outcome is not RecordOutcome.FILTERED
    }


def write_calls(connector: FakeConnector) -> list[str]:
    """Every recorded write-path call, in order. Must stay empty for a filtered-only run."""
    return [entry.method for entry in connector.call_log if entry.method in WRITE_METHODS]


def read_keys(connector: FakeConnector) -> list[str]:
    """The native keys the loop actually issued a ``read`` for."""
    return [call.args["ref"].native_key for call in connector.calls("read")]


class _OpaqueKeyedSource(FakeConnector):
    """A source that keys on a stable id and carries the dotted name in ``full_name``.

    Exactly what Databricks does: schemas and tables are keyed on a rename-proof
    ``schema_id`` / ``table_id`` UUID, and the ``catalog.schema[.table]`` name travels in
    ``IdentityRef.secondary_keys["full_name"]``. ``FakeConnector`` builds every
    ``IdentityRef`` itself and cannot populate ``secondary_keys``, so this decorates the
    listing it produced -- the paging, watermarks and reads underneath are still the real
    ``FakeConnector``, not a mock.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.full_names: dict[str, str] = {}

    def seed_opaque(self, entity: Any, *, object_id: str, full_name: str) -> None:
        """Seed ``entity`` under an opaque ``object_id``, reachable by name only via
        ``secondary_keys``."""
        self.seed(entity, native_key=object_id)
        self.full_names[object_id] = full_name

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        result = await super().list_changed(entity_type, since)
        return result.model_copy(
            update={
                "changes": [
                    change.model_copy(
                        update={
                            "ref": change.ref.model_copy(
                                update={
                                    "secondary_keys": {
                                        "full_name": self.full_names[change.ref.native_key]
                                    }
                                }
                            )
                        }
                    )
                    for change in result.changes
                ]
            }
        )


def _manifest_with_categories(base: CapabilityManifest) -> CapabilityManifest:
    """``base`` plus a supported, entirely read-only ``category`` entity.

    Categories are a neutral entity type with no write path anywhere in v1, which makes
    them the honest way to reach the loop's "this entity type has no selection scope"
    branch: both connectors declare support, so the cycle is scheduled and really lists,
    and nothing is writable, so nothing can be written whatever selection decides.
    """
    return CapabilityManifest(
        entities={
            **base.entities,
            EntityType.CATEGORY: EntityCapability(
                supported=True,
                identity_keys=["full_name"],
                fields={"name": FieldCapability.ro(), "description": FieldCapability.ro()},
            ),
        },
        concurrency=base.concurrency,
    )


# ======================================================================================
# C3's worked example, driven through a real cycle
# ======================================================================================


async def test_the_c3_worked_example_decides_a_real_cycle(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, target: FakeConnector
) -> None:
    """Include analytics, carve out staging, put prod_staging back -- last match wins.

    Asserted on what the cycle *did*: two data products really created at the target, two
    reported filtered, and neither filtered object read at all.
    """
    for name in _C3_SCHEMAS:
        seed_schema(source, name)

    report = await make_loop(selection_rules=c3_worked_example(), create_missing=True).run_cycle(
        EntityType.DATA_PRODUCT
    )

    assert outcomes(report) == {
        "analytics.sales": RecordOutcome.CREATED,
        "analytics.prod_staging": RecordOutcome.CREATED,
        "analytics.staging": RecordOutcome.FILTERED,
        "finance.reporting": RecordOutcome.FILTERED,
    }
    # The writes are real, and only for the two included schemas.
    assert sorted(call.args["entity"].name for call in target.calls("create")) == [
        "prod_staging",
        "sales",
    ]
    # A filtered object is never even read.
    assert sorted(read_keys(source)) == ["analytics.prod_staging", "analytics.sales"]
    assert report.status is RunStatus.OK


async def test_every_filtered_record_names_what_decided_it(
    make_loop: Callable[..., SyncLoop], source: FakeConnector
) -> None:
    """A selection engine that answers correctly for the wrong reason will drift (C4).

    The two filtered objects in C3's example are filtered for genuinely different reasons,
    and each record says which: one was excluded by a rule the operator wrote, the other
    matched no rule at all and fell to the default.
    """
    for name in _C3_SCHEMAS:
        seed_schema(source, name)

    report = await make_loop(selection_rules=c3_worked_example(), create_missing=True).run_cycle(
        EntityType.DATA_PRODUCT
    )
    detail = details(report)

    staging = detail["analytics.staging"]
    assert staging is not None
    assert "rule #1 exclude glob 'analytics.staging*'" in staging

    finance = detail["finance.reporting"]
    assert finance is not None
    assert "no rule matched" in finance
    # ...and it is not blamed on a rule that had nothing to do with it.
    assert "analytics.staging*" not in finance

    for record in report.records:
        if record.outcome is RecordOutcome.FILTERED:
            assert record.reason is SkipReason.NOT_SELECTED
            assert record.holds_watermark is False


# ======================================================================================
# D1 equivalence: the selector is widened, not replaced (C3)
# ======================================================================================


async def test_a_pair_with_only_glob_patterns_selects_exactly_what_d1_selected(
    make_loop: Callable[..., SyncLoop], source: FakeConnector
) -> None:
    """The regression test for C3's "widened, not replaced", asserted against D1 itself.

    The expectation is not a hand-written list: it is
    :meth:`~qlabs_catalog_sync.config.SyncPairConfig.matches`, the D1 predicate this loop
    used to call directly, evaluated over the same objects. Data products **and** datasets,
    because a schema-scoped pattern list has always synced the tables inside the schemas it
    selects, and a rule set derived from it must keep doing so (C5's inheritance -- the
    exact case T11.2 found and fixed one level down).
    """
    schemas = ("sales.retail", "sales.wholesale", "hr.people")
    tables = ("sales.retail.orders", "sales.wholesale.invoices", "hr.people.employees")
    for name in schemas:
        seed_schema(source, name)
    for name in tables:
        seed_table(source, name)

    pair = make_pair(
        patterns=("sales.*",), entity_types=(EntityType.DATA_PRODUCT, EntityType.DATASET)
    )
    loop = make_loop(pair=pair)  # no selection_rules: derived from the pair, as D1 configs are
    products = await loop.run_cycle(EntityType.DATA_PRODUCT)
    datasets = await loop.run_cycle(EntityType.DATASET)

    for report, names in ((products, schemas), (datasets, tables)):
        found = outcomes(report)
        assert set(found) == set(names)
        for name in names:
            catalog, schema = name.split(".")[:2]
            selected_by_d1 = pair.matches(catalog, schema)
            assert (found[name] is not RecordOutcome.FILTERED) is selected_by_d1, name

    # Not vacuous, and stated the way a reader can check by eye: a schema-only rule set
    # still syncs the tables under every schema it selected.
    assert considered(datasets) == {"sales.retail.orders", "sales.wholesale.invoices"}
    assert considered(products) == {"sales.retail", "sales.wholesale"}


async def test_the_derived_rule_set_is_exactly_one_include_rule_per_pattern(
    make_loop: Callable[..., SyncLoop],
) -> None:
    """C3's degenerate case, spelled out: nothing is added to a D1 configuration."""
    pair = make_pair(patterns=("sales.*", "finance.reporting"))
    loop = make_loop(pair=pair)

    rules = loop.selection_rules.rules_for(RuleScope.OBJECT)
    assert [compiled.rule.pattern for compiled in rules] == ["sales.*", "finance.reporting"]
    assert all(compiled.rule.decision is SelectionDecision.INCLUDE for compiled in rules)
    assert loop.selection_rules.rules_for(RuleScope.DATASET) == ()
    # And the standalone translation the API and the bootstrap import share agrees.
    assert rule_set_for_pair(pair).rules_for(RuleScope.OBJECT) == rules


# ======================================================================================
# The full_name defect cannot recur
# ======================================================================================


async def test_a_uuid_keyed_source_scoped_to_one_catalog_never_syncs_another(
    store: StateStore, resolver: IdentityResolver, target: FakeConnector
) -> None:
    """The defect this whole design exists for, asserted at the level it happened.

    Databricks keys a schema on a ``schema_id`` UUID. A filter that matched ``native_key``
    alone found no dot in any change, decided the selector did not apply, and selected
    everything -- so a pair scoped to one catalog created a data product for every schema
    in the metastore, in a customer tenant, with no delete path to undo it (D4).
    """
    analytics_id = "3a7d9c14-2f65-4b08-8e51-90ab7c6d4e22"
    finance_id = "5c19e740-8a3b-4d92-b077-1e6f2a9c8b03"
    source = _OpaqueKeyedSource.read_only_source(name=SOURCE)
    source.seed_opaque(
        DataProduct(name="sales"), object_id=analytics_id, full_name="analytics.sales"
    )
    source.seed_opaque(
        DataProduct(name="reporting"), object_id=finance_id, full_name="finance.reporting"
    )
    assert "." not in analytics_id and "." not in finance_id  # the keys really are opaque

    loop = SyncLoop(
        pair=make_pair(patterns=("analytics.*",)),
        source=source,
        target=target,
        store=store,
        resolver=resolver,
        create_missing=True,
    )
    report = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert outcomes(report) == {
        analytics_id: RecordOutcome.CREATED,
        finance_id: RecordOutcome.FILTERED,
    }
    assert [call.args["entity"].name for call in target.calls("create")] == ["sales"]
    # The name was read from secondary_keys and a rule decided -- this is not the
    # unreadable-name path wearing the right answer by accident.
    finance_detail = details(report)[finance_id]
    assert finance_detail is not None
    assert "no rule matched" in finance_detail
    assert "no readable" not in finance_detail


async def test_a_uuid_keyed_dataset_is_scoped_by_its_full_name_too(
    store: StateStore, resolver: IdentityResolver, target: FakeConnector
) -> None:
    """The same rule one scope down: a table's ``catalog.schema.table`` name decides it."""
    in_scope = "1f0c2ab8-77d5-4a30-9b21-0c4e5f6a7b8c"
    out_of_scope = "9d8e7f60-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
    source = _OpaqueKeyedSource.read_only_source(name=SOURCE)
    source.seed_opaque(
        Dataset(name="orders"), object_id=in_scope, full_name="analytics.sales.orders"
    )
    source.seed_opaque(
        Dataset(name="ledger"), object_id=out_of_scope, full_name="finance.reporting.ledger"
    )

    loop = SyncLoop(
        pair=make_pair(
            patterns=("analytics.*",),
            entity_types=(EntityType.DATA_PRODUCT, EntityType.DATASET),
        ),
        source=source,
        target=target,
        store=store,
        resolver=resolver,
    )
    report = await loop.run_cycle(EntityType.DATASET)

    assert outcomes(report)[out_of_scope] is RecordOutcome.FILTERED
    assert outcomes(report)[in_scope] is not RecordOutcome.FILTERED
    assert read_keys(source) == [in_scope]


# ======================================================================================
# The deliberate behaviour change: an unreadable name now fails closed
# ======================================================================================


async def test_an_object_with_no_readable_name_is_filtered_instead_of_synced(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, target: FakeConnector
) -> None:
    """The live behaviour change T11.3 makes knowingly.

    The superseded ``SyncLoop._selects`` returned ``True`` for a change with no dotted path
    in ``full_name`` or ``native_key`` -- "not something these patterns can describe" -- so
    an object like this was **synced**. The evaluator gives that candidate ``UNKNOWN``,
    reports every glob rule undetermined, and the default decision (exclude) applies.

    This test is the proof the old fallback is gone: if it were still reachable there would
    be a second create at the target and a second read at the source.
    """
    seed_schema(source, "analytics.sales")
    source.seed(DataProduct(name="mystery"), native_key="opaque-handle-with-no-name")

    report = await make_loop(selection_rules=c3_worked_example(), create_missing=True).run_cycle(
        EntityType.DATA_PRODUCT
    )

    assert outcomes(report) == {
        "analytics.sales": RecordOutcome.CREATED,
        "opaque-handle-with-no-name": RecordOutcome.FILTERED,
    }
    assert [call.args["entity"].name for call in target.calls("create")] == ["sales"]
    assert read_keys(source) == ["analytics.sales"]
    assert write_calls(source) == []  # upstream-only: the source is never written to


async def test_the_record_says_the_name_was_unreadable_not_that_a_rule_excluded_it(
    make_loop: Callable[..., SyncLoop], source: FakeConnector
) -> None:
    """Somebody watching objects disappear must be able to tell which of the two it was.

    Failing closed is correct, but it looks from the outside exactly like a rule the
    operator wrote. The detail therefore leads with the name, says what the source did not
    report, and then carries the evaluator's own undetermined list so it is clear no rule
    reached a verdict at all.
    """
    source.seed(DataProduct(name="mystery"), native_key="opaque-handle-with-no-name")

    report = await make_loop(selection_rules=c3_worked_example(), create_missing=True).run_cycle(
        EntityType.DATA_PRODUCT
    )
    record = report.records[0]

    assert record.outcome is RecordOutcome.FILTERED
    assert record.reason is SkipReason.NOT_SELECTED
    detail = record.detail
    assert detail is not None
    assert "no readable catalog.schema name" in detail
    assert "secondary_keys['full_name']" in detail
    assert "The name is what excluded it, not a rule" in detail
    # The evaluator's own report of what could not be decided is carried through verbatim,
    # for every rule in scope -- not just the first one.
    assert "3 rule(s) undetermined" in detail
    assert "rule #0 include glob 'analytics.*' could not be evaluated: qualified_name unknown" in (
        detail
    )
    # Out of scope is out of scope: nothing is outstanding, so it does not hold the watermark.
    assert record.holds_watermark is False
    assert report.watermark_held_by == ()
    assert report.status is RunStatus.OK


async def test_a_dataset_with_no_readable_name_is_filtered_by_the_name_not_by_its_parent(
    make_loop: Callable[..., SyncLoop], source: FakeConnector
) -> None:
    """C5 composition would otherwise blame the parent schema for an unreadable child.

    ``DatasetSelection.explain()`` truthfully says "parent schema not selected" -- but for a
    dataset with no name at all, no parent could be derived either, and reporting it as a
    parent problem would send an operator to look at their schema rules for a defect that
    is in the connector's keys.
    """
    source.seed(Dataset(name="mystery"), native_key="opaque-table-handle")

    pair = make_pair(entity_types=(EntityType.DATA_PRODUCT, EntityType.DATASET))
    report = await make_loop(pair=pair, selection_rules=c3_worked_example()).run_cycle(
        EntityType.DATASET
    )
    record = report.records[0]

    assert record.outcome is RecordOutcome.FILTERED
    detail = record.detail
    assert detail is not None
    assert "no readable catalog.schema.table name" in detail
    assert detail.startswith("excluded because this object has no readable")


# ======================================================================================
# An entity type selection has no scope for (C5)
# ======================================================================================


async def test_an_entity_type_with_no_selection_scope_is_excluded_and_says_so(
    store: StateStore, resolver: IdentityResolver
) -> None:
    """C5 gives rules two scopes and no others, so nothing can select a category.

    The decision is made on the entity type, deliberately, and not by whether the key
    happens to be dot-shaped: this category is keyed on ``analytics.sales``, which every
    object-scope rule in the set matches. It is still excluded, and the record says the
    scope is what excluded it rather than pretending a rule did -- and rather than the
    other failure, quietly syncing an object no rule in the set was ever written for.
    """
    source = FakeConnector.read_only_source(
        name=SOURCE, manifest=_manifest_with_categories(databricks_shaped_manifest())
    )
    target = FakeConnector.write_target(
        name=TARGET, manifest=_manifest_with_categories(qlik_shaped_manifest())
    )
    source.seed(Category(name="sales"), native_key="analytics.sales")

    loop = SyncLoop(
        pair=make_pair(entity_types=(EntityType.CATEGORY,)),
        source=source,
        target=target,
        store=store,
        resolver=resolver,
        selection_rules=c3_worked_example(),
        create_missing=True,
    )
    report = await loop.run_cycle(EntityType.CATEGORY)

    record = report.records[0]
    assert record.outcome is RecordOutcome.FILTERED
    assert record.reason is SkipReason.NOT_SELECTED
    detail = record.detail
    assert detail is not None
    assert "no rule scope for 'category'" in detail
    assert "entity_types" in detail
    # Nothing was read and nothing was written: a clean, reported no-op.
    assert read_keys(source) == []
    assert write_calls(target) == []
    assert report.status is RunStatus.OK


# ======================================================================================
# A pair matching nothing is a clean no-op
# ======================================================================================


async def test_a_pair_whose_rules_match_nothing_is_a_clean_no_op(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """The DoD's own words. Not an error, not a partial run, and not a stuck watermark.

    A cycle that filtered everything has still *seen* everything, so the watermark advances
    past it exactly as it would for a cycle that wrote everything -- otherwise a
    mis-scoped pair would re-list its whole metastore forever, and the second cycle here
    would find the same objects again.
    """
    for name in ("finance.reporting", "hr.people"):
        seed_schema(source, name)
    pair = make_pair(patterns=("analytics.*",))
    loop = make_loop(pair=pair, create_missing=True)

    report = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert report.status is RunStatus.OK
    assert report.committed is True
    assert report.errors == ()
    assert report.orphans == ()
    assert report.count(RecordOutcome.FILTERED) == 2
    assert all(record.reason is SkipReason.NOT_SELECTED for record in report.records)
    assert write_calls(target) == []
    assert read_keys(source) == []
    assert report.watermark_held_by == ()
    assert report.watermark_advanced is True

    stored = await store.get_watermark(pair.name, SOURCE, EntityType.DATA_PRODUCT)
    assert stored is not None
    assert stored.watermark_token == report.watermark_after

    second = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert second.records == ()  # nothing re-listed: the watermark really did move
    assert second.status is RunStatus.OK


async def test_an_empty_rule_set_selects_nothing_rather_than_everything(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, target: FakeConnector
) -> None:
    """The blast-radius property, at the loop. An empty rule set is not "no filter"."""
    for name in _C3_SCHEMAS:
        seed_schema(source, name)

    report = await make_loop(
        selection_rules=SelectionRuleSet.build(), create_missing=True
    ).run_cycle(EntityType.DATA_PRODUCT)

    assert report.count(RecordOutcome.FILTERED) == len(_C3_SCHEMAS)
    assert write_calls(target) == []
    assert report.status is RunStatus.OK


# ======================================================================================
# The dishonest cases
# ======================================================================================


async def test_the_cycle_and_the_console_preview_select_the_same_objects(
    make_loop: Callable[..., SyncLoop], source: FakeConnector
) -> None:
    """Decision C4, at the loop: a preview that can disagree with the run is worse than none.

    ``tests/selection/test_preview_sync_agreement.py`` certifies the property over the
    source-tree functions both callers share. This one closes the last gap by driving the
    **real cycle** -- entity types, the loop's own scope mapping, ``FILTERED`` records and
    all -- against the same rule set and the same source the preview walks, and asserting
    the two select the same objects. It fails if the loop ever grows its own opinion about
    scope, however small.
    """
    for name in _C3_SCHEMAS:
        seed_schema(source, name)
    for name in _C3_DATASETS:
        seed_table(source, name)

    rule_set = c3_worked_example()
    pair = make_pair(entity_types=(EntityType.DATA_PRODUCT, EntityType.DATASET))
    loop = make_loop(pair=pair, selection_rules=rule_set)

    products = await loop.run_cycle(EntityType.DATA_PRODUCT)
    datasets = await loop.run_cycle(EntityType.DATASET)
    synced = considered(products, datasets)

    # The very same source the cycle just ran over: a preview never disturbs it.
    previewed = {
        node.change.ref.native_key
        async for node in walk_source_tree(source, rule_set)
        if node.included
    }

    assert synced == previewed
    # Not vacuous: the example really does split this metastore in both scopes.
    assert previewed == {
        "analytics.sales",
        "analytics.prod_staging",
        "analytics.sales.orders",
        "analytics.prod_staging.snapshots",
    }


async def test_the_rule_set_is_compiled_once_and_not_once_per_change(
    monkeypatch: pytest.MonkeyPatch,
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
) -> None:
    """Pattern compilation must not be inside the per-candidate loop.

    ``SelectionRuleSet.build`` compiles every pattern into a matcher, and a real cycle
    processes thousands of changes. This counts actual ``compile_matcher`` calls: it fails
    if the loop ever builds its rule set per change, per page or per cycle instead of once.
    """
    compiled: list[str] = []
    real = selection_rules_module.compile_matcher

    def counting(*, matcher_kind: Any, pattern: str, scope: Any) -> Any:
        compiled.append(pattern)
        return real(matcher_kind=matcher_kind, pattern=pattern, scope=scope)

    monkeypatch.setattr(selection_rules_module, "compile_matcher", counting)

    for name in (*_C3_SCHEMAS, "analytics.marts", "analytics.raw", "finance.ledger"):
        seed_schema(source, name)
    loop = make_loop(pair=make_pair(patterns=("analytics.*", "finance.*")))

    assert compiled == ["analytics.*", "finance.*"]  # once per rule, at construction

    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert len(first.records) == 7  # the cycle really did evaluate a crowd
    assert compiled == ["analytics.*", "finance.*"]  # ...and compiled nothing more


async def test_an_explicit_rule_set_decides_and_the_pairs_glob_list_is_not_consulted(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, target: FakeConnector
) -> None:
    """The seam for loading rules from the configuration store, proved to be load-bearing.

    The pair's ``catalog_schema_patterns`` and the supplied rule set are deliberately each
    other's opposite. If the superseded glob path were still reachable anywhere in
    ``_process`` the two assertions below would come out exactly inverted, which is what
    makes this a regression test rather than a restatement.
    """
    seed_schema(source, "analytics.sales")
    seed_schema(source, "finance.reporting")

    loop = make_loop(
        pair=make_pair(patterns=("finance.*",)),
        selection_rules=SelectionRuleSet.build([include(0, "analytics.*")]),
        create_missing=True,
    )
    report = await loop.run_cycle(EntityType.DATA_PRODUCT)

    assert outcomes(report) == {
        "analytics.sales": RecordOutcome.CREATED,
        "finance.reporting": RecordOutcome.FILTERED,
    }
    assert [call.args["entity"].name for call in target.calls("create")] == ["sales"]


def test_the_loop_no_longer_carries_a_glob_selector_of_its_own() -> None:
    """Structural, because "the old path is unreachable" is a claim about the whole module.

    The behavioural tests above cover every path a cycle takes. This covers the paths it
    does not: a leftover helper nothing calls today is exactly what gets called again by
    the next change, and a second implementation of D1 living beside the evaluator is the
    thing decision C4 forbids.
    """
    tree = ast.parse(Path(loop_module.__file__).read_text(encoding="utf-8"))
    attributes = [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)]

    assert not hasattr(SyncLoop, "_selects")
    # SyncPairConfig.matches is D1's own predicate; the loop must not call it any more.
    assert "matches" not in attributes
    # catalog_schema_patterns survives in exactly one place: the D1 -> C3 translation.
    holders = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(inner, ast.Attribute) and inner.attr == "catalog_schema_patterns"
            for inner in ast.walk(node)
        )
    ]
    assert holders == ["rule_set_for_pair"]


def test_the_default_rule_set_is_derived_from_the_pair_and_nothing_else() -> None:
    """The seam has one input, so wiring the configuration store to it changes one argument."""
    pair = make_pair(patterns=("analytics.*", "finance.*"))
    derived = rule_set_for_pair(pair)

    assert not derived.is_empty(RuleScope.OBJECT)
    assert derived.is_empty(RuleScope.DATASET)
    assert [compiled.rule.pattern for compiled in derived.rules_for(RuleScope.OBJECT)] == list(
        pair.catalog_schema_patterns
    )


async def test_a_filtered_delete_stays_filtered_and_is_never_orphaned(
    make_loop: Callable[..., SyncLoop], source: FakeConnector, store: StateStore
) -> None:
    """Selection runs before the orphan path, and must keep doing so (D4).

    ``deleted_unknown_object`` is the channel an operator watches to find out that source
    objects are vanishing from under a live sync. Filling it with every schema the pair was
    never meant to touch makes that signal useless on a multi-catalog metastore.
    """
    ref = source.seed(DataProduct(name="reporting"), native_key="finance.reporting")
    source.vanish(ref)

    report = await make_loop(selection_rules=c3_worked_example(), create_missing=True).run_cycle(
        EntityType.DATA_PRODUCT
    )

    assert {record.outcome for record in report.records} == {RecordOutcome.FILTERED}
    assert report.orphans == ()
    assert await store.list_orphans(SOURCE) == []


async def test_selection_never_reads_the_source_to_answer_a_tag_rule(
    make_loop: Callable[..., SyncLoop], source: FakeConnector
) -> None:
    """A filter that issues I/O is not a filter any more.

    Tags and owners are ``UNKNOWN`` from the loop unless it already read the entity for
    another reason, so a tag rule is reported *undetermined* rather than resolved by a
    ``read()`` per candidate. That is the honest answer, and it reaches the report.
    """
    seed_schema(source, "analytics.sales")
    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="all-analytics"),
            exclude(
                1,
                "pii",
                matcher_kind=MatcherKind.TAG,
                rule_id="no-pii",
            ),
        ]
    )

    report = await make_loop(selection_rules=rule_set, create_missing=True).run_cycle(
        EntityType.DATA_PRODUCT
    )

    # The tag rule could not be evaluated, so it did not exclude anything...
    assert outcomes(report) == {"analytics.sales": RecordOutcome.CREATED}
    # ...and exactly one read happened: the one the write path needed, not one per rule.
    assert read_keys(source) == ["analytics.sales"]


async def test_an_undetermined_rule_is_reported_on_a_filtered_record(
    make_loop: Callable[..., SyncLoop], source: FakeConnector
) -> None:
    """ "This source cannot tell me" must never be silently read as "no"."""
    seed_schema(source, "finance.reporting")
    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="all-analytics"),
            include(
                1,
                "owner=data-platform",
                matcher_kind=MatcherKind.TAG,
                rule_id="platform-owned",
            ),
        ]
    )

    report = await make_loop(selection_rules=rule_set).run_cycle(EntityType.DATA_PRODUCT)

    detail = details(report)["finance.reporting"]
    assert detail is not None
    assert "no rule matched" in detail
    assert "1 rule(s) undetermined" in detail
    assert "rule #1 include tag 'owner=data-platform'" in detail
    assert "tags unknown" in detail
