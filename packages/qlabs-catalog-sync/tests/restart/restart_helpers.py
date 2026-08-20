"""Builders and whitebox helpers for the restart-safety tests (T8.4).

WP8 / T8.4. This module holds everything that is *not* an assertion: entity builders,
a deterministic clock, a full-state-store snapshot (used to prove "nothing churned" on
more than one known row), and two purpose-built connector doubles for injecting a
failure at the exact point a claim needs it to land:

* :class:`FailFromNth` -- the *n*-th (and every later) call to one write method raises,
  but every call before it goes through the real in-memory write path and genuinely
  mutates the target. This is how a multi-record cycle proves "the first write really
  landed, then the cycle failed" rather than "nothing happened at all".
* :class:`CrashAfterWrite` -- a *single* record's write is allowed to complete for
  real (the target's in-memory store is mutated, exactly as a successful round trip to
  Qlik would be) and only then raises, simulating a process crash in the instant after
  the wire call succeeded and before the engine's own code continues. This is the
  sharpest form of "a crash between write and commit": no second record is needed to
  produce it.

Also :func:`make_commit_crash`, which makes the *state store's own* commit -- not any
connector -- raise once, so a failure can be proved to leave nothing committed even
when every connector call succeeded cleanly.

Test helper modules land in one flat namespace under this repository's pytest import
mode (``--import-mode=importlib``); this basename is deliberately not the bare
``helpers`` two other test packages already claim.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from qlabs_catalog_sync.state.models import (
    FieldEnvelopeRow,
    IdentityMapRow,
    OrphanLogRow,
)
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.contract import Watermark
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    DataProductStatus,
    EntityType,
    FieldDiff,
    IdentityRef,
    NeutralEntity,
    Party,
    PartyRole,
    Tag,
    TextField,
)
from qlabs_catalog_sync_sdk.testing import DEFAULT_TENANT_ID, FakeConnector, qlik_shaped_manifest

__all__ = [
    "SOURCE_ENDPOINT",
    "START",
    "TARGET_ENDPOINT",
    "WRITE_METHODS",
    "CrashAfterWrite",
    "Clock",
    "FailFromNth",
    "StateSnapshot",
    "bind",
    "cursor_position",
    "data_product",
    "make_commit_crash",
    "no_sleep",
    "seed_product",
    "snapshot_state",
    "target_identity",
    "write_calls",
]

START = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)

WRITE_METHODS = ("create", "update", "delete")

SOURCE_ENDPOINT = "fake-source"
TARGET_ENDPOINT = "fake-target"


class Clock:
    """A deterministic clock that advances one second per read."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start

    def __call__(self) -> datetime:
        current = self._now
        self._now += timedelta(seconds=1)
        return current


async def no_sleep(seconds: float) -> None:
    """The loop's backoff, made instantaneous. Retries are still really performed."""
    return None


# ------------------------------------------------------------------------------------
# Entity and identity builders
# ------------------------------------------------------------------------------------


def data_product(
    name: str,
    *,
    description: str | None = None,
    tags: Sequence[tuple[str, str | None]] = (),
    owners: Sequence[str] = (),
    status: DataProductStatus | None = None,
) -> DataProduct:
    """A neutral data product with just enough shape to produce a real, multi-field diff."""
    return DataProduct(
        name=name,
        description=None if description is None else TextField.plain(description),
        tags=[Tag(key=key, value=value) for key, value in tags],
        owners=[Party(email=email, role=PartyRole.OWNER) for email in owners],
        status=status,
    )


def seed_product(connector: FakeConnector, native_key: str, **kwargs: object) -> IdentityRef:
    """Seed one data product into ``connector`` under an explicit native key.

    Native keys are Unity Catalog paths (``catalog.schema``) because that is what
    decision D1's ``catalog.schema`` selector is applied to.
    """
    name = str(kwargs.pop("name", native_key.split(".")[-1]))
    return connector.seed(data_product(name, **kwargs), native_key=native_key)  # type: ignore[arg-type]


def target_identity(native_key: str, endpoint: str = TARGET_ENDPOINT) -> IdentityRef:
    """A target-side :class:`IdentityRef` for ``native_key``."""
    return IdentityRef(
        endpoint=endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=native_key,
        tenant_id=DEFAULT_TENANT_ID,
    )


async def bind(
    store: StateStore,
    *identities: IdentityRef,
    neutral_id: uuid.UUID | None = None,
    confirmed: bool = True,
) -> uuid.UUID:
    """Bind one neutral id to each ``identity``, standing in for T7.1's confirmation step.

    Written straight through the state store because the *loop* is what is under test
    here: arranging a confirmed binding by hand is setup, not the behavior.
    """
    identifier = neutral_id if neutral_id is not None else uuid.uuid4()
    now = datetime.now(UTC)
    async with store.unit_of_work() as uow:
        for identity in identities:
            await uow.bind_identity(identifier, identity, confirmed=confirmed, now=now)
    return identifier


def write_calls(connector: FakeConnector) -> list[str]:
    """Every recorded write-path call on ``connector``, in order.

    The zero-write claim is made against this, not against the run report: "the report
    says zero" is not evidence the connector was never called.
    """
    return [entry.method for entry in connector.call_log if entry.method in WRITE_METHODS]


def cursor_position(token: str | None) -> str | None:
    """The cursor inside a serialized watermark token, or ``None`` for an unset watermark."""
    if token is None:
        return None
    return Watermark.model_validate_json(token).cursor


# ------------------------------------------------------------------------------------
# Full state-store snapshot -- "nothing churned", made a real, non-vacuous claim
# ------------------------------------------------------------------------------------

_IDENTITY_COLUMNS = (
    "neutral_id",
    "endpoint",
    "entity_type",
    "tenant_id",
    "native_key",
    "secondary_keys",
    "confirmed",
    "confirmed_at",
    "created_at",
    "updated_at",
)
_ENVELOPE_COLUMNS = (
    "neutral_id",
    "endpoint",
    "entity_type",
    "field",
    "value_json",
    "source_endpoint",
    "source_revision",
    "last_modified_at",
    "last_synced_at",
    "checksum",
    "updated_at",
)
_ORPHAN_COLUMNS = (
    "neutral_id",
    "endpoint",
    "entity_type",
    "native_key",
    "first_missing_at",
    "last_missing_at",
    "last_seen_at",
    "resolved_at",
)


def _comparable(value: Any) -> Any:
    """A row-column value made stably sortable and comparable.

    ``value_json``/``secondary_keys`` are dicts/lists straight off a JSON column, and
    Python cannot order two dicts (``<`` raises) or two lists of dicts -- which would
    break sorting a whole table into one deterministic sequence to compare. Everything
    else (UUID, str, bool, datetime) already orders and compares the way this needs.
    """
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _row_tuple(row: Any, columns: Sequence[str]) -> tuple[tuple[str, Any], ...]:
    return tuple((name, _comparable(getattr(row, name))) for name in columns)


@dataclass(frozen=True)
class StateSnapshot:
    """Every row of every entity-bearing state-store table, in a comparable form.

    Deliberately excludes ``watermarks``: that table's ``last_run_at``/``updated_at``
    legitimately change on *every* committed cycle, including a genuine no-op one, so
    comparing it wholesale would flag expected bookkeeping as spurious churn. Compare
    the watermark's actual resume position instead (:func:`cursor_position`, or the raw
    ``watermark_token`` via :meth:`StateStore.get_watermark`).
    """

    identities: tuple[tuple[tuple[str, Any], ...], ...]
    envelopes: tuple[tuple[tuple[str, Any], ...], ...]
    orphans: tuple[tuple[tuple[str, Any], ...], ...]


def snapshot_state(store: StateStore) -> StateSnapshot:
    """Read every row of ``identity_map``, ``field_envelopes`` and ``orphan_log``.

    Whitebox: reaches past the store's own read helpers into its private session
    factory and the raw ORM rows -- the same technique
    ``tests/state/test_unit_of_work.py``'s own whitebox test uses to count sessions --
    so a comparison of two snapshots is a genuine check of what is on disk, not a check
    mediated by the very read path the write path also has to get right.
    """
    with store._session_factory() as session:  # noqa: SLF001 - deliberate whitebox
        identities = tuple(
            sorted(
                _row_tuple(row, _IDENTITY_COLUMNS)
                for row in session.scalars(select(IdentityMapRow)).all()
            )
        )
        envelopes = tuple(
            sorted(
                _row_tuple(row, _ENVELOPE_COLUMNS)
                for row in session.scalars(select(FieldEnvelopeRow)).all()
            )
        )
        orphans = tuple(
            sorted(
                _row_tuple(row, _ORPHAN_COLUMNS)
                for row in session.scalars(select(OrphanLogRow)).all()
            )
        )
    return StateSnapshot(identities=identities, envelopes=envelopes, orphans=orphans)


# ------------------------------------------------------------------------------------
# Failure-injecting target connectors
# ------------------------------------------------------------------------------------


class FailFromNth(FakeConnector):
    """A Qlik-shaped write target whose write path starts failing from the Nth call.

    ``FakeConnector.fail_next`` is FIFO from the *next* call, which cannot express "let
    the first write really land, then fail" -- exactly the shape a mid-cycle failure
    has when a second record in the same page is what actually breaks the cycle. Calls
    before ``fail_from`` go through the real in-memory write path and genuinely mutate
    the target; calls from ``fail_from`` on raise instead. Set ``fail_from`` back to
    ``None`` to let a retry succeed.
    """

    name: ClassVar[str] = TARGET_ENDPOINT

    def __init__(self, *, error: BaseException, method: str = "update", **kwargs: Any) -> None:
        super().__init__(manifest=qlik_shaped_manifest(), **kwargs)
        self.fail_from: int | None = None
        self.failing_method = method
        self.error = error
        self._writes = 0

    def _maybe_fail(self, method: str) -> None:
        if method != self.failing_method:
            return
        self._writes += 1
        if self.fail_from is not None and self._writes >= self.fail_from:
            raise self.error

    async def update(self, ref: IdentityRef, diff: FieldDiff) -> Any:
        self._maybe_fail("update")
        return await super().update(ref, diff)

    async def create(self, entity: NeutralEntity) -> Any:
        self._maybe_fail("create")
        return await super().create(entity)


class _SimulatedCrash(Exception):
    """Stands in for whatever kills the process in the instant after a write landed."""


class CrashAfterWrite(FakeConnector):
    """A Qlik-shaped write target that really writes, then simulates the process dying.

    ``FakeConnector.fail_next`` raises *before* any state is touched (its own docstring
    says so), which cannot express "the write reached Qlik and then the process died"
    -- exactly the case ``sync/loop.py``'s module docstring says is the whole reason
    :attr:`~qlabs_catalog_sync_sdk.contract.WriteOutcome.NO_OP` exists. This subclass
    calls straight through to the real, state-mutating
    ``FakeConnector.update``/``create`` and *only then* raises, so the target's
    in-memory store genuinely holds the new value by the time the engine loses the
    chance to persist anything about it.

    ``crash_on`` names the methods to crash on the *next* call to each; the crash then
    disarms itself (``crash_on.discard(...)``) so a retry with the same instance
    succeeds normally.
    """

    name: ClassVar[str] = TARGET_ENDPOINT

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(manifest=qlik_shaped_manifest(), **kwargs)
        self.crash_on: set[str] = set()

    async def update(self, ref: IdentityRef, diff: FieldDiff) -> Any:
        result = await super().update(ref, diff)
        if "update" in self.crash_on:
            self.crash_on.discard("update")
            raise _SimulatedCrash(
                "the target accepted the write; the process died before the engine "
                "could persist anything about it"
            )
        return result

    async def create(self, entity: NeutralEntity) -> Any:
        result = await super().create(entity)
        if "create" in self.crash_on:
            self.crash_on.discard("create")
            raise _SimulatedCrash(
                "the target accepted the create; the process died before the engine "
                "could anchor its identity bindings"
            )
        return result


# ------------------------------------------------------------------------------------
# Crashing the state store's own commit, independent of any connector
# ------------------------------------------------------------------------------------


def make_commit_crash(
    store: StateStore, monkeypatch: pytest.MonkeyPatch, *, error: BaseException
) -> None:
    """Make the *next real* ``session.commit()`` call anywhere on ``store`` raise
    ``error``, exactly once, then get out of the way.

    Whitebox, like :func:`snapshot_state`: wraps ``store``'s private session factory so
    every session it hands out has its ``commit`` wrapped -- simulating the process
    dying at the exact instant SQLite would have made a transaction durable, *after*
    every write in the block was flushed into it (every
    :class:`~qlabs_catalog_sync.state.store.UnitOfWork` write method calls
    ``session.flush()``, which is visible within the transaction, but not
    ``session.commit()``). ``StateStore.unit_of_work`` still calls
    ``session.rollback()`` in its ``except`` clause, so this is a genuine test of
    whether that rollback really discards a flushed-but-uncommitted write.

    Every session -- not just the one a caller expects to be "the" commit -- gets
    wrapped, deliberately: a point-read session (``get_watermark``, ``fetch_envelopes``,
    ...) opened by ``_load_watermark`` before the real ``unit_of_work`` block runs would
    otherwise consume a naively "first call" trigger without ever calling ``.commit()``
    at all, silently defusing the injected crash. Point-read sessions never call
    ``.commit()``, so wrapping them is inert; the *first actual invocation* of
    ``.commit()`` on any session -- guaranteed to be inside a real ``unit_of_work``
    block -- is the one that raises. Every commit after that (including a second
    ``unit_of_work`` block in the same test, which is exactly what makes this able to
    catch a transaction that was wrongly split into two) goes through normally, so a
    retry needs no explicit ``monkeypatch.undo()``.
    """
    original_factory = store._session_factory  # noqa: SLF001 - deliberate whitebox
    fired = False

    def crashing_factory() -> Session:
        session = original_factory()
        original_commit = session.commit

        def maybe_crashing_commit() -> None:
            nonlocal fired
            if not fired:
                fired = True
                raise error
            original_commit()

        monkeypatch.setattr(session, "commit", maybe_crashing_commit)
        return session

    monkeypatch.setattr(store, "_session_factory", crashing_factory)  # noqa: SLF001
