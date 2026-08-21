"""``selection_overrides``: round trip, one decision per (pair, scope, object), FK, cascade."""

from __future__ import annotations

import uuid

import pytest
from configstore_helpers import NOW, make_endpoint, make_sync_pair
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qlabs_catalog_sync.configstore.models import SelectionOverrideRow, SyncPairRow
from qlabs_catalog_sync.configstore.types import EndpointRole, RuleScope, SelectionDecision


@pytest.fixture
def pair(session: Session) -> SyncPairRow:
    session.add(make_endpoint("databricks_prod", connector="databricks", role=EndpointRole.SOURCE))
    session.add(make_endpoint("qlik_acme", connector="qlik", role=EndpointRole.TARGET))
    row = make_sync_pair()
    session.add(row)
    session.commit()
    return row


def _override(
    pair_id: uuid.UUID,
    object_id: str,
    *,
    scope: RuleScope = RuleScope.OBJECT,
    decision: SelectionDecision = SelectionDecision.INCLUDE,
    reason: str | None = "operator pin",
) -> SelectionOverrideRow:
    return SelectionOverrideRow(
        pair_id=pair_id,
        scope=scope,
        object_id=object_id,
        decision=decision,
        reason=reason,
        created_at=NOW,
        updated_at=NOW,
    )


def test_selection_override_round_trips(session: Session, pair: SyncPairRow) -> None:
    pair_id = pair.id
    row = _override(
        pair_id,
        "analytics.prod_staging",
        scope=RuleScope.OBJECT,
        decision=SelectionDecision.INCLUDE,
        reason="keep this one schema even though the exclude rule below would drop it",
    )
    session.add(row)
    session.commit()
    override_id = row.id
    session.expunge_all()

    found = session.get(SelectionOverrideRow, override_id)
    assert found is not None
    assert found.pair_id == pair_id
    assert found.scope == RuleScope.OBJECT
    assert found.object_id == "analytics.prod_staging"
    assert found.decision == SelectionDecision.INCLUDE
    assert isinstance(found.decision, SelectionDecision)
    assert found.reason == "keep this one schema even though the exclude rule below would drop it"


def test_override_reason_is_optional(session: Session, pair: SyncPairRow) -> None:
    row = SelectionOverrideRow(
        pair_id=pair.id,
        scope=RuleScope.DATASET,
        object_id="analytics.sales.orders",
        decision=SelectionDecision.EXCLUDE,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(row)
    session.commit()
    override_id = row.id
    session.expunge_all()

    found = session.get(SelectionOverrideRow, override_id)
    assert found is not None
    assert found.reason is None


def test_duplicate_object_override_within_pair_and_scope_is_rejected(
    session: Session, pair: SyncPairRow
) -> None:
    session.add(_override(pair.id, "analytics.prod_staging"))
    session.commit()

    session.add(_override(pair.id, "analytics.prod_staging", decision=SelectionDecision.EXCLUDE))
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_object_id_is_allowed_in_a_different_scope(
    session: Session, pair: SyncPairRow
) -> None:
    """Object scope and dataset scope share the string shape but are independent pins."""
    session.add(_override(pair.id, "analytics.sales", scope=RuleScope.OBJECT))
    session.add(_override(pair.id, "analytics.sales", scope=RuleScope.DATASET))
    session.commit()  # must not raise

    rows = (
        session.execute(select(SelectionOverrideRow).where(SelectionOverrideRow.pair_id == pair.id))
        .scalars()
        .all()
    )
    assert len(rows) == 2


def test_override_referencing_nonexistent_pair_is_rejected(session: Session) -> None:
    session.add(_override(uuid.uuid4(), "analytics.sales"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_a_pair_cascades_to_its_overrides(session: Session, pair: SyncPairRow) -> None:
    pair_id = pair.id
    session.add(_override(pair_id, "analytics.sales"))
    session.commit()

    session.delete(pair)
    session.commit()

    remaining = (
        session.execute(select(SelectionOverrideRow).where(SelectionOverrideRow.pair_id == pair_id))
        .scalars()
        .all()
    )
    assert remaining == []
