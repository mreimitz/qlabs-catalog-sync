"""``FakeConnector`` — the in-memory connector test double shipped from the SDK.

WP1 / T1.10. One real, in-memory implementation of :class:`~qlabs_catalog_sync_sdk.
contract.Connector`, configurable to play either v1 endpoint role:

* a **read-only source** (the Databricks/Collibra/Snowflake shape) whose write paths
  refuse with :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError`, or
* the **sole write target** (the Qlik shape) whose writes actually mutate an in-memory
  store,

driven entirely by whatever :class:`~qlabs_catalog_sync_sdk.manifest.CapabilityManifest`
(or any :class:`~qlabs_catalog_sync_sdk.contract.CapabilityManifestBase`) it is built
with — see :mod:`.manifests` for the two canned v1 shapes and the ``read_only_source`` /
``write_target`` factories below for one-line construction.

This is what the conformance kit (T1.8) tests itself against and what every engine test
(T2.x, T7.x, T8.x) exercises the sync loop against instead of a real Databricks workspace
or Qlik tenant — the RM-01 build has neither available. Everything here has to be *real*
behavior, not a mock: :meth:`FakeConnector.create` then :meth:`FakeConnector.read`
actually round-trips through an in-memory store, idempotency is computed from the same
canonical checksums (``envelope.py``, T1.6) the real diff engine (T7.2) will use, and
:meth:`FakeConnector.list_changed` implements real watermark/paging semantics rather than
returning a canned page.

Design notes, for anyone extending this:

* **Identity.** ``Connector.name`` is declared ``ClassVar[str]`` on the base (T1.2) — a
  single fixed value per *class*, matching every other connector. An engine test that
  runs two ``FakeConnector``s side by side (a source and a target) needs each to report
  a distinct, realistic endpoint key, so :meth:`FakeConnector.named` builds a tiny
  per-name subclass on the fly rather than assigning ``self.name`` (which strict mypy
  correctly rejects as writing through a class variable via an instance) or mutating the
  shared class attribute (which would leak across instances). The base class itself still
  has a sensible default (``name = "fake"``) so the plain constructor works standalone.
* **Storage.** Each stored object keeps its field values as the *faithful* JSON
  projection (:func:`~qlabs_catalog_sync_sdk.envelope.to_json_value`) plus the
  provenance sidecar (:func:`~qlabs_catalog_sync_sdk.envelope.build_field_envelopes`)
  built from it — never the typed pydantic value directly. Canonicalization treats a
  typed neutral value and its equivalent plain JSON identically (``envelope.py`` projects
  both through the same recursion), so this loses nothing and sidesteps trying to
  validate-assign a :class:`~qlabs_catalog_sync_sdk.models.FieldChange`'s already-JSON
  ``value`` into a typed pydantic attribute. :meth:`FakeConnector.read` reconstructs the
  typed entity via ``model_validate`` on the way out.
* **Ordering.** ``list_changed`` is driven by a per-``(endpoint, entity_type)`` append-only
  changelog and a monotonic integer cursor, not by the injected clock — multiple
  mutations that happen to share one instant (very possible with a clock that only moves
  when told to) still page and exhaust correctly.
* **Seeding vs. writing.** :meth:`FakeConnector.seed` inserts an object directly, bypassing
  the capability manifest entirely, because pre-existing *source* data was never subject
  to this connector's own write permission — only :meth:`create` gates on it. Seeding also
  does not append to the call log: it is test arrangement, not connector behavior under
  test.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import count
from typing import Any, ClassVar, Final, Literal, Self, cast
from uuid import UUID

from pydantic import JsonValue

from ..config import ConnectorConfig, ConnectorContext, ManualClock
from ..contract import (
    CapabilityManifestBase,
    ChangeKind,
    ChangeRef,
    Connector,
    HealthStatus,
    IdentityRef,
    ListChangedResult,
    Watermark,
    WatermarkKind,
    WriteResult,
)
from ..envelope import build_envelope, build_field_envelopes, has_changed, order_for, to_json_value
from ..exceptions import ConflictError, NotFound
from ..models import (
    Category,
    DataProduct,
    Dataset,
    EntityType,
    FieldDiff,
    FieldEnvelope,
    GlossaryTerm,
    NeutralEntity,
)
from .manifests import databricks_shaped_manifest, qlik_shaped_manifest

__all__ = [
    "DEFAULT_TENANT_ID",
    "CallRecord",
    "ContractMethod",
    "FakeConnector",
    "FakeConnectorConfig",
]

#: The tenant id every :class:`FakeConnector` uses unless overridden — arbitrary but
#: stable, so identity refs a test builds by hand for comparison need no wiring.
DEFAULT_TENANT_ID: Final[str] = "fake-tenant"

#: The contract methods :class:`FakeConnector` records on its call log and accepts
#: failure injection for. A closed set (rather than a bare ``str``) so a typo in a test's
#: ``fail_next("raed", ...)`` is a mypy error, not a silently-never-firing rule.
ContractMethod = Literal[
    "setup", "healthcheck", "close", "list_changed", "read", "create", "update", "delete"
]

_ENTITY_CLASSES: Final[dict[EntityType, type[NeutralEntity]]] = {
    EntityType.DATA_PRODUCT: DataProduct,
    EntityType.DATASET: Dataset,
    EntityType.GLOSSARY_TERM: GlossaryTerm,
    EntityType.CATEGORY: Category,
}

#: ``NeutralEntity``'s own bookkeeping fields — never part of a capability manifest's
#: per-entity ``fields`` dict, so they are excluded from every field-name enumeration
#: below.
_BASE_NEUTRAL_FIELDS: Final[frozenset[str]] = frozenset(NeutralEntity.model_fields)


def _neutral_field_names(entity_type: EntityType) -> tuple[str, ...]:
    """The neutral field names (excluding ``NeutralEntity``'s own fields) that belong to
    ``entity_type``'s model — exactly what a capability manifest's ``fields`` dict for
    that entity type is keyed by.
    """
    entity_cls = _ENTITY_CLASSES[entity_type]
    return tuple(name for name in entity_cls.model_fields if name not in _BASE_NEUTRAL_FIELDS)


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------


class FakeConnectorConfig(ConnectorConfig):
    """Empty config. ``FakeConnector`` is fully configured through its constructor and
    factory classmethods for deterministic, code-driven tests; it needs no settings or
    secrets from the environment.
    """


# --------------------------------------------------------------------------------------
# Call log
# --------------------------------------------------------------------------------------


@dataclass
class CallRecord:
    """One recorded invocation of a :class:`FakeConnector` contract method, in order.

    ``result`` is filled in after the call completes successfully (``None`` for
    :meth:`Connector.delete`, which itself returns ``None``); it stays ``None`` if the
    call raised instead. Reading ``args`` and ``result`` back is how a test asserts, for
    example, "the sync loop performed no writes on a re-run" without re-deriving that
    from side effects.
    """

    method: ContractMethod
    args: dict[str, Any]
    result: Any = None


# --------------------------------------------------------------------------------------
# In-memory storage
# --------------------------------------------------------------------------------------


@dataclass
class _Record:
    """One stored object. ``field_values`` and ``envelopes`` are the source of truth;
    :meth:`FakeConnector._materialize` rebuilds the typed entity from them on read.
    """

    ref: IdentityRef
    entity_cls: type[NeutralEntity]
    neutral_id: UUID
    field_values: dict[str, JsonValue]
    custom_attributes: dict[str, JsonValue]
    envelopes: dict[str, FieldEnvelope[JsonValue]]
    revision: int
    updated_at: datetime

    @property
    def revision_token(self) -> str:
        """The stable string form of :attr:`revision`, used as both ``source_revision``
        and the concurrency token compared against a diff's ``expected_revision``."""
        return f"rev-{self.revision}"


@dataclass(frozen=True, slots=True)
class _ChangeEvent:
    """One entry in a per-entity-type append-only changelog, the backbone of
    :meth:`FakeConnector.list_changed`'s cursor-watermark semantics."""

    native_key: str
    kind: ChangeKind
    source_revision: str | None
    at: datetime


# --------------------------------------------------------------------------------------
# FakeConnector
# --------------------------------------------------------------------------------------


class FakeConnector(Connector):
    """An in-memory, fully-real implementation of the connector contract.

    Construct directly with an arbitrary manifest (``FakeConnector(manifest=...)``), or
    use :meth:`read_only_source` / :meth:`write_target` for the two canned v1 shapes —
    both accept an explicit ``manifest=`` override when a test needs something other than
    the canned shape while keeping that role's endpoint-naming convention.
    """

    name: ClassVar[str] = "fake"
    ConfigModel = FakeConnectorConfig

    def __init__(
        self,
        *,
        manifest: CapabilityManifestBase,
        clock: ManualClock | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        list_changed_page_size: int | None = None,
    ) -> None:
        super().__init__()
        self._manifest = manifest
        self.clock: ManualClock = clock if clock is not None else ManualClock()
        self.tenant_id = tenant_id
        self.list_changed_page_size = list_changed_page_size

        self.context: ConnectorContext[FakeConnectorConfig] | None = None
        self.closed = False
        self.call_log: list[CallRecord] = []

        self._store: dict[tuple[EntityType, str], _Record] = {}
        self._changelog: dict[EntityType, list[_ChangeEvent]] = {}
        self._pending_failures: dict[ContractMethod, list[BaseException]] = {}
        self._sequence = count(1)

    # -- construction helpers ------------------------------------------------------

    @classmethod
    def named(
        cls,
        name: str,
        *,
        manifest: CapabilityManifestBase,
        clock: ManualClock | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        list_changed_page_size: int | None = None,
    ) -> Self:
        """Build an instance whose ``.name`` (endpoint key) is ``name``.

        Every other constructor path uses the class-level default (``"fake"``); this is
        the one to reach for when a test needs two or more distinctly-named
        ``FakeConnector``s side by side (a source and a target, say) so their identity
        refs and watermarks belong to genuinely different streams, exactly as two real
        endpoints would.
        """
        dynamic_cls = cast(type[Self], type(name, (cls,), {"name": name}))
        return dynamic_cls(
            manifest=manifest,
            clock=clock,
            tenant_id=tenant_id,
            list_changed_page_size=list_changed_page_size,
        )

    @classmethod
    def read_only_source(
        cls,
        *,
        name: str = "fake-source",
        manifest: CapabilityManifestBase | None = None,
        clock: ManualClock | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        list_changed_page_size: int | None = None,
    ) -> Self:
        """A one-line Databricks-shaped read-only source (:mod:`.manifests`).

        ``manifest`` overrides the canned shape while keeping the ``fake-source`` naming
        convention — pass one when a test needs a read-only manifest with a different
        entity/field mix.
        """
        return cls.named(
            name,
            manifest=manifest if manifest is not None else databricks_shaped_manifest(),
            clock=clock,
            tenant_id=tenant_id,
            list_changed_page_size=list_changed_page_size,
        )

    @classmethod
    def write_target(
        cls,
        *,
        name: str = "fake-target",
        manifest: CapabilityManifestBase | None = None,
        clock: ManualClock | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        list_changed_page_size: int | None = None,
    ) -> Self:
        """A one-line Qlik-shaped write target (:mod:`.manifests`).

        ``manifest`` overrides the canned shape while keeping the ``fake-target`` naming
        convention — pass one when a test needs a write manifest with a different
        entity/field mix.
        """
        return cls.named(
            name,
            manifest=manifest if manifest is not None else qlik_shaped_manifest(),
            clock=clock,
            tenant_id=tenant_id,
            list_changed_page_size=list_changed_page_size,
        )

    # -- declaration -----------------------------------------------------------------

    def capabilities(self) -> CapabilityManifestBase:
        return self._manifest

    # -- lifecycle ---------------------------------------------------------------------

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        self._log_call("setup", ctx=ctx)
        self._maybe_raise("setup")
        self.context = ctx

    async def healthcheck(self) -> HealthStatus:
        record = self._log_call("healthcheck")
        self._maybe_raise("healthcheck")
        status = HealthStatus.healthy(self.name, checked_at=self.clock.now())
        record.result = status
        return status

    async def close(self) -> None:
        self._log_call("close")
        self._maybe_raise("close")
        self.closed = True

    # -- read path -----------------------------------------------------------------

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        record = self._log_call("list_changed", entity_type=entity_type, since=since)
        self._maybe_raise("list_changed")
        if since.entity_type is not entity_type:
            raise ValueError(
                f"watermark is for {since.entity_type.value!r}, not {entity_type.value!r}"
            )

        if since.is_initial:
            start = 0
        elif since.kind is WatermarkKind.CURSOR:
            start = int(since.cursor) if since.cursor is not None else 0
        else:
            raise ValueError(
                f"FakeConnector only ever proposes cursor watermarks; got {since.kind!r}"
            )

        log = self._changelog.get(entity_type, [])
        remaining = log[start:]
        page_size = self.list_changed_page_size
        page = remaining if page_size is None else remaining[:page_size]
        end = start + len(page)

        changes = [
            ChangeRef(
                ref=IdentityRef(
                    endpoint=self.name,
                    entity_type=entity_type,
                    native_key=event.native_key,
                    tenant_id=self.tenant_id,
                ),
                kind=event.kind,
                source_revision=event.source_revision,
                last_modified_at=event.at,
            )
            for event in page
        ]
        result = ListChangedResult(
            changes=changes,
            next_watermark=Watermark.from_cursor(
                self.name, entity_type, str(end), observed_at=self.clock.now()
            ),
            has_more=end < len(log),
        )
        record.result = result
        return result

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        record = self._log_call("read", ref=ref)
        self._maybe_raise("read")
        stored = self._require_record(ref)
        entity = self._materialize(stored)
        record.result = entity
        return entity

    # -- write path ------------------------------------------------------------------

    async def create(self, entity: NeutralEntity) -> WriteResult:
        entity_type = type(entity).ENTITY_TYPE
        record = self._log_call("create", entity=entity)
        self._maybe_raise("create")
        self._ensure_write_capable(entity_type, operation="create")

        stored = self._insert(entity, entity_type, native_key=None)
        self._append_change(entity_type, stored.ref.native_key, ChangeKind.CREATED, stored)
        result = WriteResult.created(
            stored.ref,
            source_revision=stored.revision_token,
            written_fields=sorted(_neutral_field_names(entity_type)),
        )
        record.result = result
        return result

    async def update(self, ref: IdentityRef, diff: FieldDiff) -> WriteResult:
        record = self._log_call("update", ref=ref, diff=diff)
        self._maybe_raise("update")
        if ref.entity_type is not diff.entity_type:
            raise ValueError(
                f"ref is for {ref.entity_type.value!r}, diff is for {diff.entity_type.value!r}"
            )
        self.ensure_writable(diff)
        stored = self._require_record(ref)

        if diff.expected_revision is not None and diff.expected_revision != stored.revision_token:
            raise ConflictError(
                f"expected revision {diff.expected_revision!r} but {ref.native_key!r} "
                f"is now at {stored.revision_token!r}",
                endpoint=self.name,
                entity_type=ref.entity_type.value,
                expected_revision=diff.expected_revision,
                actual_revision=stored.revision_token,
            )

        changes_by_field = {change.field: change for change in diff.changes}
        changed: list[str] = []
        unchanged: list[str] = []
        for name, change in changes_by_field.items():
            fresh = build_envelope(change.value, source_endpoint=self.name, order=order_for(name))
            if has_changed(fresh, stored.envelopes.get(name)):
                changed.append(name)
            else:
                unchanged.append(name)

        if not changed:
            result = WriteResult.no_op(
                ref,
                source_revision=stored.revision_token,
                skipped_fields=unchanged,
                detail="stored value already matches every field in the diff",
            )
            record.result = result
            return result

        stored.revision += 1
        now = self.clock.now()
        for name in changed:
            stored.field_values[name] = changes_by_field[name].value
        refreshed = build_field_envelopes(
            {name: stored.field_values[name] for name in changed},
            source_endpoint=self.name,
            source_revision=stored.revision_token,
            last_modified_at=now,
            last_synced_at=now,
        )
        stored.envelopes.update(refreshed)
        stored.updated_at = now
        self._append_change(ref.entity_type, ref.native_key, ChangeKind.UPDATED, stored)

        result = WriteResult.updated(
            ref,
            source_revision=stored.revision_token,
            written_fields=changed,
            skipped_fields=unchanged,
        )
        record.result = result
        return result

    async def delete(self, ref: IdentityRef) -> None:
        self._log_call("delete", ref=ref)
        self._maybe_raise("delete")
        self._ensure_write_capable(ref.entity_type, operation="delete")
        self._require_record(ref)
        del self._store[(ref.entity_type, ref.native_key)]
        self._append_change(
            ref.entity_type, ref.native_key, ChangeKind.DELETED, None, at=self.clock.now()
        )

    # -- test-support API (not part of the Connector contract) -----------------------

    def seed(self, entity: NeutralEntity, *, native_key: str | None = None) -> IdentityRef:
        """Insert ``entity`` directly, as if it already existed at this endpoint.

        Bypasses the capability manifest entirely (pre-existing source data was never
        subject to this connector's own write permission) and is not recorded on
        :attr:`call_log` — seeding is test arrangement, not connector behavior under
        test. This is how a test populates a read-only :meth:`read_only_source` with
        fixture data that :meth:`list_changed` and :meth:`read` can then serve.
        """
        entity_type = type(entity).ENTITY_TYPE
        stored = self._insert(entity, entity_type, native_key=native_key)
        self._append_change(entity_type, stored.ref.native_key, ChangeKind.CREATED, stored)
        return stored.ref

    def simulate_external_edit(self, ref: IdentityRef, changes: Mapping[str, Any]) -> None:
        """Mutate a stored object as if something other than this connector edited it
        directly at the endpoint — a person editing a Qlik data product by hand, say.

        Bypasses every capability check (an external edit does not ask this connector's
        permission) and bumps the stored revision, so a caller still holding the old
        ``expected_revision`` gets a :class:`~qlabs_catalog_sync_sdk.exceptions.
        ConflictError` from its next :meth:`update`. Appends a changelog entry, so a
        later :meth:`list_changed` surfaces the edit exactly like a real out-of-band
        change would. ``changes`` maps neutral field name to its new value (typed or
        already-JSON, either works — see the module docstring's storage note).
        """
        stored = self._require_record(ref)
        stored.revision += 1
        now = self.clock.now()
        refreshed = build_field_envelopes(
            dict(changes),
            source_endpoint=self.name,
            source_revision=stored.revision_token,
            last_modified_at=now,
            last_synced_at=now,
        )
        for name, value in changes.items():
            stored.field_values[name] = to_json_value(value)
        stored.envelopes.update(refreshed)
        stored.updated_at = now
        self._append_change(ref.entity_type, ref.native_key, ChangeKind.UPDATED, stored)

    def vanish(self, ref: IdentityRef) -> None:
        """Make a stored object disappear without going through :meth:`delete`.

        Simulates the source-side disappearance the engine's orphan path (decision D4)
        has to detect — a Databricks schema someone dropped — which a read-only
        connector can exhibit even though it never implements a real ``delete``. Raises
        :class:`~qlabs_catalog_sync_sdk.exceptions.NotFound` if ``ref`` is not currently
        stored.
        """
        self._require_record(ref)
        del self._store[(ref.entity_type, ref.native_key)]
        self._append_change(
            ref.entity_type, ref.native_key, ChangeKind.DELETED, None, at=self.clock.now()
        )

    def fail_next(self, method: ContractMethod, exc: BaseException) -> None:
        """Queue ``exc`` to be raised the next time ``method`` is called.

        Queued failures for one method are FIFO: call this once to fail the very next
        call, or several times in a row to fail the next several. To fail specifically
        the Nth call, let ``N - 1`` ordinary calls happen first and call this
        immediately before the Nth. The failure is raised (and the call still recorded
        on :attr:`call_log`) before any state is touched, so a failed call never
        partially applies.
        """
        self._pending_failures.setdefault(method, []).append(exc)

    def reset_call_log(self) -> None:
        """Clear :attr:`call_log`, without touching the store, the changelog or any
        queued failure."""
        self.call_log.clear()

    def calls(self, method: ContractMethod) -> list[CallRecord]:
        """Every recorded call to ``method``, in order."""
        return [entry for entry in self.call_log if entry.method == method]

    def call_count(self, method: ContractMethod | None = None) -> int:
        """How many calls are recorded, optionally narrowed to one ``method``."""
        if method is None:
            return len(self.call_log)
        return len(self.calls(method))

    # -- internals ---------------------------------------------------------------------

    def _log_call(self, method: ContractMethod, **args: Any) -> CallRecord:
        entry = CallRecord(method=method, args=args)
        self.call_log.append(entry)
        return entry

    def _maybe_raise(self, method: ContractMethod) -> None:
        queue = self._pending_failures.get(method)
        if queue:
            raise queue.pop(0)

    def _ensure_write_capable(self, entity_type: EntityType, *, operation: str) -> None:
        """Raise :class:`CapabilityError` unless the manifest declares at least one
        writable field for ``entity_type``.

        There is no dedicated "is this entity type creatable" question on
        :class:`CapabilityManifestBase` (only the three the contract itself needs), so
        this is the generic proxy: an entity type with no ``rw`` field at all cannot
        meaningfully be created or deleted by this connector, regardless of which
        concrete manifest type it is.
        """
        self.ensure_supported(entity_type, operation=operation)
        manifest = self.capabilities()
        if not any(
            manifest.is_writable(entity_type, name) for name in _neutral_field_names(entity_type)
        ):
            raise self._capability_error(operation, entity_type)

    def _require_record(self, ref: IdentityRef) -> _Record:
        stored = self._store.get((ref.entity_type, ref.native_key))
        if stored is None:
            raise NotFound(
                f"no {ref.entity_type.value} {ref.native_key!r} at {self.name!r}",
                endpoint=self.name,
                entity_type=ref.entity_type.value,
                native_key=ref.native_key,
            )
        return stored

    def _next_key(self, entity_type: EntityType) -> str:
        return f"{entity_type.value}-{next(self._sequence)}"

    def _insert(
        self, entity: NeutralEntity, entity_type: EntityType, *, native_key: str | None
    ) -> _Record:
        key = native_key if native_key is not None else self._next_key(entity_type)
        ref = IdentityRef(
            endpoint=self.name, entity_type=entity_type, native_key=key, tenant_id=self.tenant_id
        )
        field_names = _neutral_field_names(entity_type)
        values = {name: getattr(entity, name) for name in field_names}
        now = self.clock.now()
        envelopes = build_field_envelopes(
            values,
            source_endpoint=self.name,
            source_revision="rev-1",
            last_modified_at=now,
            last_synced_at=now,
        )
        stored = _Record(
            ref=ref,
            entity_cls=type(entity),
            neutral_id=entity.neutral_id,
            field_values={name: to_json_value(value) for name, value in values.items()},
            custom_attributes=dict(entity.custom_attributes),
            envelopes=envelopes,
            revision=1,
            updated_at=now,
        )
        self._store[(entity_type, key)] = stored
        return stored

    def _materialize(self, stored: _Record) -> NeutralEntity:
        payload: dict[str, Any] = dict(stored.field_values)
        payload.update(
            neutral_id=stored.neutral_id,
            identities=[stored.ref],
            custom_attributes=dict(stored.custom_attributes),
            field_envelopes=dict(stored.envelopes),
        )
        return stored.entity_cls.model_validate(payload)

    def _append_change(
        self,
        entity_type: EntityType,
        native_key: str,
        kind: ChangeKind,
        stored: _Record | None,
        *,
        at: datetime | None = None,
    ) -> None:
        if at is None:
            at = stored.updated_at if stored is not None else self.clock.now()
        event = _ChangeEvent(
            native_key=native_key,
            kind=kind,
            source_revision=stored.revision_token if stored is not None else None,
            at=at,
        )
        self._changelog.setdefault(entity_type, []).append(event)
