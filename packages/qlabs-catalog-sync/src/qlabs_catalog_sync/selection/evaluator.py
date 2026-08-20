"""The one selection evaluator, shared by the sync loop and the console preview (C4).

WP11 / T11.1. A pure function over ``(rule set, candidate)``: same inputs, same outputs,
always. No I/O, no clock, no environment, no database, no logging. It imports nothing but
``qlabs_catalog_sync.selection.rules`` and the standard library -- deliberately not the
engine's contract types, because the moment this module starts reaching into a
``ChangeRef`` to work out what an object is called, it has taken back the guessing that
caused the defect ``rules.py``'s module docstring describes.

Why there is exactly one of these
---------------------------------

C4: *"The sync loop and the console's preview call the same implementation. A preview that
can disagree with the run it predicts is worse than no preview, so there is exactly one
code path."* Concretely:

* T11.3's sync loop calls :func:`evaluate` once per change it is considering, and turns a
  non-inclusion into a ``FILTERED`` record whose detail is :meth:`SelectionResult.explain`;
* T12.5's preview route calls :func:`evaluate_all` over the source tree and counts.

:func:`evaluate_all` is a generator that yields :func:`evaluate` for each candidate and
does nothing else, so "the bulk path" is not a second implementation that could drift --
it is the same function in a loop. The two callers differ only in what they pass in and
what they do with the answers.

The algorithm
-------------

For one candidate, in order:

1. **An override wins outright.** If the rule set pins this candidate (by stable id, else
   by qualified name), that decision stands and no rule is consulted at all -- including a
   rule that comes after it in ordinal order, and including an exclude-everything rule.
2. **Otherwise every rule of the candidate's own scope is tested, low ordinal to high, and
   the last one that matched decides.** The loop does not stop at the first match: first
   match wins and last match wins are different systems, and C3 chose the latter so an
   operator can carve an exception out of a broad rule by appending to the list.
3. **If nothing matched, the answer is**
   :data:`~qlabs_catalog_sync.selection.rules.DEFAULT_DECISION` -- exclude. An empty rule
   set therefore selects nothing. See that constant for the three reasons, the first of
   which is that the bug this replaces was a silent default-*include*.

Rules of the other scope never participate (C5): object-scope rules decide which
``catalog.schema`` become data products, dataset-scope rules decide which tables and views
inside a selected schema become that product's members, and a candidate is only ever
measured against rules of its own scope.

What "undetermined" means, and why the caller must not ignore it
---------------------------------------------------------------

A rule whose matcher cannot reach a verdict -- a tag rule against a source that cannot
report tags (RM-01 D6), a glob rule against a candidate with no qualified name -- is
neither a match nor a non-match. It is collected into
:attr:`SelectionResult.undetermined` and has no effect on the decision, so a source that
cannot answer never silently answers "no".

Every undetermined rule of the scope is reported, including ones ordered *after* the rule
that decided: any of them could have changed the outcome had the fact been available, and
that is precisely the situation in which a preview and a run can disagree. A console
showing counts should surface it; a run recording a filtered object should carry it.

Composition across scopes is the caller's job
---------------------------------------------

This function answers exactly one question about exactly one candidate. C5's "tables
inside a *selected* schema" is a two-step question, and joining the two steps -- evaluate
the schema, and only enumerate its datasets if it was included -- belongs to the source
tree (T11.2), which both the loop and the preview go through. Nothing here knows a
dataset's parent, and it does not invent one.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum

from qlabs_catalog_sync.configstore.types import SelectionDecision
from qlabs_catalog_sync.selection.rules import (
    DEFAULT_DECISION,
    CandidateFact,
    MatchOutcome,
    SelectionCandidate,
    SelectionOverride,
    SelectionRule,
    SelectionRuleSet,
)

__all__ = [
    "DecisionSource",
    "SelectionResult",
    "UndeterminedRule",
    "evaluate",
    "evaluate_all",
]


class DecisionSource(StrEnum):
    """What produced a decision -- the third value is why "no rule matched" is not a guess."""

    OVERRIDE = "override"
    """A per-object pin decided, and no rule was consulted."""

    RULE = "rule"
    """The last matching rule of the candidate's scope decided."""

    DEFAULT = "default"
    """No override and no matching rule: :data:`DEFAULT_DECISION` applies."""


@dataclass(frozen=True, slots=True)
class UndeterminedRule:
    """A rule that could not be evaluated because the candidate cannot supply a fact."""

    rule: SelectionRule
    missing: CandidateFact

    def explain(self) -> str:
        """e.g. ``"rule #3 exclude tag 'pii' could not be evaluated: tags unknown"``."""
        return f"{self.rule.describe()} could not be evaluated: {self.missing.value} unknown"


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """One candidate's decision **and** what produced it (C4).

    :attr:`rule` is the *deciding* rule -- the last one that matched, not the last one
    evaluated -- identified by :attr:`SelectionRule.rule_id`, which is stable across
    reordering so a console can highlight it after the operator has dragged the list
    around.
    """

    candidate: SelectionCandidate
    decision: SelectionDecision
    source: DecisionSource
    rule: SelectionRule | None = None
    override: SelectionOverride | None = None
    undetermined: tuple[UndeterminedRule, ...] = ()

    @property
    def included(self) -> bool:
        """Whether this candidate is selected."""
        return self.decision is SelectionDecision.INCLUDE

    @property
    def rule_id(self) -> str | None:
        """The deciding rule's stable id, or ``None`` when an override or the default decided."""
        return self.rule.rule_id if self.rule is not None else None

    @property
    def has_undetermined(self) -> bool:
        """Whether any rule in scope could not be evaluated against this candidate."""
        return bool(self.undetermined)

    def explain(self) -> str:
        """A one-line, human-readable reason, suitable for a run report or a console row."""
        if self.source is DecisionSource.OVERRIDE and self.override is not None:
            reason = f"{self.decision.value}d by {self.override.describe()}"
        elif self.source is DecisionSource.RULE and self.rule is not None:
            reason = f"{self.decision.value}d by {self.rule.describe()}"
        else:
            reason = f"{self.decision.value}d by default (no rule matched)"
        if self.undetermined:
            reason += f"; {len(self.undetermined)} rule(s) undetermined: " + "; ".join(
                item.explain() for item in self.undetermined
            )
        return reason


def evaluate(rule_set: SelectionRuleSet, candidate: SelectionCandidate) -> SelectionResult:
    """Decide ``candidate`` against ``rule_set``, and report what decided it.

    The single shared code path: T11.3's sync loop calls this per change, T12.5's preview
    calls it (through :func:`evaluate_all`) per node of the source tree. Pure and total --
    it performs no I/O and raises nothing for any well-formed input, since every
    contradiction is refused earlier, when the rules and candidates are constructed.
    """
    override = rule_set.override_for(candidate)
    if override is not None:
        return SelectionResult(
            candidate=candidate,
            decision=override.decision,
            source=DecisionSource.OVERRIDE,
            override=override,
        )

    deciding: SelectionRule | None = None
    undetermined: list[UndeterminedRule] = []
    for compiled in rule_set.rules_for(candidate.scope):
        outcome = compiled.evaluate(candidate)
        if outcome is MatchOutcome.MATCH:
            deciding = compiled.rule  # last match wins: keep going, do not stop here
        elif outcome is MatchOutcome.UNAVAILABLE:
            undetermined.append(UndeterminedRule(compiled.rule, compiled.rule.required_fact))

    if deciding is None:
        return SelectionResult(
            candidate=candidate,
            decision=DEFAULT_DECISION,
            source=DecisionSource.DEFAULT,
            undetermined=tuple(undetermined),
        )
    return SelectionResult(
        candidate=candidate,
        decision=deciding.decision,
        source=DecisionSource.RULE,
        rule=deciding,
        undetermined=tuple(undetermined),
    )


def evaluate_all(
    rule_set: SelectionRuleSet, candidates: Iterable[SelectionCandidate]
) -> Iterator[SelectionResult]:
    """Yield :func:`evaluate` for each candidate, lazily and in the order given.

    Lazy so a console preview over a large catalog can count as the source tree pages
    rather than materializing every node first, and a thin generator over :func:`evaluate`
    rather than a second implementation, so the bulk path cannot drift from the per-object
    one. Candidates of both scopes may be mixed in one stream; each is measured only
    against rules of its own scope.
    """
    for candidate in candidates:
        yield evaluate(rule_set, candidate)
