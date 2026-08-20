"""Selection: which source objects a pair syncs, and which rule said so (C3, C4, C5).

WP11. The ordered include/exclude rule set that supersedes RM-01 decision D1's flat glob
list, and the single pure evaluator both the sync loop (T11.3) and the console's preview
(T12.5) decide through.

Two modules:

* :mod:`qlabs_catalog_sync.selection.rules` -- the immutable value types (candidates,
  rules, overrides, compiled matchers) and the converters from T10.1's persisted rows.
  Read its module docstring before writing a caller: it states why a candidate's
  fully-qualified name is a required argument rather than something the evaluator digs out
  of a ``ChangeRef``, and why an *unknown* tag set is not an empty one.
* :mod:`qlabs_catalog_sync.selection.evaluator` -- :func:`evaluate` (one candidate) and
  :func:`evaluate_all` (a lazy stream of them, implemented as :func:`evaluate` in a loop,
  so there is exactly one code path).

The vocabulary -- :class:`RuleScope`, :class:`SelectionDecision`, :class:`MatcherKind` --
is re-exported from :mod:`qlabs_catalog_sync.configstore.types`, not redefined: these are
the same enums the ``selection_rules`` and ``selection_overrides`` tables store.
"""

from qlabs_catalog_sync.configstore.types import MatcherKind, RuleScope, SelectionDecision
from qlabs_catalog_sync.selection.evaluator import (
    DecisionSource,
    SelectionResult,
    UndeterminedRule,
    evaluate,
    evaluate_all,
)
from qlabs_catalog_sync.selection.rules import (
    DEFAULT_DECISION,
    SEGMENTS_BY_SCOPE,
    UNKNOWN,
    CandidateFact,
    CompiledRule,
    Matcher,
    MatchOutcome,
    OwnerFacts,
    QualifiedName,
    SelectionCandidate,
    SelectionOverride,
    SelectionRule,
    SelectionRuleSet,
    TagFacts,
    Unknown,
    compile_matcher,
    object_rules_from_catalog_schema_patterns,
    validate_pattern,
)

__all__ = [
    "DEFAULT_DECISION",
    "SEGMENTS_BY_SCOPE",
    "UNKNOWN",
    "CandidateFact",
    "CompiledRule",
    "DecisionSource",
    "MatchOutcome",
    "Matcher",
    "MatcherKind",
    "OwnerFacts",
    "QualifiedName",
    "RuleScope",
    "SelectionCandidate",
    "SelectionDecision",
    "SelectionOverride",
    "SelectionResult",
    "SelectionRule",
    "SelectionRuleSet",
    "TagFacts",
    "UndeterminedRule",
    "Unknown",
    "compile_matcher",
    "evaluate",
    "evaluate_all",
    "object_rules_from_catalog_schema_patterns",
    "validate_pattern",
]
