"""``configstore.audit``: the one code path that writes configuration history (T10.3).

Exercises :mod:`qlabs_catalog_sync.configstore.audit` directly, against the real
migrated database the ``session``/``engine`` fixtures (``tests/configstore/conftest.py``)
provide -- not a mock of ``Session``. ``configstore/service.py``'s own tests
(``test_service.py``) additionally prove this module is actually *used* correctly
(called inside one transaction, never committed on its own); this file proves the
module's own contract: one generation bump per call, the right row shape per action,
and append-only-in-practice, mirroring T10.1's own ``test_config_generation.py`` /
``test_config_changes.py`` but exercised through the real write path rather than by
constructing rows by hand.
"""

from __future__ import annotations

import pytest
from config_service_helpers import LATER, NOW
from sqlalchemy import select
from sqlalchemy.orm import Session

from qlabs_catalog_sync.configstore import audit
from qlabs_catalog_sync.configstore.audit import FieldChange
from qlabs_catalog_sync.configstore.models import ConfigChangeRow, ConfigGenerationRow
from qlabs_catalog_sync.configstore.types import ChangeAction, ChangeEntityKind


def test_current_generation_is_zero_before_any_write(session: Session) -> None:
    assert audit.current_generation(session) == 0


def test_record_create_bumps_generation_to_one_and_writes_a_field_none_row(
    session: Session,
) -> None:
    generation = audit.record_create(
        session,
        entity_kind=ChangeEntityKind.ENDPOINT,
        entity_id="qlik_acme",
        actor="admin",
        now=NOW,
        new_value={"connector": "qlik", "role": "target", "settings": {}},
    )
    session.commit()

    assert generation == 1
    assert audit.current_generation(session) == 1

    rows = session.execute(select(ConfigChangeRow)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.entity_kind == ChangeEntityKind.ENDPOINT
    assert row.entity_id == "qlik_acme"
    assert row.action == ChangeAction.CREATE
    assert row.field is None
    assert row.old_value is None
    assert row.new_value == {"connector": "qlik", "role": "target", "settings": {}}
    assert row.actor == "admin"
    assert row.changed_at == NOW
    assert row.generation == 1


def test_record_delete_writes_old_value_with_field_none(session: Session) -> None:
    generation = audit.record_delete(
        session,
        entity_kind=ChangeEntityKind.ENDPOINT,
        entity_id="qlik_acme",
        actor="admin",
        now=NOW,
        old_value={"connector": "qlik", "role": "target", "settings": {}},
    )
    session.commit()

    row = session.execute(select(ConfigChangeRow)).scalars().one()
    assert generation == 1
    assert row.action == ChangeAction.DELETE
    assert row.field is None
    assert row.old_value == {"connector": "qlik", "role": "target", "settings": {}}
    assert row.new_value is None


def test_record_update_writes_one_row_per_changed_field_sharing_one_generation(
    session: Session,
) -> None:
    """The "dishonest case" for multi-field updates: an implementation that collapsed
    every changed field into a single blob row, or one that bumped the generation once
    per field instead of once per call, would both make this fail."""
    generation = audit.record_update(
        session,
        entity_kind=ChangeEntityKind.SYNC_PAIR,
        entity_id="pair-1",
        actor="admin",
        now=NOW,
        changes=[
            FieldChange(field="cadence_seconds", old_value=900, new_value=1800),
            FieldChange(field="target_space", old_value="personal", new_value="shared"),
        ],
    )
    session.commit()

    assert generation == 1
    assert audit.current_generation(session) == 1  # one bump, not two

    rows = (
        session.execute(select(ConfigChangeRow).order_by(ConfigChangeRow.field))
        .scalars()
        .all()
    )
    assert len(rows) == 2  # two rows: one per changed field
    by_field = {row.field: row for row in rows}
    assert set(by_field) == {"cadence_seconds", "target_space"}

    cadence_row = by_field["cadence_seconds"]
    assert cadence_row.action == ChangeAction.UPDATE
    assert cadence_row.old_value == 900
    assert cadence_row.new_value == 1800
    assert cadence_row.generation == 1

    space_row = by_field["target_space"]
    assert space_row.old_value == "personal"
    assert space_row.new_value == "shared"
    assert space_row.generation == 1  # same generation as its sibling row


def test_record_update_with_no_changes_raises_and_writes_nothing(session: Session) -> None:
    with pytest.raises(ValueError, match="at least one FieldChange"):
        audit.record_update(
            session,
            entity_kind=ChangeEntityKind.SYNC_PAIR,
            entity_id="pair-1",
            actor="admin",
            now=NOW,
            changes=[],
        )
    session.commit()

    assert audit.current_generation(session) == 0
    assert session.execute(select(ConfigChangeRow)).scalars().all() == []


def test_record_reorder_is_a_single_field_level_update_row(session: Session) -> None:
    generation = audit.record_reorder(
        session,
        entity_kind=ChangeEntityKind.SELECTION_RULE,
        entity_id="pair-1:object",
        actor="admin",
        now=NOW,
        field="order:object",
        old_value=["rule-a", "rule-b"],
        new_value=["rule-b", "rule-a"],
    )
    session.commit()

    row = session.execute(select(ConfigChangeRow)).scalars().one()
    assert generation == 1
    assert row.action == ChangeAction.UPDATE
    assert row.field == "order:object"
    assert row.old_value == ["rule-a", "rule-b"]
    assert row.new_value == ["rule-b", "rule-a"]


def test_generation_increments_by_exactly_one_per_call_across_action_kinds(
    session: Session,
) -> None:
    g1 = audit.record_create(
        session,
        entity_kind=ChangeEntityKind.ENDPOINT,
        entity_id="e1",
        actor="admin",
        now=NOW,
        new_value={"connector": "qlik"},
    )
    g2 = audit.record_update(
        session,
        entity_kind=ChangeEntityKind.ENDPOINT,
        entity_id="e1",
        actor="admin",
        now=LATER,
        changes=[FieldChange(field="enabled", old_value=False, new_value=True)],
    )
    g3 = audit.record_delete(
        session,
        entity_kind=ChangeEntityKind.ENDPOINT,
        entity_id="e1",
        actor="admin",
        now=LATER,
        old_value={"connector": "qlik", "enabled": True},
    )
    session.commit()

    assert (g1, g2, g3) == (1, 2, 3)
    assert audit.current_generation(session) == 3


def test_generation_row_stays_a_single_row_across_many_writes(session: Session) -> None:
    """The generation counter is a genuine update-in-place, not a fresh insert per
    write (T10.1's ``ConfigGenerationRow`` singleton constraint would reject a second
    row at id 1 outright -- this pins that :func:`audit._bump_generation` never tries)."""
    for _ in range(5):
        audit.record_create(
            session,
            entity_kind=ChangeEntityKind.ENDPOINT,
            entity_id="e1",
            actor="admin",
            now=NOW,
            new_value={},
        )
    session.commit()

    rows = session.execute(select(ConfigGenerationRow)).scalars().all()
    assert len(rows) == 1
    assert rows[0].generation == 5


def test_two_updates_to_the_same_entity_produce_distinct_rows_not_an_overwrite(
    session: Session,
) -> None:
    """Append-only in practice (mirrors T10.1's own ``test_config_changes.py``), now
    exercised through the real write path rather than by constructing rows by hand."""
    audit.record_update(
        session,
        entity_kind=ChangeEntityKind.SYNC_PAIR,
        entity_id="pair-1",
        actor="admin",
        now=NOW,
        changes=[FieldChange(field="cadence_seconds", old_value=900, new_value=1800)],
    )
    audit.record_update(
        session,
        entity_kind=ChangeEntityKind.SYNC_PAIR,
        entity_id="pair-1",
        actor="admin",
        now=LATER,
        changes=[FieldChange(field="cadence_seconds", old_value=1800, new_value=3600)],
    )
    session.commit()

    rows = (
        session.execute(select(ConfigChangeRow).order_by(ConfigChangeRow.changed_at))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert [row.generation for row in rows] == [1, 2]
    assert [row.new_value for row in rows] == [1800, 3600]
