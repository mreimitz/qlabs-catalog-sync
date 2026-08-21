"""In-memory selection rule model: candidates, rules, overrides, matchers (C3, C5).

WP11 / T11.1. This module holds the *value* types the evaluator
(``qlabs_catalog_sync.selection.evaluator``) works over: plain, immutable, ORM-free
dataclasses, plus the converters that turn a persisted ``selection_rules`` /
``selection_overrides`` row (T10.1) into one. Nothing here does I/O, reads a clock, or
touches a database session.

The vocabulary is **not** redefined here. ``RuleScope``, ``SelectionDecision`` and
``MatcherKind`` are imported from ``qlabs_catalog_sync.configstore.types``, so a rule the
console stores and a rule the engine evaluates are spelled with the same enum members
rather than two vocabularies that can drift apart.

The candidate boundary (read this before writing a caller)
---------------------------------------------------------

:class:`SelectionCandidate` takes a **fully-qualified name as a required, explicit
argument**. It deliberately does *not* accept a ``ChangeRef``, an SDK entity, or any
other engine contract type and go looking for the name itself.

That is a direct response to a real defect RM-01's integration pilot found in the D1
filter this rule set supersedes (``SyncLoop._selects``, ``sync/loop.py``). Databricks keys
entities on a stable UUID -- ``schema_id`` / ``table_id``, because names are renameable --
and carries the dotted ``catalog.schema`` name in ``secondary_keys["full_name"]``. Code
that matched the pattern against ``native_key`` alone found no dot in any Databricks
change, concluded the selector did not apply, and **selected everything**: a pair scoped
to a single catalog silently synced every catalog in the metastore.

The lesson is that guessing where the qualified name lives is what caused the bug, so the
guessing does not happen in here. Deciding which native key is the dotted name is the
caller's job -- T11.2 (source tree) and T11.3 (sync loop) -- and a caller that genuinely
cannot produce one must say so by passing :data:`UNKNOWN`, which is a value the type
system forces it to write down.

Unknown facts are not empty facts
---------------------------------

Every fact a matcher can consult -- the qualified name, tags, owners -- is either a known
value or :data:`UNKNOWN`, and the two behave differently on purpose:

* a candidate with **no tags** (``tags=()``) makes a tag rule *not match*;
* a candidate with **unknown tags** (``tags=UNKNOWN``) makes a tag rule
  :attr:`MatchOutcome.UNAVAILABLE`, which the evaluator reports separately so a preview
  can say "this source cannot tell me about tags" instead of quietly answering "no".

This matters because Databricks reports UC tags only when a SQL warehouse is configured
(RM-01 decision D6); without one the capability manifest declares ``tags`` unavailable.
Collapsing that into "no tags" is exactly how a preview starts disagreeing with the run it
predicts, which C4 exists to prevent. ``tags`` and ``owners`` therefore *default* to
:data:`UNKNOWN`: a caller that never thought about them gets a visible "cannot say", never
a silent "nothing there".

Pattern grammar, and how it relates to ``config.py``
----------------------------------------------------

A ``glob`` pattern is a dot-separated sequence of fnmatch segments, matched segment by
segment, case-sensitively, against the candidate's qualified name split the same way. The
number of segments is fixed by the rule's scope (C5):

* :attr:`RuleScope.OBJECT` -- exactly two, ``catalog.schema``;
* :attr:`RuleScope.DATASET` -- exactly three, ``catalog.schema.table``.

For object scope this is **the same grammar and the same matching semantics** as decision
D1's ``catalog_schema_patterns`` -- ``qlabs_catalog_sync.config``'s
``_validate_catalog_schema_pattern`` and ``matches_catalog_schema`` -- deliberately, so a
rule an operator types into the console and a pattern they write in YAML mean the same
thing. ``tests/selection/test_rules.py`` pins that parity in both directions (every
pattern one accepts the other accepts, and both reject the same ones) so the two cannot
drift apart silently. Dataset scope is the same rule widened by one segment; a two-segment
pattern is rejected in dataset scope rather than being read as "everything under this
schema", which would be a second, invisible meaning for the same text. Write
``analytics.sales.*`` for that.

A ``tag`` pattern is ``key`` (matches that key whatever its value, including a key-only
tag) or ``key=value`` (matches that exact pair). An ``owner`` pattern is an fnmatch glob
over an owner's e-mail, compared case-folded through
``qlabs_catalog_sync.party.normalize_email`` -- the same normalization the engine's
best-effort owner correlation uses (RM-01 D3), so ``*@acme.com`` and ``Alice@Acme.com``
behave here exactly as they do there.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Final, Literal, Protocol, TypeAlias

from qlabs_catalog_sync.configstore.models import SelectionOverrideRow, SelectionRuleRow
from qlabs_catalog_sync.configstore.types import MatcherKind, RuleScope, SelectionDecision
from qlabs_catalog_sync.party import normalize_email
from qlabs_catalog_sync_sdk.models import Party, PartyRole, Tag

__all__ = [
    "DEFAULT_DECISION",
    "SEGMENTS_BY_SCOPE",
    "UNKNOWN",
    "CandidateFact",
    "CompiledRule",
    "MatchOutcome",
    "Matcher",
    "OwnerFacts",
    "QualifiedName",
    "SelectionCandidate",
    "SelectionOverride",
    "SelectionRule",
    "SelectionRuleSet",
    "TagFacts",
    "Unknown",
    "compile_matcher",
    "object_rules_from_catalog_schema_patterns",
    "validate_pattern",
]


# --------------------------------------------------------------------------------------
# The "the source cannot tell me" sentinel
# --------------------------------------------------------------------------------------


class Unknown(Enum):
    """A single-member enum used as a typed sentinel for an *unavailable* fact.

    A one-member enum rather than a bare object so that ``x is UNKNOWN`` narrows a
    ``T | Unknown`` union under strict type checking, and so the sentinel prints as
    something meaningful in a repr or an error message.
    """

    UNKNOWN = "unknown"

    def __str__(self) -> str:  # pragma: no cover - trivial, but keeps reprs readable
        return "unknown"


UNKNOWN: Final[Literal[Unknown.UNKNOWN]] = Unknown.UNKNOWN
"""The source cannot report this fact at all -- which is not the same as reporting none."""

QualifiedName: TypeAlias = str | Literal[Unknown.UNKNOWN]
TagFacts: TypeAlias = tuple[Tag, ...] | Literal[Unknown.UNKNOWN]
OwnerFacts: TypeAlias = tuple[Party, ...] | Literal[Unknown.UNKNOWN]


class CandidateFact(StrEnum):
    """A fact a matcher needs from a candidate, named so an unavailable one can be reported."""

    QUALIFIED_NAME = "qualified_name"
    TAGS = "tags"
    OWNERS = "owners"


class MatchOutcome(StrEnum):
    """The three-valued result of testing one rule's matcher against one candidate."""

    MATCH = "match"
    """The candidate matches; this rule becomes the decision so far (last match wins)."""

    NO_MATCH = "no_match"
    """The fact was available and does not match. The rule has no effect."""

    UNAVAILABLE = "unavailable"
    """The candidate cannot supply the fact, so the rule is *undetermined* -- neither a
    match nor a considered non-match. The evaluator reports it separately rather than
    letting "cannot say" masquerade as "no"."""


#: How many dot-separated segments a qualified name (and a glob pattern) has per scope (C5).
SEGMENTS_BY_SCOPE: Final[Mapping[RuleScope, int]] = {
    RuleScope.OBJECT: 2,
    RuleScope.DATASET: 3,
}

#: The shape a scope's qualified name has, for error messages.
_SHAPE_BY_SCOPE: Final[Mapping[RuleScope, str]] = {
    RuleScope.OBJECT: "catalog.schema",
    RuleScope.DATASET: "catalog.schema.table",
}

#: Which candidate fact each matcher kind consults.
_FACT_BY_MATCHER: Final[Mapping[MatcherKind, CandidateFact]] = {
    MatcherKind.GLOB: CandidateFact.QUALIFIED_NAME,
    MatcherKind.TAG: CandidateFact.TAGS,
    MatcherKind.OWNER: CandidateFact.OWNERS,
}

#: Allowed characters in one glob segment: identifier characters plus the fnmatch
#: metacharacters. Character-for-character the set ``config.py``'s ``_SEGMENT_PATTERN``
#: allows for a D1 ``catalog.schema`` pattern; see the module docstring on parity.
_SEGMENT_PATTERN: Final = re.compile(r"^[A-Za-z0-9_*?\[\]!]+$")

#: The decision when no rule matched and no override applies. See :data:`DEFAULT_DECISION`.
DEFAULT_DECISION: Final[SelectionDecision] = SelectionDecision.EXCLUDE
"""Nothing is selected unless a rule says so.

Three reasons, in order of weight:

1. **Blast radius.** The defect this rule set replaces was a silent default-*include*: a
   pair scoped to one catalog synced an entire metastore because the filter could not read
   the name and fell through to "yes". Failing closed makes the same class of mistake sync
   nothing, which an operator notices immediately and which no target has to be cleaned up
   after -- and v1 never deletes in Qlik (D4), so an over-broad run is not undoable.
2. **D1 equivalence.** ``SyncPairConfig.matches`` is ``any(pattern matches ...)``: an
   object matching none of a pair's patterns is not selected. One include rule per pattern
   reproduces that *only* if "no rule matched" means exclude. Default-include would break
   C3's stated guarantee that the selector is widened, not replaced.
3. **It makes the rule set a complete description.** C3's worked example starts by
   including ``analytics.*``; the operator states what is in, then carves out. With
   default-exclude the ordered list is the whole answer, and an empty list selects nothing
   rather than everything.
"""


# --------------------------------------------------------------------------------------
# Pattern validation
# --------------------------------------------------------------------------------------


def _validate_glob(pattern: str, scope: RuleScope) -> None:
    """Raise ``ValueError`` when ``pattern`` is not a valid glob for ``scope``."""
    expected = SEGMENTS_BY_SCOPE[scope]
    shape = _SHAPE_BY_SCOPE[scope]
    segments = pattern.split(".")
    if len(segments) != expected:
        raise ValueError(
            f"{pattern!r} must contain exactly {expected - 1} '.' separating "
            f"{expected} glob segments ({scope.value} scope selects {shape}, "
            f"e.g. {'sales.*' if expected == 2 else 'sales.orders.*'})"
        )
    for index, segment in enumerate(segments):
        if not segment:
            raise ValueError(
                f"{pattern!r} has an empty segment at position {index + 1} "
                f"(every segment of a {shape} pattern must be non-empty)"
            )
        if not _SEGMENT_PATTERN.match(segment):
            raise ValueError(
                f"{pattern!r} has an invalid segment {segment!r} -- allowed characters "
                "are letters, digits, underscore, and the glob wildcards * ? [ ] !"
            )


def _validate_tag(pattern: str) -> None:
    """Raise ``ValueError`` when ``pattern`` is not a valid ``key`` or ``key=value`` tag."""
    if not pattern.strip():
        raise ValueError("a tag pattern must not be blank (use 'key' or 'key=value')")
    if pattern.count("=") > 1:
        raise ValueError(
            f"{pattern!r} has more than one '=' -- a tag pattern is 'key' or 'key=value'"
        )
    key, _, value = pattern.partition("=")
    if not key:
        raise ValueError(f"{pattern!r} has an empty tag key")
    if "=" in pattern and not value:
        raise ValueError(
            f"{pattern!r} has an empty tag value -- write just {key!r} to match the key "
            "whatever its value"
        )


def _validate_owner(pattern: str) -> None:
    """Raise ``ValueError`` when ``pattern`` is not a usable owner-e-mail glob."""
    if not pattern.strip():
        raise ValueError(
            "an owner pattern must not be blank (an e-mail, or a glob like '*@acme.com')"
        )


def validate_pattern(pattern: str, *, matcher_kind: MatcherKind, scope: RuleScope) -> None:
    """Raise ``ValueError`` with a precise reason when ``pattern`` is invalid for its rule.

    Called by :class:`SelectionRule` on construction, so an unusable pattern fails at the
    boundary -- when the console saves it or when a row is read back -- rather than
    silently matching nothing during a run.
    """
    if matcher_kind is MatcherKind.GLOB:
        _validate_glob(pattern, scope)
    elif matcher_kind is MatcherKind.TAG:
        _validate_tag(pattern)
    else:
        _validate_owner(pattern)


# --------------------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelectionCandidate:
    """One object a selection decision is asked about. See the module docstring's boundary note.

    :param scope: which question this candidate answers -- is this ``catalog.schema`` a
        data product (:attr:`RuleScope.OBJECT`), or is this table/view a member of one
        (:attr:`RuleScope.DATASET`). Only rules of the same scope are ever consulted.
    :param object_id: the source's **stable** identifier -- a Databricks ``schema_id`` /
        ``table_id`` UUID, for instance. Used to pin per-object overrides across a rename;
        never matched by a glob, tag or owner rule.
    :param qualified_name: the dotted name the glob matchers read, with exactly the number
        of non-empty segments :data:`SEGMENTS_BY_SCOPE` gives for ``scope``, or
        :data:`UNKNOWN` when the caller genuinely has no qualified name for this object.
        Required, and never inferred -- see the module docstring.
    :param tags: the source's tags for this object, or :data:`UNKNOWN` (the default) when
        the source cannot report tags at all.
    :param owners: the source's parties for this object, or :data:`UNKNOWN` (the default).
        Only parties with :attr:`PartyRole.OWNER` and an e-mail are matchable; a party
        carrying only a service-principal application id is not an e-mail and is never
        guessed at, exactly as ``qlabs_catalog_sync.party`` treats it.
    :param display_name: optional label for the console; never matched against.

    Constructing a candidate whose ``qualified_name`` has the wrong number of segments
    raises ``ValueError``. That is deliberate: a caller holding an opaque key must pass
    :data:`UNKNOWN` and *mean* it, rather than handing over something dot-shaped and
    hoping the evaluator does the right thing with it.
    """

    scope: RuleScope
    object_id: str
    qualified_name: QualifiedName
    tags: TagFacts = UNKNOWN
    owners: OwnerFacts = UNKNOWN
    display_name: str | None = None

    _segments: tuple[str, ...] | None = field(init=False, repr=False, compare=False)
    _tag_keys: frozenset[str] | None = field(init=False, repr=False, compare=False)
    _tag_pairs: frozenset[tuple[str, str]] | None = field(init=False, repr=False, compare=False)
    _owner_emails: frozenset[str] | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.object_id.strip():
            raise ValueError("SelectionCandidate.object_id must be a non-empty stable identifier")
        object.__setattr__(self, "_segments", self._parse_name())
        object.__setattr__(self, "_tag_keys", self._index_tag_keys())
        object.__setattr__(self, "_tag_pairs", self._index_tag_pairs())
        object.__setattr__(self, "_owner_emails", self._index_owner_emails())

    def _parse_name(self) -> tuple[str, ...] | None:
        name = self.qualified_name
        if name is UNKNOWN:
            return None
        expected = SEGMENTS_BY_SCOPE[self.scope]
        segments = tuple(name.split("."))
        if len(segments) != expected or not all(segments):
            raise ValueError(
                f"qualified_name {name!r} is not a {_SHAPE_BY_SCOPE[self.scope]} "
                f"({expected} non-empty dot-separated segments) for {self.scope.value} scope; "
                "pass UNKNOWN when this object has no qualified name"
            )
        return segments

    def _index_tag_keys(self) -> frozenset[str] | None:
        tags = self.tags
        if tags is UNKNOWN:
            return None
        return frozenset(tag.key for tag in tags)

    def _index_tag_pairs(self) -> frozenset[tuple[str, str]] | None:
        tags = self.tags
        if tags is UNKNOWN:
            return None
        return frozenset((tag.key, tag.value) for tag in tags if tag.value is not None)

    def _index_owner_emails(self) -> frozenset[str] | None:
        owners = self.owners
        if owners is UNKNOWN:
            return None
        emails = (normalize_email(party.email) for party in owners if party.role is PartyRole.OWNER)
        return frozenset(email for email in emails if email is not None)

    @property
    def name_segments(self) -> tuple[str, ...] | None:
        """The qualified name split on ``.``, or ``None`` when it is :data:`UNKNOWN`."""
        return self._segments

    @property
    def tag_keys(self) -> frozenset[str] | None:
        """Every tag key this candidate carries, or ``None`` when tags are :data:`UNKNOWN`."""
        return self._tag_keys

    @property
    def tag_pairs(self) -> frozenset[tuple[str, str]] | None:
        """Every ``(key, value)`` tag pair, or ``None`` when tags are :data:`UNKNOWN`.

        Key-only tags (``Tag.value is None``) appear in :attr:`tag_keys` and not here, so a
        ``key=value`` pattern cannot match a tag that has no value.
        """
        return self._tag_pairs

    @property
    def owner_emails(self) -> frozenset[str] | None:
        """Normalized owner e-mails, or ``None`` when owners are :data:`UNKNOWN`.

        Normalized by ``qlabs_catalog_sync.party.normalize_email``: stripped and
        case-folded, nothing else. Parties that are not owners, and owners without an
        e-mail, contribute nothing -- an empty set here means "no matchable owner", which
        is a different answer from ``None``.
        """
        return self._owner_emails

    @property
    def has_qualified_name(self) -> bool:
        """Whether this candidate can answer a glob rule at all."""
        return self._segments is not None

    @property
    def override_keys(self) -> tuple[str, ...]:
        """The keys an override may be pinned by, most stable first.

        ``object_id`` before ``qualified_name``: the stable id survives a rename, so when a
        rule set somehow carries an override for both, the id's decision is the one that
        applies. The dotted name is accepted too because that is what an operator pins in
        the console ("keep ``analytics.prod_staging``") and what T10.1's
        ``selection_overrides.object_id`` column documents itself as holding.
        """
        name = self.qualified_name
        if name is UNKNOWN or name == self.object_id:
            return (self.object_id,)
        return (self.object_id, name)


# --------------------------------------------------------------------------------------
# Rules and overrides
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelectionRule:
    """One ordered include/exclude rule (C3) -- the in-memory form of a ``selection_rules`` row.

    :param rule_id: a stable identity that survives reordering, so a console can highlight
        "this rule decided" after the operator drags the list around. Persisted rules use
        ``str(SelectionRuleRow.id)``; synthesized ones use any stable string.
    :param ordinal: position within ``(pair, scope)``. Evaluation runs low ordinal to high
        and the **last** match wins.
    :param scope: the rules that apply to a candidate of the same scope, and no other.
    :param decision: what a match means -- include or exclude.
    :param matcher_kind: what ``pattern`` is matched against.
    :param pattern: validated on construction (see :func:`validate_pattern`).
    """

    rule_id: str
    ordinal: int
    scope: RuleScope
    decision: SelectionDecision
    matcher_kind: MatcherKind
    pattern: str

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise ValueError("SelectionRule.rule_id must be a non-empty stable identifier")
        if self.ordinal < 0:
            raise ValueError(
                f"selection rule {self.rule_id!r}: ordinal must be >= 0, got {self.ordinal}"
            )
        try:
            validate_pattern(self.pattern, matcher_kind=self.matcher_kind, scope=self.scope)
        except ValueError as exc:
            raise ValueError(f"selection rule {self.rule_id!r}: {exc}") from exc

    @property
    def required_fact(self) -> CandidateFact:
        """The candidate fact this rule's matcher needs to reach a verdict."""
        return _FACT_BY_MATCHER[self.matcher_kind]

    def describe(self) -> str:
        """A short human label, e.g. ``"rule #2 include glob 'analytics.*'"``."""
        return (
            f"rule #{self.ordinal} {self.decision.value} {self.matcher_kind.value} {self.pattern!r}"
        )

    @classmethod
    def from_row(cls, row: SelectionRuleRow) -> SelectionRule:
        """Build the in-memory rule for one persisted ``selection_rules`` row (T10.1)."""
        return cls(
            rule_id=str(row.id),
            ordinal=row.ordinal,
            scope=row.scope,
            decision=row.decision,
            matcher_kind=row.matcher_kind,
            pattern=row.pattern,
        )


@dataclass(frozen=True, slots=True)
class SelectionOverride:
    """One per-object pin that beats every rule (C3) -- the form of a ``selection_overrides`` row.

    :param scope: the scope whose candidates this pin applies to.
    :param object_id: the pinned object's stable identifier *or* its qualified name; see
        :attr:`SelectionCandidate.override_keys` for the order they are tried in.
    :param decision: include or exclude, unconditionally.
    :param reason: the operator's note, carried through to the explanation so a run report
        can say *why* an object was pinned.
    """

    scope: RuleScope
    object_id: str
    decision: SelectionDecision
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.object_id.strip():
            raise ValueError("SelectionOverride.object_id must be a non-empty identifier")

    def describe(self) -> str:
        """A short human label, e.g. ``"override include 'analytics.prod_staging'"``."""
        label = f"override {self.decision.value} {self.object_id!r}"
        return f"{label} ({self.reason})" if self.reason else label

    @classmethod
    def from_row(cls, row: SelectionOverrideRow) -> SelectionOverride:
        """Build the in-memory override for one persisted ``selection_overrides`` row."""
        return cls(
            scope=row.scope,
            object_id=row.object_id,
            decision=row.decision,
            reason=row.reason,
        )


# --------------------------------------------------------------------------------------
# Matchers, compiled once per rule
# --------------------------------------------------------------------------------------


class Matcher(Protocol):
    """Tests one compiled pattern against a candidate, three-valued (see :class:`MatchOutcome`)."""

    def evaluate(self, candidate: SelectionCandidate) -> MatchOutcome:
        """Whether ``candidate`` matches, does not match, or cannot answer."""
        ...


@dataclass(frozen=True, slots=True)
class _GlobMatcher:
    """Segment-wise fnmatch over the qualified name, compiled once per rule."""

    segments: tuple[re.Pattern[str], ...]

    def evaluate(self, candidate: SelectionCandidate) -> MatchOutcome:
        name_segments = candidate.name_segments
        if name_segments is None:
            return MatchOutcome.UNAVAILABLE
        if len(name_segments) != len(self.segments):
            # Unreachable while candidate and rule share a scope, which the rule set
            # guarantees; a defensive non-match rather than a silent wrong answer.
            return MatchOutcome.NO_MATCH
        matched = all(
            pattern.match(segment) is not None
            for pattern, segment in zip(self.segments, name_segments, strict=True)
        )
        return MatchOutcome.MATCH if matched else MatchOutcome.NO_MATCH


@dataclass(frozen=True, slots=True)
class _TagMatcher:
    """``key`` (any value) or ``key=value`` (that exact pair) against the candidate's tags."""

    key: str
    value: str | None

    def evaluate(self, candidate: SelectionCandidate) -> MatchOutcome:
        keys = candidate.tag_keys
        pairs = candidate.tag_pairs
        if keys is None or pairs is None:
            return MatchOutcome.UNAVAILABLE
        matched = self.key in keys if self.value is None else (self.key, self.value) in pairs
        return MatchOutcome.MATCH if matched else MatchOutcome.NO_MATCH


@dataclass(frozen=True, slots=True)
class _OwnerMatcher:
    """An fnmatch glob over owner e-mails, compared case-folded (see the module docstring)."""

    pattern: re.Pattern[str]

    def evaluate(self, candidate: SelectionCandidate) -> MatchOutcome:
        emails = candidate.owner_emails
        if emails is None:
            return MatchOutcome.UNAVAILABLE
        matched = any(self.pattern.match(email) is not None for email in emails)
        return MatchOutcome.MATCH if matched else MatchOutcome.NO_MATCH


def _compile_glob_segment(segment: str) -> re.Pattern[str]:
    """Compile one fnmatch segment.

    ``re.compile(fnmatch.translate(seg)).match`` *is* ``fnmatch.fnmatchcase(name, seg)`` --
    that is literally how the stdlib implements it -- so this is D1's case-sensitive
    matching with the compilation hoisted out of the per-candidate loop.
    """
    return re.compile(fnmatch.translate(segment))


def compile_matcher(*, matcher_kind: MatcherKind, pattern: str, scope: RuleScope) -> Matcher:
    """Compile one rule's pattern into a reusable :class:`Matcher`.

    Called once per rule when a :class:`SelectionRuleSet` is built, never per candidate:
    the console previews counts over a whole catalog, so pattern compilation must not be
    inside the N-candidate loop.
    """
    validate_pattern(pattern, matcher_kind=matcher_kind, scope=scope)
    if matcher_kind is MatcherKind.GLOB:
        return _GlobMatcher(tuple(_compile_glob_segment(seg) for seg in pattern.split(".")))
    if matcher_kind is MatcherKind.TAG:
        key, sep, value = pattern.partition("=")
        return _TagMatcher(key=key, value=value if sep else None)
    normalized = pattern.strip().casefold()
    return _OwnerMatcher(re.compile(fnmatch.translate(normalized)))


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """A :class:`SelectionRule` paired with its compiled matcher."""

    rule: SelectionRule
    matcher: Matcher

    def evaluate(self, candidate: SelectionCandidate) -> MatchOutcome:
        """Test this rule against ``candidate``."""
        return self.matcher.evaluate(candidate)

    @classmethod
    def of(cls, rule: SelectionRule) -> CompiledRule:
        """Compile ``rule``'s pattern."""
        return cls(
            rule=rule,
            matcher=compile_matcher(
                matcher_kind=rule.matcher_kind, pattern=rule.pattern, scope=rule.scope
            ),
        )


# --------------------------------------------------------------------------------------
# The rule set
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelectionRuleSet:
    """One pair's selection configuration: ordered rules and overrides, compiled and indexed.

    Build it with :meth:`build` (or :meth:`from_rows`) rather than by hand -- construction
    is where contradictions are rejected and where patterns are compiled, once, so that
    evaluating N candidates is O(N x rules) with no recompilation.
    """

    _rules_by_scope: Mapping[RuleScope, tuple[CompiledRule, ...]]
    _overrides_by_scope: Mapping[RuleScope, Mapping[str, SelectionOverride]]

    @classmethod
    def build(
        cls,
        rules: Iterable[SelectionRule] = (),
        overrides: Iterable[SelectionOverride] = (),
    ) -> SelectionRuleSet:
        """Validate, order and compile ``rules`` and index ``overrides``.

        Raises ``ValueError`` on any contradiction the T10.1 schema already refuses to
        store, rather than resolving it arbitrarily: two rules sharing an ordinal within a
        scope (``uq_selection_rules_pair_id_scope_ordinal``), two rules sharing a
        ``rule_id``, or two overrides pinning the same object in the same scope
        (``uq_selection_overrides_pair_id_scope_object_id``). An evaluator handed a
        contradiction cannot be deterministic, and "picked one" is exactly the kind of
        preview/run disagreement C4 forbids.
        """
        collected: dict[RuleScope, list[SelectionRule]] = {scope: [] for scope in RuleScope}
        seen_ids: dict[str, SelectionRule] = {}
        for rule in rules:
            previous = seen_ids.get(rule.rule_id)
            if previous is not None:
                raise ValueError(
                    f"duplicate rule_id {rule.rule_id!r}: {previous.describe()} and "
                    f"{rule.describe()} cannot both be in one rule set"
                )
            seen_ids[rule.rule_id] = rule
            collected[rule.scope].append(rule)

        rules_by_scope: dict[RuleScope, tuple[CompiledRule, ...]] = {}
        for scope, scope_rules in collected.items():
            ordinals: dict[int, SelectionRule] = {}
            for rule in scope_rules:
                clash = ordinals.get(rule.ordinal)
                if clash is not None:
                    raise ValueError(
                        f"two {scope.value}-scope rules share ordinal {rule.ordinal}: "
                        f"{clash.describe()} and {rule.describe()} -- ordering is the whole "
                        "meaning of a rule set, so this is rejected rather than resolved"
                    )
                ordinals[rule.ordinal] = rule
            ordered = sorted(scope_rules, key=lambda item: item.ordinal)
            rules_by_scope[scope] = tuple(CompiledRule.of(rule) for rule in ordered)

        overrides_by_scope: dict[RuleScope, dict[str, SelectionOverride]] = {
            scope: {} for scope in RuleScope
        }
        for override in overrides:
            scoped = overrides_by_scope[override.scope]
            existing = scoped.get(override.object_id)
            if existing is not None:
                raise ValueError(
                    f"duplicate {override.scope.value}-scope override for "
                    f"{override.object_id!r}: {existing.describe()} and {override.describe()}"
                )
            scoped[override.object_id] = override

        return cls(
            _rules_by_scope=rules_by_scope,
            _overrides_by_scope={
                scope: dict(mapping) for scope, mapping in overrides_by_scope.items()
            },
        )

    @classmethod
    def from_rows(
        cls,
        rule_rows: Iterable[SelectionRuleRow] = (),
        override_rows: Iterable[SelectionOverrideRow] = (),
    ) -> SelectionRuleSet:
        """Build from persisted rows (T10.1). The only place the ORM meets the evaluator."""
        return cls.build(
            rules=[SelectionRule.from_row(row) for row in rule_rows],
            overrides=[SelectionOverride.from_row(row) for row in override_rows],
        )

    def rules_for(self, scope: RuleScope) -> tuple[CompiledRule, ...]:
        """This set's rules for ``scope``, ordinal-ascending. Compiled once, at build time."""
        return self._rules_by_scope[scope]

    def override_for(self, candidate: SelectionCandidate) -> SelectionOverride | None:
        """The override pinning ``candidate``, or ``None``.

        Tries :attr:`SelectionCandidate.override_keys` in order -- stable id, then
        qualified name -- so a pin that survives a rename wins over one that does not.
        """
        scoped = self._overrides_by_scope[candidate.scope]
        for key in candidate.override_keys:
            override = scoped.get(key)
            if override is not None:
                return override
        return None

    def is_empty(self, scope: RuleScope | None = None) -> bool:
        """Whether this set has no rules and no overrides (optionally, for one scope)."""
        scopes = tuple(RuleScope) if scope is None else (scope,)
        return not any(
            self._rules_by_scope[item] or self._overrides_by_scope[item] for item in scopes
        )


def object_rules_from_catalog_schema_patterns(
    patterns: Sequence[str], *, rule_id_prefix: str = "pattern"
) -> tuple[SelectionRule, ...]:
    """C3's degenerate case: one object-scope include rule per D1 ``catalog.schema`` pattern.

    This is the bridge that makes "the selector is widened, not replaced" a fact rather
    than a claim: feeding a ``SyncPairConfig.catalog_schema_patterns`` list through here
    produces a rule set that decides exactly what ``SyncPairConfig.matches`` decides, which
    ``tests/selection/test_evaluator.py`` pins on a table of inputs. It is also what an
    environment-bootstrap import (T10.4) needs to turn existing YAML configuration into
    console-editable rules.

    Order is preserved as the ordinal, though for pure includes order is immaterial: with
    :data:`DEFAULT_DECISION` excluding, any match includes and none excludes, which is
    ``any()``.
    """
    return tuple(
        SelectionRule(
            rule_id=f"{rule_id_prefix}-{index}",
            ordinal=index,
            scope=RuleScope.OBJECT,
            decision=SelectionDecision.INCLUDE,
            matcher_kind=MatcherKind.GLOB,
            pattern=pattern,
        )
        for index, pattern in enumerate(patterns)
    )
