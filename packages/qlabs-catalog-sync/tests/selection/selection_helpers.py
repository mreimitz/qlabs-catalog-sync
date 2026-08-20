"""Factories shared by the T11.1 selection tests.

Named ``selection_helpers`` rather than ``helpers`` -- this repo collects tests with
pytest's ``importlib`` import mode over one flat module namespace, and a plain ``helpers``
basename has already collided twice (see
``tests/configstore/configstore_helpers.py``, which this must not shadow either).

Everything here is a thin, explicit constructor: the point of these tests is to state a
rule set and a candidate in one readable line and then assert on the decision *and* the
deciding rule, not to hide either behind a fixture.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from qlabs_catalog_sync.configstore.models import SelectionOverrideRow, SelectionRuleRow
from qlabs_catalog_sync.configstore.types import MatcherKind, RuleScope, SelectionDecision
from qlabs_catalog_sync.selection import (
    UNKNOWN,
    OwnerFacts,
    QualifiedName,
    SelectionCandidate,
    SelectionOverride,
    SelectionResult,
    SelectionRule,
    TagFacts,
)
from qlabs_catalog_sync_sdk.models import Party, PartyRole, Tag

NOW: datetime = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


def rule(
    ordinal: int,
    decision: SelectionDecision,
    pattern: str,
    *,
    scope: RuleScope = RuleScope.OBJECT,
    matcher_kind: MatcherKind = MatcherKind.GLOB,
    rule_id: str | None = None,
) -> SelectionRule:
    """One rule, ``rule_id`` defaulting to ``"r<ordinal>"`` so assertions read naturally."""
    return SelectionRule(
        rule_id=rule_id if rule_id is not None else f"r{ordinal}",
        ordinal=ordinal,
        scope=scope,
        decision=decision,
        matcher_kind=matcher_kind,
        pattern=pattern,
    )


def include(
    ordinal: int,
    pattern: str,
    *,
    scope: RuleScope = RuleScope.OBJECT,
    matcher_kind: MatcherKind = MatcherKind.GLOB,
    rule_id: str | None = None,
) -> SelectionRule:
    """An include rule."""
    return rule(
        ordinal,
        SelectionDecision.INCLUDE,
        pattern,
        scope=scope,
        matcher_kind=matcher_kind,
        rule_id=rule_id,
    )


def exclude(
    ordinal: int,
    pattern: str,
    *,
    scope: RuleScope = RuleScope.OBJECT,
    matcher_kind: MatcherKind = MatcherKind.GLOB,
    rule_id: str | None = None,
) -> SelectionRule:
    """An exclude rule."""
    return rule(
        ordinal,
        SelectionDecision.EXCLUDE,
        pattern,
        scope=scope,
        matcher_kind=matcher_kind,
        rule_id=rule_id,
    )


def override(
    object_id: str,
    decision: SelectionDecision,
    *,
    scope: RuleScope = RuleScope.OBJECT,
    reason: str | None = None,
) -> SelectionOverride:
    """One per-object pin."""
    return SelectionOverride(scope=scope, object_id=object_id, decision=decision, reason=reason)


def schema_candidate(
    qualified_name: QualifiedName,
    *,
    object_id: str | None = None,
    tags: TagFacts = UNKNOWN,
    owners: OwnerFacts = UNKNOWN,
) -> SelectionCandidate:
    """An object-scope candidate.

    ``object_id`` defaults to a **UUID**, not to the name: that is what Databricks actually
    keys a schema on (``schema_id``), and building the test data the other way round would
    quietly stop these tests from covering the defect they exist for.
    """
    return SelectionCandidate(
        scope=RuleScope.OBJECT,
        object_id=object_id if object_id is not None else str(uuid.uuid4()),
        qualified_name=qualified_name,
        tags=tags,
        owners=owners,
    )


def dataset_candidate(
    qualified_name: QualifiedName,
    *,
    object_id: str | None = None,
    tags: TagFacts = UNKNOWN,
    owners: OwnerFacts = UNKNOWN,
) -> SelectionCandidate:
    """A dataset-scope candidate, keyed on a UUID like a Databricks ``table_id``."""
    return SelectionCandidate(
        scope=RuleScope.DATASET,
        object_id=object_id if object_id is not None else str(uuid.uuid4()),
        qualified_name=qualified_name,
        tags=tags,
        owners=owners,
    )


def tags(*specs: str) -> tuple[Tag, ...]:
    """Tags from ``"key"`` or ``"key=value"`` strings."""
    built: list[Tag] = []
    for spec in specs:
        key, sep, value = spec.partition("=")
        built.append(Tag(key=key, value=value if sep else None))
    return tuple(built)


def owners(*emails: str, role: PartyRole = PartyRole.OWNER) -> tuple[Party, ...]:
    """Parties carrying ``emails``, owners by default."""
    return tuple(Party(email=email, role=role) for email in emails)


def rule_row(
    ordinal: int,
    decision: SelectionDecision,
    pattern: str,
    *,
    scope: RuleScope = RuleScope.OBJECT,
    matcher_kind: MatcherKind = MatcherKind.GLOB,
    row_id: uuid.UUID | None = None,
    pair_id: uuid.UUID | None = None,
) -> SelectionRuleRow:
    """A transient (never-persisted) ``selection_rules`` row, for the converter tests."""
    return SelectionRuleRow(
        id=row_id if row_id is not None else uuid.uuid4(),
        pair_id=pair_id if pair_id is not None else uuid.uuid4(),
        ordinal=ordinal,
        scope=scope,
        decision=decision,
        matcher_kind=matcher_kind,
        pattern=pattern,
        created_at=NOW,
        updated_at=NOW,
    )


def override_row(
    object_id: str,
    decision: SelectionDecision,
    *,
    scope: RuleScope = RuleScope.OBJECT,
    reason: str | None = None,
    pair_id: uuid.UUID | None = None,
) -> SelectionOverrideRow:
    """A transient (never-persisted) ``selection_overrides`` row, for the converter tests."""
    return SelectionOverrideRow(
        id=uuid.uuid4(),
        pair_id=pair_id if pair_id is not None else uuid.uuid4(),
        scope=scope,
        object_id=object_id,
        decision=decision,
        reason=reason,
        created_at=NOW,
        updated_at=NOW,
    )


def decisions(results: Sequence[SelectionResult]) -> list[tuple[str, bool, str | None]]:
    """``(qualified name, included, deciding rule id)`` for a batch of results.

    Every assertion in these tests goes through the deciding rule as well as the boolean:
    a selection engine that returns the right answer for the wrong reason is a selection
    engine whose preview will eventually disagree with its run.
    """
    return [
        (str(result.candidate.qualified_name), result.included, result.rule_id)
        for result in results
    ]
