"""Portable timestamp type: always a timezone-aware, UTC-normalized ``datetime``.

SQLite has no native timezone-aware timestamp: SQLAlchemy's SQLite ``DATETIME``
faithfully writes an aware value's ISO string on the way in, but parses it back
*naive* on the way out, silently dropping ``tzinfo``. Left alone, this would make a
round-trip through the state store return a different (naive) value from the one
written, which breaks every equality/ordering comparison the sync loop or a test does
against ``last_modified_at``/``last_synced_at``/watermark timestamps.

:class:`UTCDateTime` normalizes on both sides of that boundary instead of leaving it
to chance: bound values are converted to UTC before storage, and any value read back
is (re)attached to UTC if it comes back naive. On PostgreSQL, where ``TIMESTAMPTZ``
already round-trips awareness natively, the same normalization is a no-op beyond the
UTC conversion -- the column stays the ordinary, portable ``DateTime(timezone=True)``
underneath; only the Python-level marshalling is dialect-aware.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

__all__ = ["UTCDateTime"]


class UTCDateTime(TypeDecorator[datetime]):
    """A timezone-aware ``datetime`` column that always round-trips in UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "UTCDateTime requires a timezone-aware datetime; got a naive one "
                f"({value!r}). Attach a tzinfo (e.g. datetime.UTC) before writing it."
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
