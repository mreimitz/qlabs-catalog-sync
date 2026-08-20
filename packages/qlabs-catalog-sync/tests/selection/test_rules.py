"""T11.1 -- the selection rule *model*: grammar, validation, and the contradictions it refuses.

The evaluator's own behaviour lives in ``test_evaluator.py``. What is pinned here is the
shape of the inputs: that a glob means in the console exactly what it means in YAML, that a
candidate cannot be built with a name whose shape nobody checked, and that a rule set which
cannot be evaluated deterministically is rejected at build time instead of resolved by
whichever rule happened to come last in a dict.
"""

from __future__ import annotations

import re
import uuid

import pytest
from selection_helpers import (
    dataset_candidate,
    exclude,
    include,
    override,
    override_row,
    owners,
    rule,
    rule_row,
    schema_candidate,
    tags,
)

from qlabs_catalog_sync.config import (
    _validate_catalog_schema_pattern,
    matches_catalog_schema,
)
from qlabs_catalog_sync.selection import (
    DEFAULT_DECISION,
    SEGMENTS_BY_SCOPE,
    UNKNOWN,
    MatcherKind,
    MatchOutcome,
    RuleScope,
    SelectionCandidate,
    SelectionDecision,
    SelectionOverride,
    SelectionRule,
    SelectionRuleSet,
    compile_matcher,
    object_rules_from_catalog_schema_patterns,
    validate_pattern,
)
from qlabs_catalog_sync.selection.rules import _GlobMatcher
from qlabs_catalog_sync_sdk.models import Party, PartyRole, Tag

# --------------------------------------------------------------------------------------
# The default, stated as a test so it cannot be flipped inattentively
# --------------------------------------------------------------------------------------


def test_default_decision_is_exclude() -> None:
    """Nothing is selected unless a rule says so.

    The filter this rule set replaces defaulted to *include* when it could not read an
    object's name, and a pair scoped to one catalog synced an entire metastore. This
    constant is the load-bearing half of that fix; the other half is that a candidate must
    say ``UNKNOWN`` out loud rather than arriving nameless.
    """
    assert DEFAULT_DECISION is SelectionDecision.EXCLUDE


# --------------------------------------------------------------------------------------
# Glob grammar: parity with config.py's D1 patterns, in both directions
# --------------------------------------------------------------------------------------

#: Patterns spanning what a D1 ``catalog.schema`` selector may look like, valid and not.
#: Both validators must agree on every one of them; which of the two is "right" is not the
#: point -- a rule typed into the console and a pattern written in YAML meaning different
#: things is the trap, and only a two-directional check catches it.
_PARITY_PATTERNS = [
    "sales.orders",
    "sales.*",
    "*.*",
    "prod_*.finance",
    "sales.?rders",
    "sales.[abc]*",
    "sales.[!x]*",
    "SALES.Orders",
    "s.o",
    "",
    ".",
    "sales",
    "sales.",
    ".orders",
    "sales.orders.items",
    "sales.or ders",
    "sales.or-ders",
    "sales.órders",
    "sales.orders#",
]


def _accepts(validate: object, pattern: str) -> bool:
    assert callable(validate)
    try:
        validate(pattern)
    except ValueError:
        return False
    return True


@pytest.mark.parametrize("pattern", _PARITY_PATTERNS)
def test_object_scope_glob_grammar_matches_config_d1_grammar(pattern: str) -> None:
    """Object scope accepts exactly the patterns ``config.py`` accepts for D1, and no others."""
    theirs = _accepts(_validate_catalog_schema_pattern, pattern)
    ours = _accepts(
        lambda value: validate_pattern(
            value, matcher_kind=MatcherKind.GLOB, scope=RuleScope.OBJECT
        ),
        pattern,
    )
    assert ours == theirs, (
        f"{pattern!r}: config.py {'accepts' if theirs else 'rejects'} it but the selection "
        f"rule grammar {'accepts' if ours else 'rejects'} it"
    )


#: ``(pattern, catalog, schema)`` triples, weighted towards the cases where a hand-rolled
#: glob implementation and ``fnmatch`` part company.
_MATCH_TABLE = [
    ("sales.*", "sales", "orders"),
    ("sales.*", "sales", ""),
    ("sales.*", "Sales", "orders"),
    ("sales.*", "sales_eu", "orders"),
    ("sales*.orders", "sales_eu", "orders"),
    ("*.orders", "anything", "orders"),
    ("*.*", "a", "b"),
    ("sales.?rders", "sales", "orders"),
    ("sales.?rders", "sales", "oorders"),
    ("sales.[abc]*", "sales", "beta"),
    ("sales.[abc]*", "sales", "zeta"),
    ("sales.[!x]*", "sales", "orders"),
    ("sales.[!x]*", "sales", "xenon"),
    ("analytics.*staging*", "analytics", "prod_staging"),
    ("analytics.staging*", "analytics", "prod_staging"),
    ("analytics.*", "analytics_archive", "sales"),
]


@pytest.mark.parametrize(("pattern", "catalog", "schema"), _MATCH_TABLE)
def test_object_scope_glob_matching_matches_config_d1_matching(
    pattern: str, catalog: str, schema: str
) -> None:
    """Segment-wise matching agrees with ``matches_catalog_schema`` case for case."""
    matcher = compile_matcher(
        matcher_kind=MatcherKind.GLOB, pattern=pattern, scope=RuleScope.OBJECT
    )
    if not catalog or not schema:
        # config.py matches against arbitrary strings; a candidate must have non-empty
        # segments, so this row only exercises the D1 side. Assert that much and stop.
        assert matches_catalog_schema(pattern, catalog, schema) in (True, False)
        return
    candidate = schema_candidate(f"{catalog}.{schema}")
    ours = matcher.evaluate(candidate) is MatchOutcome.MATCH
    assert ours is matches_catalog_schema(pattern, catalog, schema)


def test_glob_matching_is_case_sensitive_like_unity_catalog() -> None:
    """D1 matches case-sensitively because UC identifiers are stored case-sensitively."""
    matcher = compile_matcher(
        matcher_kind=MatcherKind.GLOB, pattern="sales.*", scope=RuleScope.OBJECT
    )
    assert matcher.evaluate(schema_candidate("sales.orders")) is MatchOutcome.MATCH
    assert matcher.evaluate(schema_candidate("Sales.orders")) is MatchOutcome.NO_MATCH


# --------------------------------------------------------------------------------------
# Glob grammar: the second scope (C5)
# --------------------------------------------------------------------------------------


def test_scopes_declare_their_segment_counts() -> None:
    assert SEGMENTS_BY_SCOPE[RuleScope.OBJECT] == 2
    assert SEGMENTS_BY_SCOPE[RuleScope.DATASET] == 3


def test_dataset_scope_requires_three_segments() -> None:
    validate_pattern(
        "analytics.sales.*", matcher_kind=MatcherKind.GLOB, scope=RuleScope.DATASET
    )
    with pytest.raises(ValueError, match="exactly 2 '\\.'"):
        validate_pattern("analytics.*", matcher_kind=MatcherKind.GLOB, scope=RuleScope.DATASET)


def test_object_scope_rejects_a_three_segment_pattern() -> None:
    with pytest.raises(ValueError, match="exactly 1 '\\.'"):
        validate_pattern(
            "analytics.sales.orders", matcher_kind=MatcherKind.GLOB, scope=RuleScope.OBJECT
        )


def test_a_two_segment_pattern_is_not_quietly_read_as_all_tables_in_a_schema() -> None:
    """``analytics.sales`` in dataset scope is an error, not "every table in sales".

    Two meanings for one piece of text is how an operator ends up syncing something they
    did not ask for. ``analytics.sales.*`` says it explicitly and is what they must write.
    """
    with pytest.raises(ValueError):
        rule(0, SelectionDecision.INCLUDE, "analytics.sales", scope=RuleScope.DATASET)

    ok = rule(0, SelectionDecision.INCLUDE, "analytics.sales.*", scope=RuleScope.DATASET)
    matcher = compile_matcher(
        matcher_kind=ok.matcher_kind, pattern=ok.pattern, scope=ok.scope
    )
    assert matcher.evaluate(dataset_candidate("analytics.sales.orders")) is MatchOutcome.MATCH


# --------------------------------------------------------------------------------------
# Tag and owner pattern grammar
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["pii", "pii=high", "domain=finance", "a_b=c-d"])
def test_valid_tag_patterns(pattern: str) -> None:
    validate_pattern(pattern, matcher_kind=MatcherKind.TAG, scope=RuleScope.OBJECT)


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("", "blank"),
        ("   ", "blank"),
        ("=high", "empty tag key"),
        ("pii=", "empty tag value"),
        ("a=b=c", "more than one"),
    ],
)
def test_invalid_tag_patterns(pattern: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        validate_pattern(pattern, matcher_kind=MatcherKind.TAG, scope=RuleScope.OBJECT)


def test_key_only_tag_pattern_matches_any_value_but_key_value_needs_the_value() -> None:
    key_only = compile_matcher(
        matcher_kind=MatcherKind.TAG, pattern="pii", scope=RuleScope.OBJECT
    )
    key_value = compile_matcher(
        matcher_kind=MatcherKind.TAG, pattern="pii=high", scope=RuleScope.OBJECT
    )
    labelled = schema_candidate("a.b", tags=tags("pii"))
    valued = schema_candidate("a.b", tags=tags("pii=high"))
    other = schema_candidate("a.b", tags=tags("pii=low"))

    assert key_only.evaluate(labelled) is MatchOutcome.MATCH
    assert key_only.evaluate(valued) is MatchOutcome.MATCH
    assert key_only.evaluate(other) is MatchOutcome.MATCH

    assert key_value.evaluate(valued) is MatchOutcome.MATCH
    assert key_value.evaluate(other) is MatchOutcome.NO_MATCH
    assert key_value.evaluate(labelled) is MatchOutcome.NO_MATCH


def test_owner_pattern_folds_case_and_supports_a_domain_glob() -> None:
    """Owner matching normalizes e-mails the way ``party.normalize_email`` does (RM-01 D3)."""
    exact = compile_matcher(
        matcher_kind=MatcherKind.OWNER, pattern="Alice@Acme.com", scope=RuleScope.OBJECT
    )
    domain = compile_matcher(
        matcher_kind=MatcherKind.OWNER, pattern="*@acme.com", scope=RuleScope.OBJECT
    )
    alice = schema_candidate("a.b", owners=owners("ALICE@acme.com"))
    bob = schema_candidate("a.b", owners=owners("bob@other.com"))

    assert exact.evaluate(alice) is MatchOutcome.MATCH
    assert exact.evaluate(bob) is MatchOutcome.NO_MATCH
    assert domain.evaluate(alice) is MatchOutcome.MATCH
    assert domain.evaluate(bob) is MatchOutcome.NO_MATCH


def test_only_owner_parties_with_an_email_are_matchable() -> None:
    """A steward is not an owner, and a service-principal id is not an e-mail.

    Both mirror ``qlabs_catalog_sync.party``: Databricks reports an owner as either an
    e-mail or an application id, and guessing that an application id is a person is exactly
    what that module refuses to do.
    """
    matcher = compile_matcher(
        matcher_kind=MatcherKind.OWNER, pattern="*", scope=RuleScope.OBJECT
    )
    steward = schema_candidate("a.b", owners=owners("alice@acme.com", role=PartyRole.STEWARD))
    app_id = schema_candidate(
        "a.b",
        owners=(Party(party_id="1a2b-3c4d-service-principal", role=PartyRole.OWNER),),
    )
    assert steward.owner_emails == frozenset()
    assert matcher.evaluate(steward) is MatchOutcome.NO_MATCH
    assert app_id.owner_emails == frozenset()
    assert matcher.evaluate(app_id) is MatchOutcome.NO_MATCH


@pytest.mark.parametrize("pattern", ["", "   "])
def test_blank_owner_pattern_is_rejected(pattern: str) -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        validate_pattern(pattern, matcher_kind=MatcherKind.OWNER, scope=RuleScope.OBJECT)


# --------------------------------------------------------------------------------------
# Candidates: the qualified name is required, explicit, and shape-checked
# --------------------------------------------------------------------------------------


def test_candidate_keeps_the_stable_id_and_the_name_apart() -> None:
    """A UUID stable id and a dotted name are different fields, and both survive.

    This is the type-level half of the ``full_name`` fix: the caller states which is which
    rather than the evaluator inferring it from whether a string contains a dot.
    """
    candidate = schema_candidate("analytics.sales", object_id="8f14e45f-ceea-467a-9c86-1")
    assert candidate.object_id == "8f14e45f-ceea-467a-9c86-1"
    assert candidate.qualified_name == "analytics.sales"
    assert candidate.name_segments == ("analytics", "sales")
    assert candidate.has_qualified_name


@pytest.mark.parametrize(
    ("scope", "name"),
    [
        (RuleScope.OBJECT, "analytics"),
        (RuleScope.OBJECT, "analytics.sales.orders"),
        (RuleScope.OBJECT, "analytics."),
        (RuleScope.OBJECT, ".sales"),
        (RuleScope.DATASET, "analytics.sales"),
        (RuleScope.DATASET, "analytics.sales.orders.col"),
    ],
)
def test_a_wrong_shaped_qualified_name_is_refused_not_reinterpreted(
    scope: RuleScope, name: str
) -> None:
    """Anything that is not the scope's shape must be passed as ``UNKNOWN`` deliberately."""
    with pytest.raises(ValueError, match="pass UNKNOWN"):
        SelectionCandidate(scope=scope, object_id="id", qualified_name=name)


def test_unknown_is_an_accepted_qualified_name_and_a_required_argument() -> None:
    candidate = SelectionCandidate(
        scope=RuleScope.OBJECT, object_id="opaque-handle", qualified_name=UNKNOWN
    )
    assert candidate.name_segments is None
    assert not candidate.has_qualified_name
    with pytest.raises(TypeError):
        SelectionCandidate(scope=RuleScope.OBJECT, object_id="opaque-handle")  # type: ignore[call-arg]


def test_candidate_facts_default_to_unknown_not_empty() -> None:
    """A caller who never thought about tags gets "cannot say", never a silent "none".

    The opposite default is how a source that cannot report tags (RM-01 D6: Databricks
    needs a SQL warehouse) starts answering "no" to every tag rule, and how a preview
    quietly stops predicting the run.
    """
    candidate = schema_candidate("a.b")
    assert candidate.tags is UNKNOWN
    assert candidate.owners is UNKNOWN
    assert candidate.tag_keys is None
    assert candidate.owner_emails is None

    empty = schema_candidate("a.b", tags=(), owners=())
    assert empty.tags == ()
    assert empty.tag_keys == frozenset()
    assert empty.owner_emails == frozenset()


def test_key_only_tags_are_indexed_by_key_only() -> None:
    candidate = schema_candidate("a.b", tags=(Tag(key="pii"), Tag(key="domain", value="fin")))
    assert candidate.tag_keys == {"pii", "domain"}
    assert candidate.tag_pairs == {("domain", "fin")}


def test_a_blank_stable_id_is_refused() -> None:
    with pytest.raises(ValueError, match="non-empty stable identifier"):
        SelectionCandidate(
            scope=RuleScope.OBJECT, object_id="  ", qualified_name="analytics.sales"
        )


def test_override_keys_try_the_stable_id_before_the_name() -> None:
    candidate = schema_candidate("analytics.sales", object_id="uuid-1")
    assert candidate.override_keys == ("uuid-1", "analytics.sales")

    nameless = SelectionCandidate(
        scope=RuleScope.OBJECT, object_id="uuid-1", qualified_name=UNKNOWN
    )
    assert nameless.override_keys == ("uuid-1",)

    self_keyed = schema_candidate("analytics.sales", object_id="analytics.sales")
    assert self_keyed.override_keys == ("analytics.sales",)


# --------------------------------------------------------------------------------------
# Rules: validated at construction
# --------------------------------------------------------------------------------------


def test_a_rule_validates_its_pattern_when_it_is_built() -> None:
    with pytest.raises(ValueError, match="selection rule 'r0'"):
        include(0, "analytics")


def test_a_rule_needs_a_stable_id_and_a_non_negative_ordinal() -> None:
    with pytest.raises(ValueError, match="non-empty stable identifier"):
        include(0, "a.b", rule_id=" ")
    with pytest.raises(ValueError, match="ordinal must be >= 0"):
        include(-1, "a.b")


def test_a_rule_reports_the_fact_its_matcher_needs() -> None:
    from qlabs_catalog_sync.selection import CandidateFact

    assert include(0, "a.b").required_fact is CandidateFact.QUALIFIED_NAME
    assert include(0, "pii", matcher_kind=MatcherKind.TAG).required_fact is CandidateFact.TAGS
    assert (
        include(0, "a@b.com", matcher_kind=MatcherKind.OWNER).required_fact
        is CandidateFact.OWNERS
    )


def test_rules_and_overrides_describe_themselves_for_the_console() -> None:
    assert include(2, "analytics.*").describe() == "rule #2 include glob 'analytics.*'"
    assert (
        override("analytics.prod_staging", SelectionDecision.INCLUDE, reason="pilot").describe()
        == "override include 'analytics.prod_staging' (pilot)"
    )


# --------------------------------------------------------------------------------------
# Building a rule set: ordering, and the contradictions it refuses
# --------------------------------------------------------------------------------------


def test_rules_are_ordered_by_ordinal_not_by_input_order() -> None:
    rule_set = SelectionRuleSet.build([include(2, "a.*"), exclude(0, "b.*"), include(1, "c.*")])
    assert [compiled.rule.ordinal for compiled in rule_set.rules_for(RuleScope.OBJECT)] == [
        0,
        1,
        2,
    ]


def test_two_rules_sharing_an_ordinal_in_one_scope_are_rejected_loudly() -> None:
    """The T10.1 schema refuses to store this; an evaluator handed it must not guess.

    ``uq_selection_rules_pair_id_scope_ordinal`` makes it unstorable, so reaching the
    evaluator means something built the set by hand. Ordering *is* the meaning of a rule
    set, so there is no defensible way to pick one.
    """
    with pytest.raises(ValueError, match="share ordinal 1"):
        SelectionRuleSet.build(
            [include(1, "a.*", rule_id="first"), exclude(1, "b.*", rule_id="second")]
        )


def test_the_same_ordinal_in_different_scopes_is_fine() -> None:
    """The unique constraint is per ``(pair, scope)``; the two scopes are separate lists."""
    rule_set = SelectionRuleSet.build(
        [include(0, "a.*"), include(0, "a.b.*", scope=RuleScope.DATASET, rule_id="d0")]
    )
    assert len(rule_set.rules_for(RuleScope.OBJECT)) == 1
    assert len(rule_set.rules_for(RuleScope.DATASET)) == 1


def test_two_rules_sharing_a_rule_id_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate rule_id 'same'"):
        SelectionRuleSet.build(
            [include(0, "a.*", rule_id="same"), include(1, "b.*", rule_id="same")]
        )


def test_two_overrides_pinning_the_same_object_in_one_scope_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate object-scope override"):
        SelectionRuleSet.build(
            overrides=[
                override("analytics.sales", SelectionDecision.INCLUDE),
                override("analytics.sales", SelectionDecision.EXCLUDE),
            ]
        )


def test_the_same_object_id_pinned_in_both_scopes_is_fine() -> None:
    rule_set = SelectionRuleSet.build(
        overrides=[
            override("x", SelectionDecision.INCLUDE),
            override("x", SelectionDecision.EXCLUDE, scope=RuleScope.DATASET),
        ]
    )
    assert rule_set.override_for(schema_candidate("a.b", object_id="x")) == SelectionOverride(
        scope=RuleScope.OBJECT, object_id="x", decision=SelectionDecision.INCLUDE
    )


def test_an_empty_rule_set_is_reportably_empty() -> None:
    empty = SelectionRuleSet.build()
    assert empty.is_empty()
    assert empty.is_empty(RuleScope.OBJECT)
    assert empty.rules_for(RuleScope.DATASET) == ()

    partial = SelectionRuleSet.build([include(0, "a.*")])
    assert not partial.is_empty()
    assert not partial.is_empty(RuleScope.OBJECT)
    assert partial.is_empty(RuleScope.DATASET)


# --------------------------------------------------------------------------------------
# Compiled once, not once per candidate
# --------------------------------------------------------------------------------------


def test_patterns_are_compiled_at_build_time_and_reused_across_candidates() -> None:
    """The console previews counts over a whole catalog; compilation must be outside the loop.

    Identity, not timing: ``rules_for`` hands back the very same compiled objects on every
    call, so evaluating N candidates cannot be recompiling N times.
    """
    rule_set = SelectionRuleSet.build([include(0, "analytics.*"), exclude(1, "analytics.tmp*")])
    first = rule_set.rules_for(RuleScope.OBJECT)
    second = rule_set.rules_for(RuleScope.OBJECT)
    assert first is second
    assert all(a.matcher is b.matcher for a, b in zip(first, second, strict=True))

    matchers_before = [compiled.matcher for compiled in first]
    for index in range(200):
        rule_set.rules_for(RuleScope.OBJECT)[0].evaluate(schema_candidate(f"analytics.s{index}"))
    assert [compiled.matcher for compiled in rule_set.rules_for(RuleScope.OBJECT)] == (
        matchers_before
    )


def test_a_compiled_glob_is_a_precompiled_regex_per_segment() -> None:
    """Not an implementation detail: it is what makes bulk evaluation linear in candidates."""
    matcher = compile_matcher(
        matcher_kind=MatcherKind.GLOB, pattern="analytics.*", scope=RuleScope.OBJECT
    )
    assert isinstance(matcher, _GlobMatcher)
    assert len(matcher.segments) == 2
    assert all(isinstance(segment, re.Pattern) for segment in matcher.segments)


# --------------------------------------------------------------------------------------
# D1's flat glob list is one include rule per pattern (C3)
# --------------------------------------------------------------------------------------


def test_object_rules_from_catalog_schema_patterns_is_one_include_rule_per_pattern() -> None:
    built = object_rules_from_catalog_schema_patterns(["sales.*", "analytics.finance"])
    assert [(item.ordinal, item.decision, item.matcher_kind, item.pattern) for item in built] == [
        (0, SelectionDecision.INCLUDE, MatcherKind.GLOB, "sales.*"),
        (1, SelectionDecision.INCLUDE, MatcherKind.GLOB, "analytics.finance"),
    ]
    assert all(item.scope is RuleScope.OBJECT for item in built)
    assert len({item.rule_id for item in built}) == 2


def test_an_invalid_d1_pattern_still_fails_when_converted_to_a_rule() -> None:
    with pytest.raises(ValueError):
        object_rules_from_catalog_schema_patterns(["sales"])


# --------------------------------------------------------------------------------------
# Converting persisted rows (T10.1)
# --------------------------------------------------------------------------------------


def test_a_persisted_rule_row_converts_to_the_in_memory_rule() -> None:
    row_id = uuid.uuid4()
    row = rule_row(3, SelectionDecision.EXCLUDE, "analytics.tmp*", row_id=row_id)
    converted = SelectionRule.from_row(row)
    assert converted == SelectionRule(
        rule_id=str(row_id),
        ordinal=3,
        scope=RuleScope.OBJECT,
        decision=SelectionDecision.EXCLUDE,
        matcher_kind=MatcherKind.GLOB,
        pattern="analytics.tmp*",
    )


def test_a_persisted_override_row_converts_to_the_in_memory_override() -> None:
    row = override_row(
        "analytics.prod_staging", SelectionDecision.INCLUDE, reason="pilot schema"
    )
    assert SelectionOverride.from_row(row) == SelectionOverride(
        scope=RuleScope.OBJECT,
        object_id="analytics.prod_staging",
        decision=SelectionDecision.INCLUDE,
        reason="pilot schema",
    )


def test_a_rule_set_builds_straight_from_rows() -> None:
    pair = uuid.uuid4()
    rule_set = SelectionRuleSet.from_rows(
        rule_rows=[
            rule_row(1, SelectionDecision.EXCLUDE, "analytics.tmp*", pair_id=pair),
            rule_row(0, SelectionDecision.INCLUDE, "analytics.*", pair_id=pair),
        ],
        override_rows=[
            override_row("analytics.tmp_keep", SelectionDecision.INCLUDE, pair_id=pair)
        ],
    )
    ordered = rule_set.rules_for(RuleScope.OBJECT)
    assert [compiled.rule.pattern for compiled in ordered] == ["analytics.*", "analytics.tmp*"]
    pinned = rule_set.override_for(schema_candidate("analytics.tmp_keep"))
    assert pinned is not None
    assert pinned.decision is SelectionDecision.INCLUDE


def test_the_rule_id_is_the_row_id_so_it_survives_reordering() -> None:
    """Reordering rewrites ordinals; the console still needs to point at "that rule"."""
    row = rule_row(0, SelectionDecision.INCLUDE, "analytics.*")
    before = SelectionRule.from_row(row)
    row.ordinal = 7
    after = SelectionRule.from_row(row)
    assert before.rule_id == after.rule_id
    assert before.ordinal != after.ordinal
