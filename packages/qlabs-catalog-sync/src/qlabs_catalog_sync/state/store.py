"""The state store's public surface: read helpers plus the transactional unit of work.

WP2 / T2.2. Two ways to touch the state store:

* Point reads (:meth:`StateStore.find_binding`, :meth:`StateStore.get_binding`,
  :meth:`StateStore.fetch_envelopes`, :meth:`StateStore.get_watermark`,
  :meth:`StateStore.list_orphans`) each open one short-lived session and are safe to
  call at any point in a sync cycle -- identity resolution and diffing both read
  before anything is written.
* :meth:`StateStore.unit_of_work` -- an async context manager over exactly one
  :class:`~sqlalchemy.orm.Session` and exactly one transaction. This is what T2.4's
  sync loop uses to persist envelopes and advance the watermark together: every
  :class:`UnitOfWork` method operates on the same session, so nothing commits until
  the ``async with`` block exits cleanly, and *nothing* commits if it raises --
  restart safety (RS-07 section 6) falls directly out of "one session, one
  transaction, no partial commit," not out of any retry logic.

Every public method is ``async def`` (the engine is an async process); the blocking
SQLAlchemy calls underneath run in a worker thread via :func:`asyncio.to_thread` (see
:mod:`qlabs_catalog_sync.state.db` for why there is no native async engine here yet).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from qlabs_catalog_sync.state.db import create_state_engine
from qlabs_catalog_sync.state.models import (
    FieldEnvelopeRow,
    IdentityMapRow,
    OrphanLogRow,
    WatermarkRow,
)
from qlabs_catalog_sync_sdk.models import EntityType, FieldEnvelope, IdentityRef

__all__ = [
    "IdentityBinding",
    "OrphanRecord",
    "StateStore",
    "UnitOfWork",
    "WatermarkState",
]


# --------------------------------------------------------------------------------------
# Domain-level read results (plain dataclasses over the SDK's own types, not a
# parallel vocabulary -- an IdentityBinding *is* an IdentityRef plus the engine-side
# bookkeeping the SDK type itself has no reason to carry).
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IdentityBinding:
    """A stored identity_map row: the SDK's :class:`IdentityRef` plus engine bookkeeping."""

    neutral_id: uuid.UUID
    identity: IdentityRef
    confirmed: bool
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WatermarkState:
    """A stored watermark row for one (sync pair, endpoint, entity type)."""

    sync_pair: str
    endpoint: str
    entity_type: EntityType
    watermark_token: str | None
    last_run_at: datetime | None
    last_status: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OrphanRecord:
    """A stored orphan_log row: a source object observed missing at ``endpoint``."""

    neutral_id: uuid.UUID
    endpoint: str
    entity_type: EntityType
    native_key: str | None
    first_missing_at: datetime
    last_missing_at: datetime
    last_seen_at: datetime | None
    resolved_at: datetime | None


def _binding_from_row(row: IdentityMapRow) -> IdentityBinding:
    return IdentityBinding(
        neutral_id=row.neutral_id,
        identity=IdentityRef(
            endpoint=row.endpoint,
            entity_type=EntityType(row.entity_type),
            native_key=row.native_key,
            tenant_id=row.tenant_id,
            secondary_keys=dict(row.secondary_keys),
        ),
        confirmed=row.confirmed,
        created_at=row.created_at,
        updated_at=row.updated_at,
        confirmed_at=row.confirmed_at,
    )


def _watermark_from_row(row: WatermarkRow) -> WatermarkState:
    return WatermarkState(
        sync_pair=row.sync_pair,
        endpoint=row.endpoint,
        entity_type=EntityType(row.entity_type),
        watermark_token=row.watermark_token,
        last_run_at=row.last_run_at,
        last_status=row.last_status,
        updated_at=row.updated_at,
    )


def _orphan_from_row(row: OrphanLogRow) -> OrphanRecord:
    return OrphanRecord(
        neutral_id=row.neutral_id,
        endpoint=row.endpoint,
        entity_type=EntityType(row.entity_type),
        native_key=row.native_key,
        first_missing_at=row.first_missing_at,
        last_missing_at=row.last_missing_at,
        last_seen_at=row.last_seen_at,
        resolved_at=row.resolved_at,
    )


def _envelope_from_row(row: FieldEnvelopeRow) -> FieldEnvelope[JsonValue]:
    return FieldEnvelope[JsonValue](
        value=row.value_json,
        source_endpoint=row.source_endpoint or row.endpoint,
        source_revision=row.source_revision,
        last_modified_at=row.last_modified_at,
        last_synced_at=row.last_synced_at,
        checksum=row.checksum,
    )


# --------------------------------------------------------------------------------------
# The transactional unit of work
# --------------------------------------------------------------------------------------


class UnitOfWork:
    """One session, one transaction. Every method here writes through the same session.

    Never construct directly -- obtained from :meth:`StateStore.unit_of_work`, which
    owns the commit/rollback boundary. Every method is ``async def`` for interface
    consistency with the rest of the store, but all of them operate on the same
    in-flight session and none commits on its own; only the owning context manager
    does, and only if the ``with`` block exits without raising.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- identity ------------------------------------------------------------------

    async def bind_identity(
        self,
        neutral_id: uuid.UUID,
        identity: IdentityRef,
        *,
        confirmed: bool = False,
        now: datetime,
    ) -> None:
        """Create or update the (neutral_id, endpoint, entity_type) binding.

        Upserts by primary key: re-binding the same (neutral_id, endpoint,
        entity_type) updates the row (native key, secondary keys, confirmation)
        rather than erroring. Binding the same native key under a *different*
        neutral_id at the same endpoint+tenant raises ``IntegrityError`` on flush --
        the ``uq_identity_map_endpoint_entity_type_tenant_id_native_key`` constraint
        this schema owns and no other task may alter.
        """

        def _write() -> None:
            existing = self._session.get(
                IdentityMapRow,
                (neutral_id, identity.endpoint, identity.entity_type.value),
            )
            if existing is None:
                self._session.add(
                    IdentityMapRow(
                        neutral_id=neutral_id,
                        endpoint=identity.endpoint,
                        entity_type=identity.entity_type.value,
                        tenant_id=identity.tenant_id,
                        native_key=identity.native_key,
                        secondary_keys=dict(identity.secondary_keys),
                        confirmed=confirmed,
                        confirmed_at=now if confirmed else None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                existing.tenant_id = identity.tenant_id
                existing.native_key = identity.native_key
                existing.secondary_keys = dict(identity.secondary_keys)
                existing.confirmed = confirmed
                if confirmed and existing.confirmed_at is None:
                    existing.confirmed_at = now
                existing.updated_at = now
            self._session.flush()

        await asyncio.to_thread(_write)

    async def confirm_identity(
        self, neutral_id: uuid.UUID, endpoint: str, entity_type: EntityType, *, now: datetime
    ) -> None:
        """Mark an existing binding confirmed (T7.1's explicit confirmation step)."""

        def _write() -> None:
            row = self._session.get(IdentityMapRow, (neutral_id, endpoint, entity_type.value))
            if row is None:
                raise LookupError(
                    f"no identity_map binding for neutral_id={neutral_id!s} "
                    f"endpoint={endpoint!r} entity_type={entity_type!r}"
                )
            row.confirmed = True
            row.confirmed_at = now
            row.updated_at = now
            self._session.flush()

        await asyncio.to_thread(_write)

    # -- field envelopes -------------------------------------------------------------

    async def upsert_field_envelope(
        self,
        neutral_id: uuid.UUID,
        endpoint: str,
        entity_type: EntityType,
        field: str,
        envelope: FieldEnvelope[JsonValue],
        *,
        now: datetime,
    ) -> None:
        """Write the last-known envelope for (neutral_id, endpoint, entity_type, field)."""

        def _write() -> None:
            existing = self._session.get(
                FieldEnvelopeRow, (neutral_id, endpoint, entity_type.value, field)
            )
            if existing is None:
                self._session.add(
                    FieldEnvelopeRow(
                        neutral_id=neutral_id,
                        endpoint=endpoint,
                        entity_type=entity_type.value,
                        field=field,
                        value_json=envelope.value,
                        source_endpoint=envelope.source_endpoint,
                        source_revision=envelope.source_revision,
                        last_modified_at=envelope.last_modified_at,
                        last_synced_at=envelope.last_synced_at,
                        checksum=envelope.checksum,
                        updated_at=now,
                    )
                )
            else:
                existing.value_json = envelope.value
                existing.source_endpoint = envelope.source_endpoint
                existing.source_revision = envelope.source_revision
                existing.last_modified_at = envelope.last_modified_at
                existing.last_synced_at = envelope.last_synced_at
                existing.checksum = envelope.checksum
                existing.updated_at = now
            self._session.flush()

        await asyncio.to_thread(_write)

    # -- watermarks -------------------------------------------------------------------

    async def advance_watermark(
        self,
        sync_pair: str,
        endpoint: str,
        entity_type: EntityType,
        watermark_token: str | None,
        *,
        status: str = "ok",
        run_at: datetime,
    ) -> None:
        """Persist the next opaque resume token for (sync_pair, endpoint, entity_type)."""

        def _write() -> None:
            existing = self._session.get(WatermarkRow, (sync_pair, endpoint, entity_type.value))
            if existing is None:
                self._session.add(
                    WatermarkRow(
                        sync_pair=sync_pair,
                        endpoint=endpoint,
                        entity_type=entity_type.value,
                        watermark_token=watermark_token,
                        last_run_at=run_at,
                        last_status=status,
                        updated_at=run_at,
                    )
                )
            else:
                existing.watermark_token = watermark_token
                existing.last_run_at = run_at
                existing.last_status = status
                existing.updated_at = run_at
            self._session.flush()

        await asyncio.to_thread(_write)

    # -- orphan log ---------------------------------------------------------------------

    async def record_orphan(
        self,
        neutral_id: uuid.UUID,
        endpoint: str,
        entity_type: EntityType,
        *,
        native_key: str | None,
        last_seen_at: datetime | None,
        observed_at: datetime,
    ) -> None:
        """Record ``endpoint`` no longer reporting this entity as of ``observed_at``.

        First call for a (neutral_id, endpoint, entity_type) sets both
        ``first_missing_at`` and ``last_missing_at``; later calls only advance
        ``last_missing_at`` and clear ``resolved_at`` (still missing). D4: this never
        deletes anything in Qlik -- it only records what vanished, where, and when.
        """

        def _write() -> None:
            existing = self._session.get(OrphanLogRow, (neutral_id, endpoint, entity_type.value))
            if existing is None:
                self._session.add(
                    OrphanLogRow(
                        neutral_id=neutral_id,
                        endpoint=endpoint,
                        entity_type=entity_type.value,
                        native_key=native_key,
                        first_missing_at=observed_at,
                        last_missing_at=observed_at,
                        last_seen_at=last_seen_at,
                        resolved_at=None,
                    )
                )
            else:
                existing.last_missing_at = observed_at
                if native_key is not None:
                    existing.native_key = native_key
                if last_seen_at is not None:
                    existing.last_seen_at = last_seen_at
                existing.resolved_at = None
            self._session.flush()

        await asyncio.to_thread(_write)

    async def resolve_orphan(
        self, neutral_id: uuid.UUID, endpoint: str, entity_type: EntityType, *, now: datetime
    ) -> None:
        """Mark a previously-orphaned entity as seen again (it reappeared at the source)."""

        def _write() -> None:
            row = self._session.get(OrphanLogRow, (neutral_id, endpoint, entity_type.value))
            if row is None:
                raise LookupError(
                    f"no orphan_log row for neutral_id={neutral_id!s} "
                    f"endpoint={endpoint!r} entity_type={entity_type!r}"
                )
            row.resolved_at = now
            row.last_seen_at = now
            self._session.flush()

        await asyncio.to_thread(_write)

    # -- reads within the same transaction (optional convenience) --------------------

    async def fetch_envelopes(
        self, neutral_id: uuid.UUID, endpoint: str
    ) -> dict[str, FieldEnvelope[JsonValue]]:
        """Every last-known envelope for one entity at one endpoint, keyed by field name."""

        def _read() -> dict[str, FieldEnvelope[JsonValue]]:
            stmt = select(FieldEnvelopeRow).where(
                FieldEnvelopeRow.neutral_id == neutral_id,
                FieldEnvelopeRow.endpoint == endpoint,
            )
            rows = self._session.scalars(stmt).all()
            return {row.field: _envelope_from_row(row) for row in rows}

        return await asyncio.to_thread(_read)


# --------------------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------------------


class StateStore:
    """Owns the engine and session factory; the single entry point onto the state store."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory: sessionmaker[Session] = sessionmaker(
            bind=engine, expire_on_commit=False, future=True
        )

    @classmethod
    def from_url(cls, url: str, *, echo: bool = False) -> StateStore:
        """Build a store from a SQLAlchemy URL, applying the SQLite WAL hook when relevant."""
        return cls(create_state_engine(url, echo=echo))

    @property
    def engine(self) -> Engine:
        """The underlying SQLAlchemy engine (e.g. for Alembic or diagnostics)."""
        return self._engine

    async def aclose(self) -> None:
        """Dispose the underlying connection pool."""
        await asyncio.to_thread(self._engine.dispose)

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[UnitOfWork]:
        """One session, one transaction, committed only if the block does not raise.

        This is the restart-safety primitive T2.4's sync loop is built on: persisting
        envelopes and advancing the watermark inside one ``async with
        store.unit_of_work()`` block means a crash -- or any exception -- midway
        through leaves the database exactly as it was before the block started. There
        is exactly one session for the whole block; nothing here opens a session per
        write.
        """
        session = self._session_factory()
        uow = UnitOfWork(session)
        try:
            yield uow
            await asyncio.to_thread(session.commit)
        except BaseException:
            await asyncio.to_thread(session.rollback)
            raise
        finally:
            await asyncio.to_thread(session.close)

    # -- point reads (each opens and closes its own short-lived session) -------------

    async def find_binding(
        self, endpoint: str, entity_type: EntityType, tenant_id: str, native_key: str
    ) -> IdentityBinding | None:
        """Reverse (native key -> neutral id) lookup bootstrap matching needs."""

        def _read() -> IdentityBinding | None:
            with self._session_factory() as session:
                stmt = select(IdentityMapRow).where(
                    IdentityMapRow.endpoint == endpoint,
                    IdentityMapRow.entity_type == entity_type.value,
                    IdentityMapRow.tenant_id == tenant_id,
                    IdentityMapRow.native_key == native_key,
                )
                row = session.scalars(stmt).one_or_none()
                return None if row is None else _binding_from_row(row)

        return await asyncio.to_thread(_read)

    async def get_binding(
        self, neutral_id: uuid.UUID, endpoint: str, entity_type: EntityType
    ) -> IdentityBinding | None:
        """Forward (neutral id -> native key) lookup the sync loop needs."""

        def _read() -> IdentityBinding | None:
            with self._session_factory() as session:
                row = session.get(IdentityMapRow, (neutral_id, endpoint, entity_type.value))
                return None if row is None else _binding_from_row(row)

        return await asyncio.to_thread(_read)

    async def fetch_envelopes(
        self, neutral_id: uuid.UUID, endpoint: str
    ) -> dict[str, FieldEnvelope[JsonValue]]:
        """Every last-known envelope for one entity at one endpoint, keyed by field name."""

        def _read() -> dict[str, FieldEnvelope[JsonValue]]:
            with self._session_factory() as session:
                stmt = select(FieldEnvelopeRow).where(
                    FieldEnvelopeRow.neutral_id == neutral_id,
                    FieldEnvelopeRow.endpoint == endpoint,
                )
                rows = session.scalars(stmt).all()
                return {row.field: _envelope_from_row(row) for row in rows}

        return await asyncio.to_thread(_read)

    async def get_watermark(
        self, sync_pair: str, endpoint: str, entity_type: EntityType
    ) -> WatermarkState | None:
        """The stored resume token for one (sync pair, endpoint, entity type)."""

        def _read() -> WatermarkState | None:
            with self._session_factory() as session:
                row = session.get(WatermarkRow, (sync_pair, endpoint, entity_type.value))
                return None if row is None else _watermark_from_row(row)

        return await asyncio.to_thread(_read)

    async def list_orphans(
        self, endpoint: str | None = None, *, unresolved_only: bool = True
    ) -> list[OrphanRecord]:
        """Orphans for the run report, optionally filtered to one endpoint."""

        def _read() -> list[OrphanRecord]:
            with self._session_factory() as session:
                stmt = select(OrphanLogRow)
                if endpoint is not None:
                    stmt = stmt.where(OrphanLogRow.endpoint == endpoint)
                if unresolved_only:
                    stmt = stmt.where(OrphanLogRow.resolved_at.is_(None))
                rows = session.scalars(stmt).all()
                return [_orphan_from_row(row) for row in rows]

        return await asyncio.to_thread(_read)
