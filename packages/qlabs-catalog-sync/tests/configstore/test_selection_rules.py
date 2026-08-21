"""``selection_rules``: ordered, round-trips, and the ordering constraint actually bites.

The duplicate-``(pair, scope, ordinal)`` test here is also this task's "dishonest case"
for C3: an ordered rule set is only meaningful if two rules cannot claim the same
position in the same pair's same scope, so the schema -- not just the service layer built
on top of it later -- refuses to store that.
"""

from __future__ import annotations

import uuid

import pytest
from configstore_helpers import NOW, make_endpoint, make_sync_pair
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qlabs_catalog_sync.configstore.models import SelectionRuleRow, SyncPairRow
from qlabs_catalog_sync.configstore.types import (
    EndpointRole,
    MatcherKind,
    RuleScope,
    SelectionDecision,
)


@pytest.fixture
def pair(session: Session) -> SyncPairRow:
    session.add(make_endpoint("databricks_prod", connector="databricks", role=EndpointRole.SOURCE))
    session.add(make_endpoint("qlik_acme", connector="qlik", role=EndpointRole.TARGET))
    row = make_sync_pair()
    session.add(row)
    session.commit()
    return row


def _rule(
    pair_id: uuid.UUID,
    ordinal: int,
    *,
    scope: RuleScope = RuleScope.OBJECT,
    decision: SelectionDecision = SelectionDecision.INCLUDE,
    matcher_kind: MatcherKind = MatcherKind.GLOB,
    pattern: str = "sales.*",
) -> SelectionRuleRow:
    return SelectionRuleRow(
        pair_id=pair_id,
        ordinal=ordinal,
        scope=scope,
        decision=decision,
        matcher_kind=matcher_kind,
        pattern=pattern,
        created_at=NOW,
        updated_at=NOW,
    )


def test_selection_rule_round_trips(session: Session, pair: SyncPairRow) -> None:
    pair_id = pair.id
    row = _rule(
        pair_id,
        0,
        scope=RuleScope.DATASET,
        decision=SelectionDecision.EXCLUDE,
        matcher_kind=MatcherKind.TAG,
        pattern="pii=true",
    )
    session.add(row)
    session.commit()
    rule_id = row.id
    session.expunge_all()

    found = session.get(SelectionRuleRow, rule_id)
    assert found is not None
    assert found.pair_id == pair_id
    assert found.ordinal == 0
    assert found.scope == RuleScope.DATASET
    assert isinstance(found.scope, RuleScope)
    assert found.decision == SelectionDecision.EXCLUDE
    assert found.matcher_kind == MatcherKind.TAG
    assert found.pattern == "pii=true"


def test_rule_set_evaluates_in_ordinal_order(session: Session, pair: SyncPairRow) -> None:
    """A basic sanity check that ordinal really is stored and queryable in order."""
    session.add(_rule(pair.id, 2, pattern="third"))
    session.add(_rule(pair.id, 0, pattern="first"))
    session.add(_rule(pair.id, 1, pattern="second"))
    session.commit()

    patterns = (
        session.execute(
            select(SelectionRuleRow.pattern)
            .where(SelectionRuleRow.pair_id == pair.id)
            .order_by(SelectionRuleRow.ordinal)
        )
        .scalars()
        .all()
    )
    assert list(patterns) == ["first", "second", "third"]


def test_duplicate_ordinal_within_pair_and_scope_is_rejected(
    session: Session, pair: SyncPairRow
) -> None:
    session.add(_rule(pair.id, 0, scope=RuleScope.OBJECT))
    session.commit()

    session.add(_rule(pair.id, 0, scope=RuleScope.OBJECT, pattern="other.*"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_ordinal_is_allowed_in_a_different_scope(session: Session, pair: SyncPairRow) -> None:
    """Ordinal is unique per (pair, scope) -- object and dataset scope are independent orders."""
    session.add(_rule(pair.id, 0, scope=RuleScope.OBJECT))
    session.add(_rule(pair.id, 0, scope=RuleScope.DATASET))
    session.commit()  # must not raise

    count = (
        session.execute(select(SelectionRuleRow).where(SelectionRuleRow.pair_id == pair.id))
        .scalars()
        .all()
    )
    assert len(count) == 2


def test_negative_ordinal_is_rejected(session: Session, pair: SyncPairRow) -> None:
    session.add(_rule(pair.id, -1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_rule_referencing_nonexistent_pair_is_rejected(session: Session) -> None:
    session.add(_rule(uuid.uuid4(), 0))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_a_pair_cascades_to_its_rules(session: Session, pair: SyncPairRow) -> None:
    pair_id = pair.id
    session.add(_rule(pair_id, 0))
    session.add(_rule(pair_id, 1))
    session.commit()

    session.delete(pair)
    session.commit()

    remaining = (
        session.execute(select(SelectionRuleRow).where(SelectionRuleRow.pair_id == pair_id))
        .scalars()
        .all()
    )
    assert remaining == []
