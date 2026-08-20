"""``config_changes``: round trip (JSON old/new values), and append-only in practice.

No database trigger enforces immutability (see ``configstore/models.py`` for why); what
these tests check instead is the shape that makes the table append-only *in practice*: two
writes about the same entity produce two distinct rows with their own generated id, never
one row silently overwritten in place.
"""

from __future__ import annotations

from configstore_helpers import LATER, NOW
from sqlalchemy import select
from sqlalchemy.orm import Session

from qlabs_catalog_sync.configstore.models import ConfigChangeRow
from qlabs_catalog_sync.configstore.types import ChangeAction, ChangeEntityKind


def test_config_change_round_trips(session: Session) -> None:
    row = ConfigChangeRow(
        entity_kind=ChangeEntityKind.SYNC_PAIR,
        entity_id="databricks-to-qlik",
        action=ChangeAction.UPDATE,
        field="cadence_seconds",
        old_value=900,
        new_value=1800,
        actor="admin",
        changed_at=NOW,
        generation=3,
    )
    session.add(row)
    session.commit()
    change_id = row.id
    session.expunge_all()

    found = session.get(ConfigChangeRow, change_id)
    assert found is not None
    assert found.entity_kind == ChangeEntityKind.SYNC_PAIR
    assert isinstance(found.entity_kind, ChangeEntityKind)
    assert found.entity_id == "databricks-to-qlik"
    assert found.action == ChangeAction.UPDATE
    assert found.field == "cadence_seconds"
    assert found.old_value == 900
    assert found.new_value == 1800
    assert found.actor == "admin"
    assert found.changed_at == NOW
    assert found.changed_at.tzinfo is not None
    assert found.generation == 3


def test_create_and_delete_actions_carry_a_whole_row_and_no_field(session: Session) -> None:
    row = ConfigChangeRow(
        entity_kind=ChangeEntityKind.ENDPOINT,
        entity_id="qlik_acme",
        action=ChangeAction.CREATE,
        old_value=None,
        new_value={"connector": "qlik", "role": "target", "settings": {}},
        actor="admin",
        changed_at=NOW,
        generation=1,
    )
    session.add(row)
    session.commit()
    change_id = row.id
    session.expunge_all()

    found = session.get(ConfigChangeRow, change_id)
    assert found is not None
    assert found.field is None
    assert found.old_value is None
    assert found.new_value == {"connector": "qlik", "role": "target", "settings": {}}


def test_two_changes_to_the_same_entity_produce_two_distinct_rows(session: Session) -> None:
    """The dishonest case: a second write must not silently overwrite the first (C1)."""
    first = ConfigChangeRow(
        entity_kind=ChangeEntityKind.SYNC_PAIR,
        entity_id="databricks-to-qlik",
        action=ChangeAction.UPDATE,
        field="cadence_seconds",
        old_value=900,
        new_value=1800,
        actor="admin",
        changed_at=NOW,
        generation=1,
    )
    second = ConfigChangeRow(
        entity_kind=ChangeEntityKind.SYNC_PAIR,
        entity_id="databricks-to-qlik",
        action=ChangeAction.UPDATE,
        field="cadence_seconds",
        old_value=1800,
        new_value=3600,
        actor="admin",
        changed_at=LATER,
        generation=2,
    )
    session.add(first)
    session.add(second)
    session.commit()

    assert first.id != second.id
    rows = session.execute(
        select(ConfigChangeRow)
        .where(ConfigChangeRow.entity_id == "databricks-to-qlik")
        .order_by(ConfigChangeRow.changed_at)
    ).scalars().all()
    assert len(rows) == 2
    assert [row.generation for row in rows] == [1, 2]
    assert [row.new_value for row in rows] == [1800, 3600]
