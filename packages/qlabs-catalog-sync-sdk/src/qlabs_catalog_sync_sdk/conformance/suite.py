"""``ConnectorConformanceSuite`` — the conformance kit's main entry point.

WP1 / T1.8. A connector author certifies a connector by subclassing
:class:`ConnectorConformanceSuite` and providing one pytest fixture, ``connector``, that
yields an already-``setup()`` :class:`~qlabs_catalog_sync_sdk.contract.Connector`
instance. Every test method below then runs against it. See the class docstring for a
runnable example.

What each test checks, and why it is written the way it is:

* **Contract completeness** — every abstract method is implemented by construction (a
  ``Connector`` subclass missing one cannot even be instantiated, so the ``connector``
  fixture succeeding is itself proof); this suite additionally checks
  ``capabilities()`` returns a real manifest and that every entity type it declares
  ``supported`` carries non-empty ``identity_keys``.
* **Capability honesty** — an entity type the manifest does not declare refuses
  ``create``/``delete`` with :class:`CapabilityError`; a field the manifest marks
  ``ro``/``na`` refuses ``update`` the same way, *and* the refusal happens before any
  HTTP request is sent (checked via the respx harness in :mod:`.harness` — see that
  module for what this cannot see).
* **Round-trip** — for every entity type with at least one ``rw`` field: ``create`` then
  ``read`` returns the same values for every writable field; updating one writable field
  is reflected on the next ``read``; every ``ro``/``na`` field is untouched by that
  update.
* **Idempotency** — re-applying the value an update just set is a no-op: the return
  value says so, an independent re-``read`` shows the field's revision token did not
  move, the connector's own call log (when it exposes one — duck-typed, so this degrades
  gracefully for a connector that does not) agrees, and no HTTP request was sent for the
  replay.

Every check that needs a specific entity type or field to exercise skips (with a clear
reason via ``pytest.skip``) rather than silently passing when a connector's manifest
gives it nothing to work with — a fully read-only connector's manifest, for instance,
has no writable field for the round-trip/idempotency checks to use, and that is
reported as a skip, not a false green.
"""

from __future__ import annotations

import pytest

from ..config import ConnectorConfig
from ..contract import CapabilityManifestBase, Connector, HealthStatus, WriteOutcome
from ..envelope import to_json_value
from ..exceptions import CapabilityError
from ..manifest import CapabilityManifest, EntityCapability, FieldCapabilityMode
from ..models import EntityType, FieldChange, FieldDiff, FieldUpdateMode, IdentityRef
from .harness import assert_no_http_calls
from .samples import sample_entity, sample_value

__all__ = ["ConnectorConformanceSuite"]

_SYNTHETIC_NATIVE_KEY = "conformance-kit-synthetic-ref"
_SYNTHETIC_TENANT_ID = "conformance-kit-tenant"


class ConnectorConformanceSuite:
    """Subclass this to certify a connector; pytest runs its test methods against it.

    Provide one pytest fixture named ``connector`` — as a method on your subclass, or in
    a ``conftest.py`` above it — that yields a
    :class:`~qlabs_catalog_sync_sdk.contract.Connector` instance which has already had
    :meth:`~qlabs_catalog_sync_sdk.contract.Connector.setup` called and is ready for I/O
    (against a real sandbox, a mock endpoint, or an in-memory double — this suite does
    not know or care). Function-scoped is the right default: several test methods below
    write to the connector, and a fresh instance per test keeps one test's writes from
    leaking into another's assertions.

    Nothing here is specific to any one connector — the same base class is what T3.8
    (Qlik) and T4.6 (Databricks) subclass, and it is what this SDK's own test suite runs
    against :class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` in both its
    ``read_only_source`` and ``write_target`` shapes.

    Example, using the SDK's own test double::

        from collections.abc import AsyncIterator

        import pytest

        from qlabs_catalog_sync_sdk.conformance import ConnectorConformanceSuite
        from qlabs_catalog_sync_sdk.config import ConnectorContext
        from qlabs_catalog_sync_sdk.contract import Connector
        from qlabs_catalog_sync_sdk.testing import FakeConnector, FakeConnectorConfig


        class TestFakeWriteTargetConformance(ConnectorConformanceSuite):
            @pytest.fixture
            async def connector(self) -> AsyncIterator[Connector]:
                target = FakeConnector.write_target()
                await target.setup(
                    ConnectorContext.build(config=FakeConnectorConfig(), endpoint=target.name)
                )
                yield target
                await target.close()

    A real connector's fixture looks the same shape — build its real ``ConfigModel``,
    point it at a mock/sandbox endpoint, call ``setup()``, yield it, close it.
    """

    # -- contract completeness --------------------------------------------------------

    async def test_connector_declares_a_name_and_config_model(self, connector: Connector) -> None:
        assert connector.name, "Connector.name must be a non-empty entry-point key"
        config_model = type(connector).ConfigModel
        assert isinstance(config_model, type) and issubclass(config_model, ConnectorConfig), (
            f"{type(connector).__qualname__}.ConfigModel must be a ConnectorConfig subclass, "
            f"got {config_model!r}"
        )

    async def test_capabilities_returns_a_manifest(self, connector: Connector) -> None:
        manifest = connector.capabilities()
        assert isinstance(manifest, CapabilityManifestBase)

    async def test_healthcheck_returns_a_status(self, connector: Connector) -> None:
        status = await connector.healthcheck()
        assert isinstance(status, HealthStatus)

    async def test_every_supported_entity_declares_identity_keys(
        self, connector: Connector
    ) -> None:
        manifest = _require_concrete_manifest(connector)
        supported = manifest.supported_entity_types
        if not supported:
            pytest.skip("manifest declares no supported entity types")
        for entity_type in supported:
            capability = manifest.entity_capability(entity_type)
            assert capability is not None
            assert capability.identity_keys, (
                f"{entity_type.value} is declared supported but has no identity_keys"
            )

    # -- capability honesty -------------------------------------------------------------

    async def test_unsupported_entities_refuse_writes_with_capability_error(
        self, connector: Connector
    ) -> None:
        manifest = _require_concrete_manifest(connector)
        supported = manifest.supported_entity_types
        unsupported = [entity_type for entity_type in EntityType if entity_type not in supported]
        if not unsupported:
            pytest.skip("manifest supports every known entity type")
        for entity_type in unsupported:
            with pytest.raises(CapabilityError), assert_no_http_calls():
                await connector.create(sample_entity(entity_type))
            with pytest.raises(CapabilityError), assert_no_http_calls():
                await connector.delete(_synthetic_ref(connector, entity_type))

    async def test_writing_a_ro_or_na_field_raises_capability_error(
        self, connector: Connector
    ) -> None:
        manifest = _require_concrete_manifest(connector)
        checked_any = False
        for entity_type in manifest.supported_entity_types:
            capability = manifest.entity_capability(entity_type)
            assert capability is not None
            for field, _mode in _non_writable_fields(capability):
                checked_any = True
                diff = _build_diff(
                    entity_type,
                    capability,
                    field,
                    sample_value(entity_type, field),
                    expected_revision=None,
                )
                with pytest.raises(CapabilityError), assert_no_http_calls():
                    await connector.update(_synthetic_ref(connector, entity_type), diff)
        if not checked_any:
            pytest.skip("manifest declares no ro/na field on any supported entity")

    # -- round-trip -----------------------------------------------------------------

    async def test_create_then_read_round_trips_writable_fields(self, connector: Connector) -> None:
        manifest = _require_concrete_manifest(connector)
        writable_types = _writable_entity_types(manifest)
        if not writable_types:
            pytest.skip("manifest declares no entity type with any writable field")
        for entity_type in writable_types:
            capability = manifest.entity_capability(entity_type)
            assert capability is not None
            sample = sample_entity(entity_type, variant=0)

            created = await connector.create(sample)
            assert created.outcome is WriteOutcome.CREATED, (
                f"create() of a fresh {entity_type.value} returned {created.outcome}, "
                "expected CREATED"
            )

            read_back = await connector.read(created.ref)
            for field in _writable_fields(capability):
                expected = to_json_value(getattr(sample, field))
                actual = to_json_value(getattr(read_back, field))
                assert actual == expected, (
                    f"{entity_type.value}.{field} did not round-trip: "
                    f"wrote {expected!r}, read back {actual!r}"
                )
                assert field in read_back.field_envelopes, (
                    f"{entity_type.value}.{field} has no field_envelopes entry after read()"
                )

    async def test_update_of_a_writable_field_is_reflected_on_read(
        self, connector: Connector
    ) -> None:
        manifest = _require_concrete_manifest(connector)
        writable_types = _writable_entity_types(manifest)
        if not writable_types:
            pytest.skip("manifest declares no entity type with any writable field")
        for entity_type in writable_types:
            capability = manifest.entity_capability(entity_type)
            assert capability is not None
            field = _writable_fields(capability)[0]

            created = await connector.create(sample_entity(entity_type, variant=0))
            before = await connector.read(created.ref)
            new_value = sample_value(entity_type, field, variant=1)
            diff = _build_diff(
                entity_type,
                capability,
                field,
                new_value,
                expected_revision=_revision_of(before, field, created.source_revision),
            )

            updated = await connector.update(created.ref, diff)
            assert updated.outcome is WriteOutcome.UPDATED, (
                f"updating {entity_type.value}.{field} to a genuinely different value "
                f"returned {updated.outcome}, expected UPDATED"
            )
            assert field in updated.written_fields

            after = await connector.read(created.ref)
            actual = to_json_value(getattr(after, field))
            assert actual == to_json_value(new_value), (
                f"{entity_type.value}.{field} was not updated on read: got {actual!r}"
            )

    async def test_ro_and_na_fields_are_never_mutated_by_an_update(
        self, connector: Connector
    ) -> None:
        manifest = _require_concrete_manifest(connector)
        checked_any = False
        for entity_type in _writable_entity_types(manifest):
            capability = manifest.entity_capability(entity_type)
            assert capability is not None
            non_writable = _non_writable_fields(capability)
            if not non_writable:
                continue
            checked_any = True
            writable_field = _writable_fields(capability)[0]

            created = await connector.create(sample_entity(entity_type, variant=0))
            before = await connector.read(created.ref)
            new_value = sample_value(entity_type, writable_field, variant=1)
            diff = _build_diff(
                entity_type,
                capability,
                writable_field,
                new_value,
                expected_revision=_revision_of(before, writable_field, created.source_revision),
            )

            await connector.update(created.ref, diff)
            after = await connector.read(created.ref)

            for field, mode in non_writable:
                after_value = to_json_value(getattr(after, field))
                before_value = to_json_value(getattr(before, field))
                assert after_value == before_value, (
                    f"{entity_type.value}.{field} is declared {mode.value} but changed after "
                    f"an update to {writable_field!r}"
                )
        if not checked_any:
            pytest.skip("no writable entity type also declares a ro/na field to protect")

    # -- idempotency -----------------------------------------------------------------

    async def test_reapplying_an_unchanged_diff_is_a_no_op(self, connector: Connector) -> None:
        manifest = _require_concrete_manifest(connector)
        writable_types = _writable_entity_types(manifest)
        if not writable_types:
            pytest.skip("manifest declares no entity type with any writable field")
        for entity_type in writable_types:
            capability = manifest.entity_capability(entity_type)
            assert capability is not None
            field = _writable_fields(capability)[0]

            created = await connector.create(sample_entity(entity_type, variant=0))
            before = await connector.read(created.ref)
            new_value = sample_value(entity_type, field, variant=1)
            diff = _build_diff(
                entity_type,
                capability,
                field,
                new_value,
                expected_revision=_revision_of(before, field, created.source_revision),
            )

            first = await connector.update(created.ref, diff)
            assert first.outcome is WriteOutcome.UPDATED  # sanity: this was a real change

            after_first = await connector.read(created.ref)
            replay_diff = _build_diff(
                entity_type,
                capability,
                field,
                new_value,  # the exact same value, now already current
                expected_revision=_revision_of(after_first, field, first.source_revision),
            )

            with assert_no_http_calls():
                replay = await connector.update(created.ref, replay_diff)

            assert replay.outcome is WriteOutcome.NO_OP, (
                f"re-applying the unchanged value for {entity_type.value}.{field} returned "
                f"{replay.outcome}, not NO_OP"
            )
            assert replay.written_fields == []

            after_replay = await connector.read(created.ref)
            revision_before_replay = _revision_of(after_first, field, None)
            revision_after_replay = _revision_of(after_replay, field, None)
            if revision_before_replay is not None and revision_after_replay is not None:
                assert revision_after_replay == revision_before_replay, (
                    f"{entity_type.value}.{field}'s revision moved on a no-op re-apply "
                    f"({revision_before_replay!r} -> {revision_after_replay!r}) — "
                    "a write reached the target even though nothing changed"
                )

            logged_outcome = _last_logged_write_outcome(connector, "update")
            if logged_outcome is not None:
                assert logged_outcome is WriteOutcome.NO_OP, (
                    "the connector's own call log records the re-applied update as "
                    f"{logged_outcome}, not NO_OP — its return value claimed a no-op but "
                    "its recorded call disagrees"
                )


# ------------------------------------------------------------------------------------
# Internals
# ------------------------------------------------------------------------------------


def _require_concrete_manifest(connector: Connector) -> CapabilityManifest:
    """The connector's manifest, or a ``pytest.skip`` when it is not the concrete
    :class:`CapabilityManifest` type this suite enumerates entities/fields from.

    ``CapabilityManifestBase`` only exposes the three yes/no questions the contract
    itself asks (``supports``/``is_writable``/``requires_full_replace``); there is no
    generic way to enumerate "which entity types, which fields" from it. Every real v1
    connector returns the concrete type (the SDK root re-exports it as exactly what
    ``capabilities()`` returns in practice), so this should never actually skip in
    practice — it exists so a connector with a genuinely custom manifest subclass gets a
    clear, honest skip instead of an ``AttributeError``.
    """
    manifest = connector.capabilities()
    if not isinstance(manifest, CapabilityManifest):
        pytest.skip(
            f"{type(manifest).__qualname__} is a custom CapabilityManifestBase subclass; "
            "this check enumerates entities/fields from the concrete CapabilityManifest "
            "type and cannot introspect an arbitrary one generically"
        )
    return manifest


def _writable_fields(capability: EntityCapability) -> list[str]:
    return sorted(name for name, fc in capability.fields.items() if fc.is_writable)


def _non_writable_fields(capability: EntityCapability) -> list[tuple[str, FieldCapabilityMode]]:
    return sorted(
        (
            (name, field_capability.mode)
            for name, field_capability in capability.fields.items()
            if not field_capability.is_writable
        ),
        key=lambda pair: pair[0],
    )


def _writable_entity_types(manifest: CapabilityManifest) -> list[EntityType]:
    result: list[EntityType] = []
    entity_types = sorted(manifest.supported_entity_types, key=lambda et: et.value)
    for entity_type in entity_types:
        capability = manifest.entity_capability(entity_type)
        if capability is not None and _writable_fields(capability):
            result.append(entity_type)
    return result


def _synthetic_ref(connector: Connector, entity_type: EntityType) -> IdentityRef:
    """A syntactically valid ref to an object that does not exist.

    Safe for the negative capability checks above because the contract's own guards
    (``ensure_supported``/``ensure_writable``) run before any existence lookup — a
    connector that gets this right never even asks "does this exist?" for a write it was
    never going to honor.
    """
    return IdentityRef(
        endpoint=connector.name,
        entity_type=entity_type,
        native_key=_SYNTHETIC_NATIVE_KEY,
        tenant_id=_SYNTHETIC_TENANT_ID,
    )


def _build_diff(
    entity_type: EntityType,
    capability: EntityCapability,
    field: str,
    value: object,
    *,
    expected_revision: str | None,
) -> FieldDiff:
    field_capability = capability.fields.get(field)
    mode = (
        FieldUpdateMode.REPLACE
        if field_capability is not None and field_capability.requires_full_replace
        else FieldUpdateMode.PATCH
    )
    change = FieldChange(field=field, mode=mode, value=to_json_value(value))
    return FieldDiff(entity_type=entity_type, changes=[change], expected_revision=expected_revision)


def _revision_of(entity: object, field: str, default: str | None) -> str | None:
    """The concurrency/revision token :meth:`Connector.read` recorded for ``field``,
    falling back to ``default`` (typically a ``WriteResult.source_revision``) when the
    entity carries no envelope for it.
    """
    field_envelopes = getattr(entity, "field_envelopes", None)
    if isinstance(field_envelopes, dict):
        envelope = field_envelopes.get(field)
        if envelope is not None:
            source_revision = getattr(envelope, "source_revision", None)
            if isinstance(source_revision, str):
                return source_revision
    return default


def _last_logged_write_outcome(connector: Connector, method: str) -> WriteOutcome | None:
    """The outcome the connector's own call log recorded for its most recent call to
    ``method``, or ``None`` when the connector exposes no such log.

    Duck-typed against :class:`~qlabs_catalog_sync_sdk.testing.FakeConnector`'s
    ``calls(method) -> list[CallRecord]`` (each record's ``.result`` is what the call
    returned) rather than importing it — the ``Connector`` contract itself has no call
    log, so this is deliberately best-effort and never required for the suite to pass.
    """
    calls = getattr(connector, "calls", None)
    if not callable(calls):
        return None
    try:
        records = calls(method)
    except TypeError:
        return None
    if not records:
        return None
    result = getattr(records[-1], "result", None)
    outcome = getattr(result, "outcome", None)
    return outcome if isinstance(outcome, WriteOutcome) else None
