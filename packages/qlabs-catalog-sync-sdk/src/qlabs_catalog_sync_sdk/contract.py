"""Connector contract — the abstract base class every connector implements.

WP1 / T1.2 (foundational). This module is the **contract freeze**: the engine and every
connector are typed against what is declared here.

Binding shape (RM-01 decision D8, which overrides the synchronous sketch in RS-08
section 4.1):

* every I/O method on :class:`Connector` is ``async``;
* :meth:`Connector.list_changed` returns a :class:`ListChangedResult` carrying both the
  candidate changes *and* the connector's proposed ``next_watermark``, so the engine can
  commit envelopes and the watermark in a single transaction and a crash can never
  advance the watermark past unpersisted work (RS-07 section 2, steps 1 and 7);
* :meth:`Connector.capabilities` stays **synchronous** — it is a static declaration with
  no I/O, the engine caches it once at startup (RS-08 section 5, step 4), and connector
  documentation is generated from it at build time (RS-08 section 10). This matches the
  post-D8 skeleton in the RM-01 agent guide.

Layering. ``models.py`` (T1.1) is the leaf; this module sits directly on top of it and
depends only on ``models`` (the neutral types), ``exceptions`` (the typed error
hierarchy) and ``config`` (the runtime context), each of which imports nothing from here.

* :class:`CapabilityManifestBase` — the query surface the contract needs from a manifest.
  T1.3's concrete ``manifest.CapabilityManifest`` subclasses it, so the manifest keeps
  full freedom over its own shape while the contract stays typed and non-circular.
* :class:`ConnectorContext` and :class:`CapabilityError` / :class:`ConnectorError` are
  **re-exported** here for connector authors' convenience; they are defined in ``config``
  and ``exceptions`` respectively, and there is exactly one of each in the SDK.

Read-only source connectors. ``create``/``update``/``delete`` are **not** abstract: their
default implementations raise :class:`CapabilityError`. A read-only connector (Databricks,
Collibra, Snowflake per the v1 guardrails) therefore inherits an honest refusal and never
writes a fake write path, and the conformance kit's capability-honesty check (T1.8,
RS-08 section 9) passes without the connector calling any API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Final

from pydantic import AwareDatetime, ConfigDict, Field, JsonValue, model_validator
from pydantic_settings import BaseSettings

from .config import ConnectorContext
from .exceptions import CapabilityError, ConnectorError
from .models import (
    EntityType,
    FieldDiff,
    IdentityRef,
    NeutralEntity,
    NeutralModel,
)

__all__ = [
    "SDK_CONTRACT_VERSION",
    "CapabilityError",
    "CapabilityManifestBase",
    "ChangeKind",
    "ChangeRef",
    "Connector",
    "ConnectorContext",
    "ConnectorError",
    "EntityType",
    "FieldDiff",
    "HealthState",
    "HealthStatus",
    "IdentityRef",
    "ListChangedResult",
    "NeutralEntity",
    "Watermark",
    "WatermarkKind",
    "WriteOutcome",
    "WriteResult",
]


#: Contract major version. The engine refuses to load a connector whose
#: :attr:`Connector.sdk_contract_version` differs from this (RS-08 section 7). Defined
#: here because :class:`Connector` stamps it; T1.9's ``version.py`` re-exports it.
SDK_CONTRACT_VERSION: Final[int] = 1


# --------------------------------------------------------------------------------------
# Capability manifest anchor (concrete type: manifest.CapabilityManifest, T1.3)
# --------------------------------------------------------------------------------------


class CapabilityManifestBase(NeutralModel, ABC):
    """The query surface every capability manifest must expose.

    T1.3's ``manifest.CapabilityManifest`` subclasses this and adds the declarative data
    model (``entities``, ``EntityCapability``, ``FieldCapability`` with its ``rw``/``ro``/
    ``na`` mode, ``allowed_update_paths``, connector-level ``concurrency``). Only the three
    questions the contract itself asks are declared here, so T1.3 keeps full freedom over
    how the manifest is *shaped* while the contract stays typed and non-circular.
    """

    @abstractmethod
    def supports(self, entity_type: EntityType) -> bool:
        """True when this connector handles ``entity_type`` at all.

        An unsupported entity type is simply never scheduled for this endpoint
        (RS-08 section 8).
        """

    @abstractmethod
    def is_writable(self, entity_type: EntityType, field: str) -> bool:
        """True when ``field`` is declared writable (``rw``) for ``entity_type``.

        False for a field declared ``ro`` or ``na``, and for any field of an unsupported
        entity type. The engine never writes a field for which this is False.
        """

    @abstractmethod
    def requires_full_replace(self, entity_type: EntityType, field: str) -> bool:
        """True when writing ``field`` needs the complete new value.

        Mirrors ``FieldDiff.requires_full_replace`` on the read side: the engine widens a
        partial diff into a read-modify-write full replacement before calling
        :meth:`Connector.update` (RS-07 section 2; Qlik data-product arrays are the
        motivating case).
        """


# --------------------------------------------------------------------------------------
# Watermark — the per-endpoint, per-entity-type resume token
# --------------------------------------------------------------------------------------


class WatermarkKind(StrEnum):
    """What a :class:`Watermark` actually carries, and therefore how it may be compared."""

    INITIAL = "initial"
    """Nothing has been synced for this stream yet — the next poll is a full scan."""

    TIMESTAMP = "timestamp"
    """A modified-since instant (Databricks ``updated_at``, Qlik RFC3339 ``updatedAt``)."""

    CURSOR = "cursor"
    """An opaque page/continuation token. Meaningful only to the connector that issued it."""


class Watermark(NeutralModel):
    """The resume position for one ``(endpoint, entity_type)`` poll stream.

    Deliberately not a bare ``str``: the state store has to round-trip it and the engine
    has to be able to tell a full-scan start from a modified-since instant from an opaque
    page token. Every shape the MVP needs fits — a Databricks ``updated_at``, a Qlik
    RFC3339 ``updatedAt`` (both :attr:`WatermarkKind.TIMESTAMP`), and a continuation token
    (:attr:`WatermarkKind.CURSOR`).

    Frozen: a watermark is a value the connector *proposes* and the engine commits
    alongside the envelopes it belongs to. Making it immutable removes the whole class of
    restart-safety bug where something mutates the proposal mid-transaction. Build a new
    one (or ``model_copy(update=...)``) instead of mutating.

    Persistence is ``model_dump(mode="json")`` / ``model_validate`` — the timestamp
    serializes as ISO-8601 with an explicit offset and comes back identical.
    """

    model_config = ConfigDict(frozen=True)

    endpoint: str = Field(min_length=1)
    entity_type: EntityType
    kind: WatermarkKind = WatermarkKind.INITIAL
    timestamp: AwareDatetime | None = None
    cursor: str | None = None
    observed_at: AwareDatetime | None = None
    """When the connector proposed this watermark; diagnostic only, never compared."""

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> Watermark:
        if self.kind is WatermarkKind.TIMESTAMP and self.timestamp is None:
            raise ValueError("a timestamp watermark requires `timestamp`")
        if self.kind is WatermarkKind.CURSOR and not self.cursor:
            raise ValueError("a cursor watermark requires `cursor`")
        if self.kind is WatermarkKind.INITIAL and (self.timestamp is not None or self.cursor):
            raise ValueError("an initial watermark carries neither `timestamp` nor `cursor`")
        if self.kind is WatermarkKind.TIMESTAMP and self.cursor:
            raise ValueError("a timestamp watermark must not also carry `cursor`")
        if self.kind is WatermarkKind.CURSOR and self.timestamp is not None:
            raise ValueError("a cursor watermark must not also carry `timestamp`")
        return self

    @classmethod
    def initial(cls, endpoint: str, entity_type: EntityType) -> Watermark:
        """The starting position for a stream that has never been synced."""
        return cls(endpoint=endpoint, entity_type=entity_type, kind=WatermarkKind.INITIAL)

    @classmethod
    def at(
        cls,
        endpoint: str,
        entity_type: EntityType,
        timestamp: datetime,
        *,
        observed_at: datetime | None = None,
    ) -> Watermark:
        """A modified-since watermark. ``timestamp`` must be timezone-aware."""
        return cls(
            endpoint=endpoint,
            entity_type=entity_type,
            kind=WatermarkKind.TIMESTAMP,
            timestamp=timestamp,
            observed_at=observed_at,
        )

    @classmethod
    def from_cursor(
        cls,
        endpoint: str,
        entity_type: EntityType,
        cursor: str,
        *,
        observed_at: datetime | None = None,
    ) -> Watermark:
        """An opaque continuation-token watermark."""
        return cls(
            endpoint=endpoint,
            entity_type=entity_type,
            kind=WatermarkKind.CURSOR,
            cursor=cursor,
            observed_at=observed_at,
        )

    @property
    def stream_key(self) -> str:
        """``"<endpoint>:<entity_type>"`` — the state store's per-stream primary key."""
        return f"{self.endpoint}:{self.entity_type.value}"

    @property
    def is_initial(self) -> bool:
        """True when nothing has been synced for this stream yet."""
        return self.kind is WatermarkKind.INITIAL

    def same_stream_as(self, other: Watermark) -> bool:
        """True when both watermarks describe the same ``(endpoint, entity_type)`` stream."""
        return self.endpoint == other.endpoint and self.entity_type == other.entity_type

    def is_after(self, other: Watermark) -> bool:
        """True when this watermark is strictly ahead of ``other`` on the same stream.

        Only timestamps are ordered. Two cursor watermarks are **not** comparable — a
        continuation token is opaque by definition — so this returns ``False`` for them
        and the engine simply commits what the connector proposed. Any watermark is after
        :attr:`WatermarkKind.INITIAL`.

        Raises ``ValueError`` when the two describe different streams: comparing across
        streams is a programming error, not a "no".
        """
        if not self.same_stream_as(other):
            raise ValueError(
                f"cannot compare watermarks from different streams: "
                f"{self.stream_key} vs {other.stream_key}"
            )
        if self.is_initial:
            return False
        if other.is_initial:
            return True
        mine, theirs = self.timestamp, other.timestamp
        if mine is not None and theirs is not None:
            return mine > theirs
        return False


# --------------------------------------------------------------------------------------
# ChangeRef / ListChangedResult — what list_changed returns (D8)
# --------------------------------------------------------------------------------------


class ChangeKind(StrEnum):
    """What the source knew about a candidate change, cheaply, from its listing."""

    UPSERT = "upsert"
    """The object exists and looks changed; the source cannot say created vs updated.

    The default, and the honest answer for a poll-based listing filtered by modified-time
    (both MVP connectors).
    """

    CREATED = "created"
    UPDATED = "updated"

    DELETED = "deleted"
    """The source reports the object as gone. v1 never deletes in Qlik (decision D4) — the
    engine reports these as orphans."""


class ChangeRef(NeutralModel):
    """One candidate changed object returned by :meth:`Connector.list_changed`.

    Carries exactly enough to call :meth:`Connector.read` on it (:attr:`ref`) plus what
    the listing already revealed for free, so the engine can short-circuit an unchanged
    object without a second round trip.
    """

    ref: IdentityRef
    kind: ChangeKind = ChangeKind.UPSERT
    source_revision: str | None = None
    """ETag / revision counter / version tag, when the listing exposes one."""

    last_modified_at: AwareDatetime | None = None
    """The source's own last-modified instant, when the listing exposes one."""

    display_name: str | None = None
    """A cheap human label for logs and the run report; never used for identity."""

    @property
    def endpoint(self) -> str:
        """The endpoint this change was observed at."""
        return self.ref.endpoint

    @property
    def entity_type(self) -> EntityType:
        """The neutral entity type of the changed object."""
        return self.ref.entity_type

    @property
    def is_delete(self) -> bool:
        """True when the source reports the object as gone."""
        return self.kind is ChangeKind.DELETED


class ListChangedResult(NeutralModel):
    """The result of one :meth:`Connector.list_changed` call (decision D8).

    Pairing the candidates with the connector's *proposed* next watermark is what makes
    restart safety real: the engine writes the envelopes it derived from
    :attr:`changes` and commits :attr:`next_watermark` in the same transaction, so a
    crash can never advance the watermark past unpersisted work (RS-07 section 2, step 7).

    :attr:`has_more` separates the two cases the engine must not confuse:

    * ``changes == [] and not has_more`` — caught up; nothing more to do this cycle.
    * ``has_more`` — this was one page; call again with :attr:`next_watermark` to
      continue, whether or not this page was empty.
    """

    changes: list[ChangeRef] = Field(default_factory=list)
    next_watermark: Watermark
    has_more: bool = False

    @model_validator(mode="after")
    def _changes_belong_to_the_watermark_stream(self) -> ListChangedResult:
        mark = self.next_watermark
        for change in self.changes:
            if change.ref.endpoint != mark.endpoint or change.ref.entity_type != mark.entity_type:
                raise ValueError(
                    f"change {change.ref.native_key!r} belongs to stream "
                    f"{change.ref.endpoint}:{change.ref.entity_type.value}, "
                    f"not to {mark.stream_key}"
                )
        return self

    @classmethod
    def empty(cls, next_watermark: Watermark) -> ListChangedResult:
        """Nothing changed and there are no further pages."""
        return cls(changes=[], next_watermark=next_watermark, has_more=False)

    @property
    def is_empty(self) -> bool:
        """True when this call surfaced no candidates (which is not the same as exhausted)."""
        return not self.changes

    @property
    def is_exhausted(self) -> bool:
        """True when the connector has no further pages for this watermark."""
        return not self.has_more


# --------------------------------------------------------------------------------------
# WriteResult — what create/update return
# --------------------------------------------------------------------------------------


class WriteOutcome(StrEnum):
    """Whether a write actually changed anything at the target."""

    CREATED = "created"
    UPDATED = "updated"
    NO_OP = "no_op"
    """Nothing was written — the target already matched. The engine's idempotency claim
    ("replaying a cycle finds no diff and no-ops") is only checkable because this is
    distinguishable from a real write."""


class WriteResult(NeutralModel):
    """The outcome of a :meth:`Connector.create` or :meth:`Connector.update`.

    Carries the resulting native identity, the new concurrency token if the endpoint
    returned one, and — crucially — whether anything was actually written.
    :attr:`skipped_fields` plus :attr:`detail` is how a connector reports a partial write
    honestly rather than silently: decision D2 requires unresolved Qlik dataset members to
    be *omitted and reported*, never invented.
    """

    ref: IdentityRef
    outcome: WriteOutcome
    source_revision: str | None = None
    """The new ETag / revision counter, when the endpoint returned one."""

    written_fields: list[str] = Field(default_factory=list)
    skipped_fields: list[str] = Field(default_factory=list)
    detail: str | None = None
    """Human-readable context for the run report — why something was skipped, typically."""

    @model_validator(mode="after")
    def _no_op_writes_nothing(self) -> WriteResult:
        if self.outcome is WriteOutcome.NO_OP and self.written_fields:
            raise ValueError("a no-op write cannot report written fields")
        return self

    @classmethod
    def _of(
        cls,
        outcome: WriteOutcome,
        ref: IdentityRef,
        *,
        source_revision: str | None = None,
        written_fields: Sequence[str] | None = None,
        skipped_fields: Sequence[str] | None = None,
        detail: str | None = None,
    ) -> WriteResult:
        return cls(
            ref=ref,
            outcome=outcome,
            source_revision=source_revision,
            written_fields=list(written_fields or ()),
            skipped_fields=list(skipped_fields or ()),
            detail=detail,
        )

    @classmethod
    def created(
        cls,
        ref: IdentityRef,
        *,
        source_revision: str | None = None,
        written_fields: Sequence[str] | None = None,
        skipped_fields: Sequence[str] | None = None,
        detail: str | None = None,
    ) -> WriteResult:
        """A new object was created at the target."""
        return cls._of(
            WriteOutcome.CREATED,
            ref,
            source_revision=source_revision,
            written_fields=written_fields,
            skipped_fields=skipped_fields,
            detail=detail,
        )

    @classmethod
    def updated(
        cls,
        ref: IdentityRef,
        *,
        source_revision: str | None = None,
        written_fields: Sequence[str] | None = None,
        skipped_fields: Sequence[str] | None = None,
        detail: str | None = None,
    ) -> WriteResult:
        """An existing object was mutated at the target."""
        return cls._of(
            WriteOutcome.UPDATED,
            ref,
            source_revision=source_revision,
            written_fields=written_fields,
            skipped_fields=skipped_fields,
            detail=detail,
        )

    @classmethod
    def no_op(
        cls,
        ref: IdentityRef,
        *,
        source_revision: str | None = None,
        skipped_fields: Sequence[str] | None = None,
        detail: str | None = None,
    ) -> WriteResult:
        """The target already matched; no API mutation was issued."""
        return cls._of(
            WriteOutcome.NO_OP,
            ref,
            source_revision=source_revision,
            skipped_fields=skipped_fields,
            detail=detail,
        )

    @property
    def changed(self) -> bool:
        """True when this write actually mutated the target."""
        return self.outcome is not WriteOutcome.NO_OP


# --------------------------------------------------------------------------------------
# HealthStatus — what healthcheck returns
# --------------------------------------------------------------------------------------


class HealthState(StrEnum):
    """How usable an endpoint is right now."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    """Usable but impaired (throttled, a secondary API unavailable). Still scheduled."""

    UNHEALTHY = "unhealthy"
    """Unusable. The engine quarantines this endpoint and keeps the others running."""


class HealthStatus(NeutralModel):
    """The result of :meth:`Connector.healthcheck`.

    A red status quarantines one endpoint without stopping the others (RS-08 section 5,
    step 5), so it has to say *why*: anything other than :attr:`HealthState.HEALTHY` is
    rejected without a ``reason``. Never put credential material in :attr:`details`.
    """

    endpoint: str = Field(min_length=1)
    state: HealthState
    reason: str | None = None
    checked_at: AwareDatetime | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)
    """Structured, non-secret diagnostics (latency, API version, failing sub-check)."""

    @model_validator(mode="after")
    def _unhealthy_states_explain_themselves(self) -> HealthStatus:
        if self.state is not HealthState.HEALTHY and not self.reason:
            raise ValueError(f"a {self.state.value} health status must carry a reason")
        return self

    @classmethod
    def healthy(
        cls,
        endpoint: str,
        *,
        checked_at: datetime | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> HealthStatus:
        """The endpoint is fully usable."""
        return cls(
            endpoint=endpoint,
            state=HealthState.HEALTHY,
            checked_at=checked_at,
            details=details or {},
        )

    @classmethod
    def degraded(
        cls,
        endpoint: str,
        reason: str,
        *,
        checked_at: datetime | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> HealthStatus:
        """The endpoint is impaired but still scheduled."""
        return cls(
            endpoint=endpoint,
            state=HealthState.DEGRADED,
            reason=reason,
            checked_at=checked_at,
            details=details or {},
        )

    @classmethod
    def unhealthy(
        cls,
        endpoint: str,
        reason: str,
        *,
        checked_at: datetime | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> HealthStatus:
        """The endpoint is unusable and must be quarantined."""
        return cls(
            endpoint=endpoint,
            state=HealthState.UNHEALTHY,
            reason=reason,
            checked_at=checked_at,
            details=details or {},
        )

    @property
    def is_healthy(self) -> bool:
        """True only for :attr:`HealthState.HEALTHY`."""
        return self.state is HealthState.HEALTHY

    @property
    def should_quarantine(self) -> bool:
        """True when the engine must stop scheduling this endpoint."""
        return self.state is HealthState.UNHEALTHY


# --------------------------------------------------------------------------------------
# The Connector ABC
# --------------------------------------------------------------------------------------


class Connector(ABC):
    """The contract every connector implements and the engine's only view of an endpoint.

    Subclasses set :attr:`name` (matching their entry-point name) and :attr:`ConfigModel`,
    implement :meth:`capabilities`, :meth:`setup`, :meth:`healthcheck`,
    :meth:`list_changed` and :meth:`read`, and — only if they write — override
    :meth:`create`, :meth:`update` and :meth:`delete`.

    Connectors hold no sync state. The IdentityMap, watermarks and last-known envelopes
    all live in the engine's state store; a connector owns nothing but live clients and
    sessions (RS-08 section 5).
    """

    name: ClassVar[str] = ""
    """The endpoint key. Must equal the ``qlabs_catalog_sync.connectors`` entry-point name
    under which this class is registered; it is what appears in config, the IdentityMap
    and every log line."""

    ConfigModel: ClassVar[type[BaseSettings]]
    """The connector's ``ConnectorConfig`` subclass (T1.7). The engine builds and validates
    an instance of it and hands it back through ``ctx.config``."""

    sdk_contract_version: ClassVar[int] = SDK_CONTRACT_VERSION
    """Stamped by this base class. The engine's discovery gate (T1.9) rejects a connector
    whose value differs from the SDK it is running against."""

    def __init__(self) -> None:
        cls = type(self)
        if not cls.name:
            raise TypeError(
                f"{cls.__qualname__} must set a class-level `name` matching its "
                f"`qlabs_catalog_sync.connectors` entry-point name"
            )

    # -- declaration ---------------------------------------------------------------

    @abstractmethod
    def capabilities(self) -> CapabilityManifestBase:
        """Declare what this connector supports. Synchronous: a static declaration, no I/O.

        The engine caches the manifest once and plans field-level sync strictly from it,
        so it must be honest — the conformance kit fails a manifest that claims a field is
        writable when the write path refuses it.
        """

    # -- lifecycle -----------------------------------------------------------------

    @abstractmethod
    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        """Build vendor clients, resolve auth and validate config against the endpoint.

        Called once per configured endpoint before any other I/O method.
        """

    @abstractmethod
    async def healthcheck(self) -> HealthStatus:
        """Pre-flight check run before a cycle. A red result quarantines this endpoint."""

    async def close(self) -> None:
        """Release clients and sessions. Optional — the default is a no-op."""
        return None

    # -- read path -----------------------------------------------------------------

    @abstractmethod
    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        """List candidates changed since ``since``, plus the proposed next watermark.

        ``since`` is always a real :class:`Watermark`; a stream that has never run gets
        ``Watermark.initial(...)`` rather than ``None``. Returning
        :class:`ListChangedResult` rather than a bare iterable is decision D8: the engine
        commits the returned envelopes and :attr:`ListChangedResult.next_watermark` in one
        transaction. Set :attr:`ListChangedResult.has_more` when this was one page of
        several.
        """

    @abstractmethod
    async def read(self, ref: IdentityRef) -> NeutralEntity:
        """Read one object and map it to a neutral entity with its provenance sidecar.

        The concrete return is whichever :class:`NeutralEntity` subclass matches
        ``ref.entity_type`` — returning a ``DataProduct`` from an override is a valid
        narrowing. Populate ``field_envelopes`` so the engine can diff by checksum.

        Raise ``NotFound`` (T1.7) when the object is gone.
        """

    # -- write path (default: refuse, so read-only connectors stay read-only) --------

    async def create(self, entity: NeutralEntity) -> WriteResult:
        """Create ``entity`` at this endpoint.

        The default refuses with :class:`CapabilityError`, which is exactly what a
        read-only source connector wants: it inherits an honest refusal and never writes a
        stub that could reach an API. Write connectors override this.

        Overrides must keep the parameter typed as :class:`NeutralEntity` — narrowing it
        to a single subclass is a Liskov violation mypy will reject. Reject unsupported
        entity types inside the body via :meth:`ensure_supported`.
        """
        entity_type: EntityType | None = getattr(type(entity), "ENTITY_TYPE", None)
        raise self._capability_error("create", entity_type)

    async def update(self, ref: IdentityRef, diff: FieldDiff) -> WriteResult:
        """Apply ``diff`` as the smallest native mutation this endpoint supports.

        The default refuses with :class:`CapabilityError`. Write connectors override this
        and should call :meth:`ensure_writable` first, then translate the diff — honoring
        each change's replace-vs-patch intent and sending the concurrency token when the
        manifest declares one.
        """
        raise self._capability_error("update", ref.entity_type)

    async def delete(self, ref: IdentityRef) -> None:
        """Delete or lifecycle-retire the object.

        The default refuses with :class:`CapabilityError`. Note decision D4: v1 never
        deletes in Qlik — the Qlik connector implements this for contract completeness and
        the engine has no path that calls it.
        """
        raise self._capability_error("delete", ref.entity_type)

    # -- capability guards ----------------------------------------------------------

    def ensure_supported(self, entity_type: EntityType, *, operation: str = "sync") -> None:
        """Raise :class:`CapabilityError` when this connector does not support ``entity_type``."""
        if not self.capabilities().supports(entity_type):
            raise self._capability_error(operation, entity_type)

    def ensure_writable(self, diff: FieldDiff) -> None:
        """Raise :class:`CapabilityError` for anything in ``diff`` the manifest won't write.

        Write connectors call this at the top of :meth:`update`. It is what makes the
        conformance kit's capability-honesty rule true by construction: a write to a field
        declared ``ro``/``na`` raises before any API call is made.
        """
        manifest = self.capabilities()
        if not manifest.supports(diff.entity_type):
            raise self._capability_error("update", diff.entity_type)
        for field_name in diff.field_names:
            if not manifest.is_writable(diff.entity_type, field_name):
                raise self._capability_error("update", diff.entity_type, field=field_name)

    def _capability_error(
        self,
        operation: str,
        entity_type: EntityType | None = None,
        *,
        field: str | None = None,
    ) -> CapabilityError:
        """Build the :class:`CapabilityError` for an operation this connector does not declare."""
        target = entity_type.value if entity_type is not None else "that entity type"
        where = f", field {field!r}" if field else ""
        return CapabilityError(
            f"connector {self.name!r} does not declare {operation} for {target}{where}",
            endpoint=self.name,
            entity_type=entity_type,
            field=field,
            operation=operation,
        )
