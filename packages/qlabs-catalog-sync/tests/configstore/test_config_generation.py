"""``config_generation``: the single-row counter, and why it can never become two rows.

The single-row constraint is modeled in the schema (a fixed-``id`` primary key plus a
``CHECK (id = 1)``), not left to application discipline -- these tests prove both halves
of that actually hold: a second row at the *same* id (1) fails the primary key, and a
second row at any *other* id fails the check constraint, so there is no id an application
bug could use to sneak in a second row.
"""

from __future__ import annotations

import pytest
from configstore_helpers import LATER, NOW
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qlabs_catalog_sync.configstore.models import ConfigGenerationRow


def test_config_generation_round_trips(session: Session) -> None:
    session.add(ConfigGenerationRow(id=1, generation=0, updated_at=NOW))
    session.commit()
    session.expunge_all()

    found = session.get(ConfigGenerationRow, 1)
    assert found is not None
    assert found.generation == 0
    assert found.updated_at == NOW


def test_default_id_is_one(session: Session) -> None:
    row = ConfigGenerationRow(generation=0, updated_at=NOW)
    session.add(row)
    session.commit()

    assert row.id == 1


def test_bumping_the_generation_is_an_update_not_a_new_row(session: Session) -> None:
    session.add(ConfigGenerationRow(id=1, generation=0, updated_at=NOW))
    session.commit()

    row = session.get(ConfigGenerationRow, 1)
    assert row is not None
    row.generation = 1
    row.updated_at = LATER
    session.commit()
    session.expunge_all()

    rows = session.execute(select(ConfigGenerationRow)).scalars().all()
    assert len(rows) == 1
    assert rows[0].generation == 1
    assert rows[0].updated_at == LATER


def test_a_second_row_at_id_one_is_rejected(session: Session) -> None:
    session.add(ConfigGenerationRow(id=1, generation=0, updated_at=NOW))
    session.commit()

    session.add(ConfigGenerationRow(id=1, generation=0, updated_at=NOW))
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_second_row_at_a_different_id_is_also_rejected(session: Session) -> None:
    """The dishonest case: an id other than 1 must not become a back door to a second row."""
    session.add(ConfigGenerationRow(id=1, generation=0, updated_at=NOW))
    session.commit()

    session.add(ConfigGenerationRow(id=2, generation=0, updated_at=NOW))
    with pytest.raises(IntegrityError):
        session.commit()
