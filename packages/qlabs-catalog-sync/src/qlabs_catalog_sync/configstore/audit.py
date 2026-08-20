"""The one code path that writes configuration history (T10.3).

WP10 / T10.3. ``configstore/service.py`` is the only caller of this module, and it
calls these functions from inside its own :meth:`~qlabs_catalog_sync.configstore.
service.ConfigService._unit_of_work` -- exactly one open :class:`~sqlalchemy.orm.
Session`, not yet committed. Every function here does two things on that same session,
never in a separate transaction of its own:

1. bump :class:`~qlabs_catalog_sync.configstore.models.ConfigGenerationRow` (decision
   C1: "every write bumps a generation counter that the scheduler reconciles
   against"), and
2. append one or more :class:`~qlabs_catalog_sync.configstore.models.ConfigChangeRow`
   entries stamped with that same post-bump generation.

Nothing here calls ``session.commit()`` -- that boundary belongs entirely to the
service's unit of work, which is what makes "a failed write leaves no audit row and no
generation bump" true: if anything raises after these functions return, the owning
``async with`` block rolls back the whole session, audit rows and the generation bump
included, because they were never more than uncommitted statements on one shared
transaction.

Multi-field updates -- one row per changed field, not one row for the whole update.
``ConfigChangeRow.field`` (``configstore/models.py``) is documented as "the single
field that changed, for a field-level update; NULL for a whole-row create/delete" --
that shape is already fixed by the schema this task builds on, not a choice made here.
Recording every changed field as its own row is also what makes the log actually answer
its stated purpose ("when did this schema start syncing", per the decision's
Consequences section): a query for ``entity_id = ... AND field = 'cadence_seconds'``
finds exactly the history of that one setting. A single blob-diff row would still say
"something about this pair changed on this date" but never let a caller ask about one
field in isolation. :func:`record_update` still bumps the generation counter exactly
**once** per call, no matter how many fields changed -- every row it writes carries the
one post-bump generation, because they describe one write, even though that one write
touched several columns.

Never puts a credential in ``old_value``/``new_value``. This module has no way to know
whether a given value is secret-shaped -- that judgment is the caller's (``service.py``
never resolves or reads a real credential in the first place; ``EndpointRow.secret_ref``
is a reference, never a value, and is the only credential-adjacent thing this schema
lets through at all -- see ``configstore/models.py``). This module simply never touches
anything else.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from qlabs_catalog_sync.configstore.models import ConfigChangeRow, ConfigGenerationRow
from qlabs_catalog_sync.configstore.types import ChangeAction, ChangeEntityKind

__all__ = [
    "FieldChange",
    "current_generation",
    "record_create",
    "record_delete",
    "record_reorder",
    "record_update",
]


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One field's old and new value, for a single :func:`record_update` row.

    Values are whatever :class:`~qlabs_catalog_sync.configstore.models.ConfigChangeRow`
    itself accepts (``object | None``, stored as JSON) -- the caller is responsible for
    passing something JSON-serializable, and for never passing a credential (see the
    module docstring).
    """

    field: str
    old_value: object
    new_value: object


def _bump_generation(session: Session, now: datetime) -> int:
    """Increment the single-row generation counter and return its new value.

    Creates the row (starting at generation ``1``) the first time any write happens --
    the migration does not seed it (see ``configstore/models.py``: the row is modeled,
    not pre-populated). Every later call increments the existing row instead of
    fighting the ``CHECK (id = 1)`` singleton constraint by inserting a second one.
    """
    row = session.get(ConfigGenerationRow, 1)
    if row is None:
        row = ConfigGenerationRow(id=1, generation=1, updated_at=now)
        session.add(row)
    else:
        row.generation += 1
        row.updated_at = now
    session.flush()
    return row.generation


def current_generation(session: Session) -> int:
    """The current generation, or ``0`` when no configuration write has ever happened."""
    row = session.get(ConfigGenerationRow, 1)
    return 0 if row is None else row.generation


def record_create(
    session: Session,
    *,
    entity_kind: ChangeEntityKind,
    entity_id: str,
    actor: str,
    now: datetime,
    new_value: object,
) -> int:
    """Bump the generation and append one CREATE row carrying the whole new entity.

    ``field`` is left ``None`` -- a create has no single field to name, ``new_value``
    already carries the complete row (see the module docstring on
    ``ConfigChangeRow.field``'s two shapes).
    """
    generation = _bump_generation(session, now)
    session.add(
        ConfigChangeRow(
            entity_kind=entity_kind,
            entity_id=entity_id,
            action=ChangeAction.CREATE,
            field=None,
            old_value=None,
            new_value=new_value,
            actor=actor,
            changed_at=now,
            generation=generation,
        )
    )
    session.flush()
    return generation


def record_delete(
    session: Session,
    *,
    entity_kind: ChangeEntityKind,
    entity_id: str,
    actor: str,
    now: datetime,
    old_value: object,
) -> int:
    """Bump the generation and append one DELETE row carrying the whole removed entity."""
    generation = _bump_generation(session, now)
    session.add(
        ConfigChangeRow(
            entity_kind=entity_kind,
            entity_id=entity_id,
            action=ChangeAction.DELETE,
            field=None,
            old_value=old_value,
            new_value=None,
            actor=actor,
            changed_at=now,
            generation=generation,
        )
    )
    session.flush()
    return generation


def record_update(
    session: Session,
    *,
    entity_kind: ChangeEntityKind,
    entity_id: str,
    actor: str,
    now: datetime,
    changes: Sequence[FieldChange],
) -> int:
    """Bump the generation once and append one UPDATE row per entry in ``changes``.

    ``changes`` must be non-empty -- a caller that found nothing actually changed
    should never call this at all (no audit row, no generation bump for a write that
    would not have written anything; ``service.py`` computes the diff and only calls
    here when it is non-empty). Every row this call produces shares the *same*
    post-bump generation: they describe one write, not several.
    """
    if not changes:
        raise ValueError("record_update requires at least one FieldChange")
    generation = _bump_generation(session, now)
    for change in changes:
        session.add(
            ConfigChangeRow(
                entity_kind=entity_kind,
                entity_id=entity_id,
                action=ChangeAction.UPDATE,
                field=change.field,
                old_value=change.old_value,
                new_value=change.new_value,
                actor=actor,
                changed_at=now,
                generation=generation,
            )
        )
    session.flush()
    return generation


def record_reorder(
    session: Session,
    *,
    entity_kind: ChangeEntityKind,
    entity_id: str,
    actor: str,
    now: datetime,
    field: str,
    old_value: object,
    new_value: object,
) -> int:
    """Bump the generation and append one UPDATE row describing a reorder.

    A reorder is recorded as a single field-level update (``field`` names the ordered
    list that moved, e.g. ``"order:object"`` for a pair's object-scope rule set;
    ``old_value``/``new_value`` are the ordered lists of affected row ids as strings) --
    it is exactly :func:`record_update`'s one-field-changed shape, not a new one.
    """
    return record_update(
        session,
        entity_kind=entity_kind,
        entity_id=entity_id,
        actor=actor,
        now=now,
        changes=[FieldChange(field=field, old_value=old_value, new_value=new_value)],
    )
