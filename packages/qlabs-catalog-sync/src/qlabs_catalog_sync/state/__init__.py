"""State store.

WP2 / T2.2. SQLAlchemy 2.0 models for the identity map, per-(sync pair, endpoint,
entity type) watermarks, last-known field envelopes, and the D4 orphan log, with an
Alembic-managed migration (``packages/qlabs-catalog-sync/alembic/``) that creates the
schema from empty on SQLite in WAL mode -- the same schema is dialect-portable to
PostgreSQL later. :class:`~qlabs_catalog_sync.state.store.StateStore` is the single
entry point: point reads for identity/envelope/watermark/orphan lookups, plus
:meth:`~qlabs_catalog_sync.state.store.StateStore.unit_of_work`, the one-session,
one-transaction primitive T2.4's sync loop persists envelopes and advances the
watermark through, so a crash mid-cycle commits nothing.

This is the only task that owns the schema (``identity_map``, ``watermarks``,
``field_envelopes``, ``orphan_log``) -- no other task adds a migration.
"""

from qlabs_catalog_sync.state.db import create_state_engine
from qlabs_catalog_sync.state.models import (
    Base,
    FieldEnvelopeRow,
    IdentityMapRow,
    OrphanLogRow,
    WatermarkRow,
)
from qlabs_catalog_sync.state.store import (
    IdentityBinding,
    OrphanRecord,
    StateStore,
    UnitOfWork,
    WatermarkState,
)

__all__ = [
    "Base",
    "FieldEnvelopeRow",
    "IdentityBinding",
    "IdentityMapRow",
    "OrphanLogRow",
    "OrphanRecord",
    "StateStore",
    "UnitOfWork",
    "WatermarkRow",
    "WatermarkState",
    "create_state_engine",
]
