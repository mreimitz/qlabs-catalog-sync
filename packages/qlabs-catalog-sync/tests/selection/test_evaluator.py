"""T11.1 -- the evaluator's behaviour: ordering, overrides, scopes, and the deciding rule.

Every assertion here is about what the evaluator *decides* and *why*, never about how it
was called. A selection engine that returns the right booleans for the wrong reasons is a
selection engine whose console preview will eventually disagree with the run it predicts,
which is the exact failure C4 exists to make impossible.

Several tests are written specifically to fail against a plausible wrong implementation
rather than to confirm a happy path -- first-match-wins instead of last-match-wins, the
last *evaluated* rule reported instead of the last *matching* one, an unknown fact quietly
treated as an absent one, and the default-include that let a pair scoped to one catalog
sync an entire metastore.
"""

from __future__ import annotations

import fnmatch
import random
import uuid
from collections.abc import Sequence

import pytest
from selection_helpers import (
    dataset_candidate,
    decisions,
    exclude,
    include,
    override,
    owners,
    schema_candidate,
    tags,
)

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.selection import (
    UNKNOWN,
    CandidateFact,
    DecisionSource,
    MatcherKind,
    RuleScope,
    SelectionCandidate,
    SelectionDecision,
    SelectionRule,
    SelectionRuleSet,
    evaluate,
    evaluate_all,
    object_rules_from_catalog_schema_patterns,
)
from qlabs_catalog_sync_sdk.models import EntityType

# --------------------------------------------------------------------------------------
# The default: an empty rule set selects nothing
# --------------------------------------------------------------------------------------


def test_an_empty_rule_set_includes_nothing_and_says_so() -> None:
    result = evaluate(SelectionRuleSet.build(), schema_candidate("analytics.sales"))
    assert not result.included
    assert result.decision is SelectionDecision.EXCLUDE
    assert result.source is DecisionSource.DEFAULT
    assert result.rule is None and result.rule_id is None
    assert result.override is None
    assert result.explain() == "excluded by default (no rule matched)"


def test_a_candidate_no_rule_matches_is_excluded_by_the_default_not_by_a_rule() -> None:
    """"No rule matched" is a first-class answer, distinguishable from "a rule excluded it"."""
    rule_set = SelectionRuleSet.build([include(0, "analytics.*")])
    matched = evaluate(rule_set, schema_candidate("analytics.sales"))
    unmatched = evaluate(rule_set, schema_candidate("finance.sales"))

    assert matched.source is DecisionSource.RULE
    assert unmatched.source is DecisionSource.DEFAULT
    assert not unmatched.included
    assert unmatched.rule is None


# --------------------------------------------------------------------------------------
# Ordering: the last matching rule decides (C3)
# --------------------------------------------------------------------------------------


def test_the_same_two_rules_in_the_opposite_order_decide_the_opposite_way() -> None:
    """Order is the whole point: reversing the list must reverse the answer."""
    candidate = schema_candidate("analytics.staging_a")

    include_then_exclude = SelectionRuleSet.build(
        [include(0, "analytics.*", rule_id="inc"), exclude(1, "analytics.staging*", rule_id="exc")]
    )
    exclude_then_include = SelectionRuleSet.build(
        [exclude(0, "analytics.staging*", rule_id="exc"), include(1, "analytics.*", rule_id="inc")]
    )

    first = evaluate(include_then_exclude, candidate)
    second = evaluate(exclude_then_include, candidate)

    assert not first.included
    assert first.rule_id == "exc"
    assert second.included
    assert second.rule_id == "inc"


def test_the_last_matching_rule_wins_not_the_first() -> None:
    """Fails against a first-match-wins implementation.

    Both rules match ``analytics.staging_a``. First-match-wins would include it and name
    ``inc``; C3 says the later ``exc`` decides.
    """
    rule_set = SelectionRuleSet.build(
        [include(0, "analytics.*", rule_id="inc"), exclude(1, "analytics.staging*", rule_id="exc")]
    )
    result = evaluate(rule_set, schema_candidate("analytics.staging_a"))
    assert not result.included
    assert result.rule_id == "exc"


def test_the_deciding_rule_is_the_last_matching_rule_not_the_last_evaluated_rule() -> None:
    """Fails against an implementation that reports whichever rule the loop ended on.

    The rule set ends with two rules that do **not** match the candidate, so "the last rule
    evaluated" and "the last rule that matched" are different objects with different
    decisions. Only the latter is a correct answer, and only the latter lets a console
    highlight something an operator can act on.
    """
    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="broad-include"),
            exclude(1, "analytics.tmp*", rule_id="tmp-exclude"),
            exclude(2, "finance.*", rule_id="other-catalog"),
            include(3, "warehouse.*", rule_id="other-catalog-2"),
        ]
    )
    result = evaluate(rule_set, schema_candidate("analytics.sales"))
    assert result.included
    assert result.rule_id == "broad-include"
    assert result.rule is not None and result.rule.ordinal == 0


def test_evaluation_does_not_stop_at_the_first_match() -> None:
    """Three rules all match; the third must be the one that decides."""
    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="a"),
            exclude(1, "analytics.*", rule_id="b"),
            include(2, "analytics.*", rule_id="c"),
        ]
    )
    result = evaluate(rule_set, schema_candidate("analytics.sales"))
    assert result.included
    assert result.rule_id == "c"


def test_ordinals_need_not_be_contiguous() -> None:
    """Reordering in a console leaves gaps; relative order is what matters."""
    rule_set = SelectionRuleSet.build(
        [include(10, "analytics.*", rule_id="inc"), exclude(99, "analytics.s*", rule_id="exc")]
    )
    assert evaluate(rule_set, schema_candidate("analytics.sales")).rule_id == "exc"
    assert evaluate(rule_set, schema_candidate("analytics.finance")).rule_id == "inc"


# --------------------------------------------------------------------------------------
# C3's worked example
# --------------------------------------------------------------------------------------


def test_c3_worked_example_include_then_exclude_then_carve_back_in() -> None:
    """C3: "everything in the analytics catalog except the staging schemas, but keep
    ``analytics.prod_staging``" -- with the deciding rule asserted for all three outcomes.

    The exclude pattern is ``analytics.*staging*`` so that it genuinely covers
    ``prod_staging``; otherwise the carve-back rule would be decorative and this test would
    prove nothing about carving back in.
    """
    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="all-analytics"),
            exclude(1, "analytics.*staging*", rule_id="no-staging"),
            include(2, "analytics.prod_staging", rule_id="keep-prod-staging"),
        ]
    )
    results = list(
        evaluate_all(
            rule_set,
            [
                schema_candidate("analytics.sales"),
                schema_candidate("analytics.staging_eu"),
                schema_candidate("analytics.prod_staging"),
                schema_candidate("finance.sales"),
            ],
        )
    )
    assert decisions(results) == [
        ("analytics.sales", True, "all-analytics"),
        ("analytics.staging_eu", False, "no-staging"),
        ("analytics.prod_staging", True, "keep-prod-staging"),
        ("finance.sales", False, None),
    ]


def test_c3_worked_example_with_a_prefix_exclude_leaves_prod_staging_to_the_broad_include() -> None:
    """The literal ``analytics.staging*`` spelling, whose exclusion does *not* reach
    ``prod_staging`` -- so the broad include is still what decides it.

    Worth pinning because the two spellings look interchangeable in prose and are not:
    naming the deciding rule is what tells an operator which of the two they wrote.
    """
    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="all-analytics"),
            exclude(1, "analytics.staging*", rule_id="no-staging"),
        ]
    )
    prod_staging = evaluate(rule_set, schema_candidate("analytics.prod_staging"))
    staging = evaluate(rule_set, schema_candidate("analytics.staging_eu"))

    assert prod_staging.included and prod_staging.rule_id == "all-analytics"
    assert not staging.included and staging.rule_id == "no-staging"


def test_the_carve_back_in_can_be_an_override_instead_of_a_rule() -> None:
    """The same outcome, pinned per object -- and reported as an override, not a rule."""
    rule_set = SelectionRuleSet.build(
        rules=[
            include(0, "analytics.*", rule_id="all-analytics"),
            exclude(1, "analytics.*staging*", rule_id="no-staging"),
        ],
        overrides=[
            override("analytics.prod_staging", SelectionDecision.INCLUDE, reason="pilot schema")
        ],
    )
    result = evaluate(rule_set, schema_candidate("analytics.prod_staging"))
    assert result.included
    assert result.source is DecisionSource.OVERRIDE
    assert result.rule is None
    assert result.override is not None and result.override.reason == "pilot schema"
    assert "pilot schema" in result.explain()


# --------------------------------------------------------------------------------------
# Overrides beat every rule (C3)
# --------------------------------------------------------------------------------------


def test_an_override_beats_a_rule_that_comes_after_it() -> None:
    """Overrides are not "a rule at ordinal -1"; ordering cannot reach them."""
    rule_set = SelectionRuleSet.build(
        rules=[exclude(99, "analytics.*", rule_id="late-exclude")],
        overrides=[override("analytics.sales", SelectionDecision.INCLUDE)],
    )
    result = evaluate(rule_set, schema_candidate("analytics.sales"))
    assert result.included
    assert result.source is DecisionSource.OVERRIDE


def test_an_override_beats_an_exclude_everything_rule() -> None:
    rule_set = SelectionRuleSet.build(
        rules=[exclude(0, "*.*", rule_id="exclude-all")],
        overrides=[override("analytics.sales", SelectionDecision.INCLUDE)],
    )
    assert evaluate(rule_set, schema_candidate("analytics.sales")).included
    assert not evaluate(rule_set, schema_candidate("analytics.other")).included


def test_an_override_can_exclude_something_every_rule_includes() -> None:
    rule_set = SelectionRuleSet.build(
        rules=[include(0, "*.*", rule_id="include-all")],
        overrides=[override("analytics.sales", SelectionDecision.EXCLUDE, reason="noisy")],
    )
    result = evaluate(rule_set, schema_candidate("analytics.sales"))
    assert not result.included
    assert result.source is DecisionSource.OVERRIDE


def test_an_override_can_pin_a_stable_id_so_it_survives_a_rename() -> None:
    """Pinning the UUID, not the name, is what makes an override outlive a schema rename."""
    stable_id = str(uuid.uuid4())
    rule_set = SelectionRuleSet.build(
        rules=[exclude(0, "*.*", rule_id="exclude-all")],
        overrides=[override(stable_id, SelectionDecision.INCLUDE)],
    )
    before = evaluate(rule_set, schema_candidate("analytics.sales", object_id=stable_id))
    after = evaluate(rule_set, schema_candidate("analytics.sales_v2", object_id=stable_id))
    assert before.included and after.included


def test_a_stable_id_pin_wins_over_a_name_pin_for_the_same_candidate() -> None:
    """Both can exist; the tie-break is documented and deterministic, never "whichever"."""
    stable_id = str(uuid.uuid4())
    rule_set = SelectionRuleSet.build(
        overrides=[
            override(stable_id, SelectionDecision.INCLUDE),
            override("analytics.sales", SelectionDecision.EXCLUDE),
        ]
    )
    result = evaluate(rule_set, schema_candidate("analytics.sales", object_id=stable_id))
    assert result.included
    assert result.override is not None and result.override.object_id == stable_id


def test_an_override_only_applies_within_its_own_scope() -> None:
    rule_set = SelectionRuleSet.build(
        overrides=[override("analytics.sales.orders", SelectionDecision.INCLUDE)]
    )
    dataset = evaluate(rule_set, dataset_candidate("analytics.sales.orders"))
    assert not dataset.included
    assert dataset.source is DecisionSource.DEFAULT


# --------------------------------------------------------------------------------------
# D1 equivalence: the selector is widened, not replaced (C3)
# --------------------------------------------------------------------------------------

#: Catalog/schema inputs against a pair configured with the patterns below. Deliberately
#: full of near-misses: prefix-but-not-match, case differences, and names that only a
#: segment-wise matcher gets right.
_D1_INPUTS = [
    ("analytics", "sales"),
    ("analytics", "finance"),
    ("analytics", ""),
    ("analytics_archive", "sales"),
    ("Analytics", "sales"),
    ("analytic", "sales"),
    ("finance", "reporting"),
    ("finance", "reporting_eu"),
    ("finance", "Reporting"),
    ("warehouse", "prod"),
    ("warehouse", "prod_2024"),
    ("sales_eu", "orders"),
    ("sales", "orders"),
    ("other", "other"),
]


def test_a_rule_set_of_plain_include_globs_reproduces_d1_exactly() -> None:
    """C3's claim, as a test: one include rule per pattern decides what ``SyncPairConfig``
    decided, input for input.

    Both sides are exercised: the pair's own ``matches`` (which is what RM-01's sync loop
    filtered on) and the evaluator built from the very same pattern list.
    """
    pair = SyncPairConfig(
        name="databricks-to-qlik",
        source="databricks_prod",
        target="qlik_acme",
        catalog_schema_patterns=["analytics.*", "finance.reporting", "warehouse.prod*"],
        target_space="Data Products",
        entity_types=[EntityType.DATA_PRODUCT],
    )
    rule_set = SelectionRuleSet.build(
        object_rules_from_catalog_schema_patterns(pair.catalog_schema_patterns)
    )

    for catalog, schema in _D1_INPUTS:
        expected = pair.matches(catalog, schema)
        if not schema:
            # An empty schema segment is not a name a candidate can carry; D1 answers it
            # only because it matches raw strings. Nothing to compare, so skip the row
            # explicitly rather than pretending the two agreed.
            with pytest.raises(ValueError):
                schema_candidate(f"{catalog}.{schema}")
            continue
        actual = evaluate(rule_set, schema_candidate(f"{catalog}.{schema}")).included
        assert actual is expected, f"{catalog}.{schema}: D1 says {expected}, evaluator {actual}"


def test_d1_equivalence_holds_whatever_order_the_include_globs_are_in() -> None:
    """Pure includes are ``any()``: with the default excluding, their order cannot matter."""
    patterns = ["analytics.*", "finance.reporting", "warehouse.prod*"]
    forwards = SelectionRuleSet.build(object_rules_from_catalog_schema_patterns(patterns))
    backwards = SelectionRuleSet.build(
        object_rules_from_catalog_schema_patterns(list(reversed(patterns)))
    )
    for catalog, schema in _D1_INPUTS:
        if not schema:
            continue
        candidate = schema_candidate(f"{catalog}.{schema}")
        assert evaluate(forwards, candidate).included is evaluate(backwards, candidate).included


# --------------------------------------------------------------------------------------
# The regression the whole design exists for: matching the name, not the key
# --------------------------------------------------------------------------------------


def test_a_uuid_keyed_object_is_selected_on_its_dotted_name() -> None:
    """The pilot defect, as a test.

    Databricks keys a schema on ``schema_id`` -- a UUID, because names are renameable --
    and carries ``catalog.schema`` in ``secondary_keys["full_name"]``. The old filter
    matched the pattern against the UUID, found no dot, concluded the selector did not
    apply, and included everything. Here the UUID is the stable id, the dotted name is the
    name, and the pattern is measured against the name.
    """
    rule_set = SelectionRuleSet.build([include(0, "analytics.*", rule_id="analytics-only")])
    in_scope = schema_candidate("analytics.sales", object_id="6f6b8e0e-uuid")
    out_of_scope = schema_candidate("other_catalog.sales", object_id="9a1c2d3e-uuid")

    assert evaluate(rule_set, in_scope).rule_id == "analytics-only"
    assert evaluate(rule_set, in_scope).included
    assert not evaluate(rule_set, out_of_scope).included


def test_a_pair_scoped_to_one_catalog_never_selects_another_catalog() -> None:
    """The blast radius the defect actually had: one catalog configured, every catalog synced."""
    rule_set = SelectionRuleSet.build([include(0, "analytics.*", rule_id="analytics-only")])
    metastore = [
        schema_candidate(f"{catalog}.{schema}")
        for catalog in ("analytics", "finance", "hr", "raw_landing")
        for schema in ("sales", "staging", "tmp")
    ]
    included = [
        result.candidate.qualified_name
        for result in evaluate_all(rule_set, metastore)
        if result.included
    ]
    assert included == ["analytics.sales", "analytics.staging", "analytics.tmp"]


def test_a_candidate_with_no_qualified_name_is_excluded_and_says_why() -> None:
    """A nameless object falls to the default -- exclude -- and every glob rule it could not
    answer is reported, rather than any of it being read as "the selector does not apply".

    This is the case the old filter got wrong. Failing closed and *saying so* is the
    behaviour; silently including was the bug.
    """
    rule_set = SelectionRuleSet.build(
        [include(0, "analytics.*", rule_id="a"), exclude(1, "analytics.tmp*", rule_id="b")]
    )
    opaque = SelectionCandidate(
        scope=RuleScope.OBJECT, object_id="qlik-opaque-handle", qualified_name=UNKNOWN
    )
    result = evaluate(rule_set, opaque)

    assert not result.included
    assert result.source is DecisionSource.DEFAULT
    assert [item.rule.rule_id for item in result.undetermined] == ["a", "b"]
    assert all(item.missing is CandidateFact.QUALIFIED_NAME for item in result.undetermined)
    assert "qualified_name unknown" in result.explain()


def test_an_unnamed_candidate_is_still_reachable_by_an_override_on_its_stable_id() -> None:
    """Failing closed is not a dead end: the operator can still pin the object."""
    rule_set = SelectionRuleSet.build(
        overrides=[override("qlik-opaque-handle", SelectionDecision.INCLUDE)]
    )
    opaque = SelectionCandidate(
        scope=RuleScope.OBJECT, object_id="qlik-opaque-handle", qualified_name=UNKNOWN
    )
    assert evaluate(rule_set, opaque).included


# --------------------------------------------------------------------------------------
# Unknown facts are not empty facts (C5, RM-01 D6)
# --------------------------------------------------------------------------------------


def _tag_rules() -> Sequence[SelectionRule]:
    return [
        include(0, "analytics.*", rule_id="all-analytics"),
        exclude(1, "pii", matcher_kind=MatcherKind.TAG, rule_id="no-pii"),
    ]


def test_a_tag_rule_against_a_source_with_no_tags_does_not_match() -> None:
    rule_set = SelectionRuleSet.build(_tag_rules())
    result = evaluate(rule_set, schema_candidate("analytics.sales", tags=()))
    assert result.included
    assert result.rule_id == "all-analytics"
    assert result.undetermined == ()
    assert not result.has_undetermined


def test_a_tag_rule_against_a_source_that_cannot_report_tags_is_undetermined() -> None:
    """Databricks reports UC tags only with a SQL warehouse configured (RM-01 D6).

    The decision is the same as the no-tags case -- included, by the broad rule -- and the
    *result* is deliberately not: the tag rule is reported as unevaluated, which is what
    lets a preview say "this source cannot tell me about tags" instead of implying it
    checked and found none. Collapsing these two into one answer is the single most likely
    way for a preview and a run to drift apart.
    """
    rule_set = SelectionRuleSet.build(_tag_rules())
    known_empty = evaluate(rule_set, schema_candidate("analytics.sales", tags=()))
    unknown = evaluate(rule_set, schema_candidate("analytics.sales", tags=UNKNOWN))

    assert unknown.included == known_empty.included
    assert unknown.rule_id == known_empty.rule_id
    assert unknown != known_empty
    assert known_empty.undetermined == ()
    assert [item.rule.rule_id for item in unknown.undetermined] == ["no-pii"]
    assert unknown.undetermined[0].missing is CandidateFact.TAGS
    assert "tags unknown" in unknown.explain()


def test_an_unknown_tag_never_silently_matches_either() -> None:
    """Unavailable is not "yes" any more than it is "no": the rule simply does not fire."""
    rule_set = SelectionRuleSet.build(
        [include(0, "pii", matcher_kind=MatcherKind.TAG, rule_id="only-pii")]
    )
    result = evaluate(rule_set, schema_candidate("analytics.sales", tags=UNKNOWN))
    assert not result.included
    assert result.source is DecisionSource.DEFAULT
    assert result.has_undetermined


def test_undetermined_rules_after_the_deciding_one_are_still_reported() -> None:
    """A rule that could have overturned the answer is reported even though it did not run.

    The exclude sits *after* the include that decided, so had tags been available it might
    have flipped the decision. Reporting only the rules before the decision would hide
    exactly the disagreements this list exists to surface.
    """
    rule_set = SelectionRuleSet.build(
        [
            exclude(0, "deprecated", matcher_kind=MatcherKind.TAG, rule_id="early-tag"),
            include(1, "analytics.*", rule_id="decides"),
            exclude(2, "pii", matcher_kind=MatcherKind.TAG, rule_id="late-tag"),
        ]
    )
    result = evaluate(rule_set, schema_candidate("analytics.sales", tags=UNKNOWN))
    assert result.rule_id == "decides"
    assert [item.rule.rule_id for item in result.undetermined] == ["early-tag", "late-tag"]


def test_owner_facts_carry_the_same_unknown_versus_empty_distinction() -> None:
    rule_set = SelectionRuleSet.build(
        [include(0, "*@acme.com", matcher_kind=MatcherKind.OWNER, rule_id="acme-owned")]
    )
    owned = evaluate(rule_set, schema_candidate("analytics.sales", owners=owners("a@acme.com")))
    unowned = evaluate(rule_set, schema_candidate("analytics.sales", owners=()))
    unknown = evaluate(rule_set, schema_candidate("analytics.sales", owners=UNKNOWN))

    assert owned.included and owned.rule_id == "acme-owned"
    assert not unowned.included and unowned.undetermined == ()
    assert not unknown.included
    assert [item.missing for item in unknown.undetermined] == [CandidateFact.OWNERS]


def test_tag_and_owner_rules_decide_when_the_facts_are_present() -> None:
    """The matchers are not decorative: with facts available they carry a real decision."""
    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="all-analytics"),
            exclude(1, "pii=high", matcher_kind=MatcherKind.TAG, rule_id="no-high-pii"),
            include(2, "data-eng@acme.com", matcher_kind=MatcherKind.OWNER, rule_id="team-owned"),
        ]
    )
    plain = schema_candidate("analytics.sales", tags=tags("domain=finance"), owners=owners())
    sensitive = schema_candidate("analytics.hr", tags=tags("pii=high"), owners=owners())
    sensitive_but_owned = schema_candidate(
        "analytics.hr", tags=tags("pii=high"), owners=owners("data-eng@acme.com")
    )

    assert evaluate(rule_set, plain).rule_id == "all-analytics"
    assert not evaluate(rule_set, sensitive).included
    assert evaluate(rule_set, sensitive).rule_id == "no-high-pii"
    assert evaluate(rule_set, sensitive_but_owned).included
    assert evaluate(rule_set, sensitive_but_owned).rule_id == "team-owned"


# --------------------------------------------------------------------------------------
# Two scopes, kept apart (C5)
# --------------------------------------------------------------------------------------


def test_object_scope_rules_do_not_decide_dataset_candidates() -> None:
    rule_set = SelectionRuleSet.build([include(0, "analytics.*", rule_id="object-rule")])
    result = evaluate(rule_set, dataset_candidate("analytics.sales.orders"))
    assert not result.included
    assert result.source is DecisionSource.DEFAULT
    assert result.undetermined == ()


def test_dataset_scope_rules_do_not_decide_object_candidates() -> None:
    rule_set = SelectionRuleSet.build(
        [include(0, "analytics.sales.*", scope=RuleScope.DATASET, rule_id="dataset-rule")]
    )
    result = evaluate(rule_set, schema_candidate("analytics.sales"))
    assert not result.included
    assert result.source is DecisionSource.DEFAULT


def test_scope_separation_holds_for_matchers_that_have_no_shape_to_give_it_away() -> None:
    """A tag or owner rule reads a fact, not a name, so nothing about the candidate stops it
    firing in the wrong scope -- only the partition by scope does.

    Glob rules are accidentally protected: an object pattern has two segments and a dataset
    name has three, so they cannot match each other even if the scopes were ignored. Tag and
    owner rules have no such shape, which makes this the test that actually pins C5.
    """
    object_tag_rule = SelectionRuleSet.build(
        [include(0, "curated", matcher_kind=MatcherKind.TAG, rule_id="object-tag")]
    )
    dataset_owner_rule = SelectionRuleSet.build(
        [
            include(
                0,
                "*@acme.com",
                scope=RuleScope.DATASET,
                matcher_kind=MatcherKind.OWNER,
                rule_id="dataset-owner",
            )
        ]
    )
    tagged_table = dataset_candidate(
        "analytics.sales.orders", tags=tags("curated"), owners=owners("a@acme.com")
    )
    owned_schema = schema_candidate(
        "analytics.sales", tags=tags("curated"), owners=owners("a@acme.com")
    )

    from_object_rules = evaluate(object_tag_rule, tagged_table)
    from_dataset_rules = evaluate(dataset_owner_rule, owned_schema)

    assert not from_object_rules.included
    assert from_object_rules.source is DecisionSource.DEFAULT
    assert from_object_rules.undetermined == ()
    assert not from_dataset_rules.included
    assert from_dataset_rules.source is DecisionSource.DEFAULT
    assert from_dataset_rules.undetermined == ()


def test_the_two_scopes_are_evaluated_independently_in_one_stream() -> None:
    """The console's tree mixes schemas and tables; one call handles both, each by its own rules."""
    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="schemas"),
            include(0, "analytics.sales.*", scope=RuleScope.DATASET, rule_id="tables"),
            exclude(1, "analytics.sales.tmp*", scope=RuleScope.DATASET, rule_id="no-tmp-tables"),
        ]
    )
    results = list(
        evaluate_all(
            rule_set,
            [
                schema_candidate("analytics.sales"),
                dataset_candidate("analytics.sales.orders"),
                dataset_candidate("analytics.sales.tmp_stage"),
                dataset_candidate("analytics.finance.ledger"),
            ],
        )
    )
    assert decisions(results) == [
        ("analytics.sales", True, "schemas"),
        ("analytics.sales.orders", True, "tables"),
        ("analytics.sales.tmp_stage", False, "no-tmp-tables"),
        ("analytics.finance.ledger", False, None),
    ]


# --------------------------------------------------------------------------------------
# One code path (C4), and it is pure
# --------------------------------------------------------------------------------------


def test_evaluate_all_is_evaluate_in_a_loop() -> None:
    """The bulk path a preview uses and the single call the loop uses are the same function."""
    rule_set = SelectionRuleSet.build(
        [include(0, "analytics.*", rule_id="inc"), exclude(1, "analytics.tmp*", rule_id="exc")]
    )
    candidates = [
        schema_candidate("analytics.sales"),
        schema_candidate("analytics.tmp_a"),
        schema_candidate("finance.sales"),
    ]
    assert list(evaluate_all(rule_set, candidates)) == [
        evaluate(rule_set, candidate) for candidate in candidates
    ]


def test_evaluate_all_is_lazy() -> None:
    """A preview over a paging source must not have to materialize the whole tree first."""
    consumed: list[str] = []

    def stream() -> object:
        for name in ("analytics.a", "analytics.b", "analytics.c"):
            consumed.append(name)
            yield schema_candidate(name)

    rule_set = SelectionRuleSet.build([include(0, "analytics.*")])
    results = evaluate_all(rule_set, stream())  # type: ignore[arg-type]
    assert consumed == []
    next(results)
    assert consumed == ["analytics.a"]


def test_evaluating_twice_gives_the_same_answer() -> None:
    rule_set = SelectionRuleSet.build(
        [include(0, "analytics.*", rule_id="inc"), exclude(1, "analytics.tmp*", rule_id="exc")]
    )
    candidate = schema_candidate("analytics.tmp_a")
    assert evaluate(rule_set, candidate) == evaluate(rule_set, candidate)


def test_the_order_candidates_arrive_in_does_not_change_any_decision() -> None:
    """Evaluation carries no state between candidates -- shuffling the stream proves it."""
    rule_set = SelectionRuleSet.build(
        [
            include(0, "analytics.*", rule_id="inc"),
            exclude(1, "analytics.tmp*", rule_id="exc"),
            include(2, "warehouse.prod", rule_id="wh"),
        ]
    )
    candidates = [
        schema_candidate(f"{catalog}.{schema}")
        for catalog in ("analytics", "warehouse", "finance")
        for schema in ("sales", "tmp_a", "prod")
    ]
    straight = {
        result.candidate.object_id: (result.included, result.rule_id)
        for result in evaluate_all(rule_set, candidates)
    }
    shuffled = list(candidates)
    random.Random(20260820).shuffle(shuffled)
    reversed_order = {
        result.candidate.object_id: (result.included, result.rule_id)
        for result in evaluate_all(rule_set, shuffled)
    }
    assert straight == reversed_order


# --------------------------------------------------------------------------------------
# A randomized cross-check against an independent oracle
# --------------------------------------------------------------------------------------


def _oracle(rules: Sequence[SelectionRule], catalog: str, schema: str) -> tuple[bool, str | None]:
    """Last-match-wins, written a completely different way: filter, then take the last.

    Independent of the evaluator's running-``deciding`` loop, which is the point -- it is a
    check on the implementation, not a copy of it. Glob-only, so it needs nothing but
    ``fnmatch``.
    """
    matching = [
        item
        for item in sorted(rules, key=lambda r: r.ordinal)
        if fnmatch.fnmatchcase(catalog, item.pattern.split(".")[0])
        and fnmatch.fnmatchcase(schema, item.pattern.split(".")[1])
    ]
    if not matching:
        return False, None
    last = matching[-1]
    return last.decision is SelectionDecision.INCLUDE, last.rule_id


def test_random_glob_rule_sets_agree_with_an_independent_last_match_oracle() -> None:
    """Order-dependence claims are worth a randomized check; a fixed seed keeps it a test.

    Fails against first-match-wins, against any-exclude-wins, and against a default-include
    -- none of which the hand-written cases above can rule out for every shape of rule set.
    """
    rng = random.Random(20260820)
    catalogs = ["analytics", "analytics_eu", "finance", "warehouse"]
    schemas = ["sales", "staging", "prod_staging", "tmp_a", "finance"]
    patterns = [
        "analytics.*",
        "analytics.staging*",
        "analytics.*staging*",
        "*.tmp*",
        "*.*",
        "finance.finance",
        "analytics_eu.*",
        "*.prod_staging",
    ]

    for trial in range(60):
        chosen = rng.sample(patterns, rng.randint(1, len(patterns)))
        rules = [
            SelectionRule(
                rule_id=f"t{trial}-r{index}",
                ordinal=index,
                scope=RuleScope.OBJECT,
                decision=rng.choice([SelectionDecision.INCLUDE, SelectionDecision.EXCLUDE]),
                matcher_kind=MatcherKind.GLOB,
                pattern=pattern,
            )
            for index, pattern in enumerate(chosen)
        ]
        rule_set = SelectionRuleSet.build(rules)
        for catalog in catalogs:
            for schema in schemas:
                result = evaluate(rule_set, schema_candidate(f"{catalog}.{schema}"))
                assert (result.included, result.rule_id) == _oracle(rules, catalog, schema), (
                    f"trial {trial}, {catalog}.{schema}, rules "
                    f"{[(r.ordinal, r.decision.value, r.pattern) for r in rules]}"
                )


# --------------------------------------------------------------------------------------
# Explanations the console and the run report can show
# --------------------------------------------------------------------------------------


def test_explanations_name_what_decided() -> None:
    rule_set = SelectionRuleSet.build(
        rules=[
            include(0, "analytics.*", rule_id="inc"),
            exclude(1, "analytics.tmp*", rule_id="exc"),
        ],
        overrides=[override("analytics.pinned", SelectionDecision.INCLUDE, reason="pilot")],
    )
    assert (
        evaluate(rule_set, schema_candidate("analytics.sales")).explain()
        == "included by rule #0 include glob 'analytics.*'"
    )
    assert (
        evaluate(rule_set, schema_candidate("analytics.tmp_a")).explain()
        == "excluded by rule #1 exclude glob 'analytics.tmp*'"
    )
    assert (
        evaluate(rule_set, schema_candidate("analytics.pinned")).explain()
        == "included by override include 'analytics.pinned' (pilot)"
    )
    assert (
        evaluate(rule_set, schema_candidate("finance.sales")).explain()
        == "excluded by default (no rule matched)"
    )
