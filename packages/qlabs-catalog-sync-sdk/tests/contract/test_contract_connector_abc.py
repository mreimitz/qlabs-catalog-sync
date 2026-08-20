"""The Connector ABC: a stub subclass is real, awaitable, and honest about capabilities."""

from __future__ import annotations

import inspect

import pytest

from qlabs_catalog_sync_sdk.contract import (
    SDK_CONTRACT_VERSION,
    CapabilityError,
    CapabilityManifestBase,
    Connector,
    ConnectorContext,
    ConnectorError,
    EntityType,
    HealthStatus,
    IdentityRef,
    ListChangedResult,
    Watermark,
    WriteOutcome,
)
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    Dataset,
    FieldChange,
    FieldDiff,
    FieldUpdateMode,
    NeutralEntity,
    TextField,
)

ASYNC_METHODS = (
    "setup",
    "healthcheck",
    "list_changed",
    "read",
    "create",
    "update",
    "delete",
    "close",
)


# --------------------------------------------------------------------------------------
# Shape of the contract
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ASYNC_METHODS)
def test_every_io_method_is_async(method: str) -> None:
    """Decision D8: the connector contract is async."""
    assert inspect.iscoroutinefunction(getattr(Connector, method))


def test_capabilities_is_synchronous() -> None:
    """A static declaration with no I/O; the engine caches it once at startup."""
    assert not inspect.iscoroutinefunction(Connector.capabilities)


def test_only_the_read_path_and_lifecycle_are_abstract() -> None:
    """Write methods are concrete so a read-only source connector inherits a refusal."""
    assert Connector.__abstractmethods__ == frozenset(
        {"capabilities", "setup", "healthcheck", "list_changed", "read"}
    )


def test_the_base_stamps_the_contract_version() -> None:
    assert Connector.sdk_contract_version == SDK_CONTRACT_VERSION


def test_the_abc_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        Connector()  # type: ignore[abstract]


# --------------------------------------------------------------------------------------
# Instantiating subclasses
# --------------------------------------------------------------------------------------


def test_a_stub_subclass_is_instantiable(source_connector) -> None:
    assert isinstance(source_connector, Connector)
    assert source_connector.name == "stub_source"
    assert source_connector.sdk_contract_version == SDK_CONTRACT_VERSION


def test_an_incomplete_subclass_cannot_be_instantiated() -> None:
    class Incomplete(Connector):
        name = "incomplete"

        async def setup(self, ctx) -> None:
            return None

    with pytest.raises(TypeError) as excinfo:
        Incomplete()  # type: ignore[abstract]

    message = str(excinfo.value)
    for missing in ("capabilities", "healthcheck", "list_changed", "read"):
        assert missing in message


def test_a_subclass_without_a_name_cannot_be_instantiated(source_connector) -> None:
    class Nameless(type(source_connector)):  # type: ignore[misc]
        name = ""

    with pytest.raises(TypeError, match="entry-point name"):
        Nameless()


# --------------------------------------------------------------------------------------
# Awaiting the read path
# --------------------------------------------------------------------------------------


async def test_setup_receives_the_sdk_connector_context(source_connector, context) -> None:
    assert isinstance(context, ConnectorContext)
    assert context.endpoint == source_connector.name
    assert context.logger is not None

    await source_connector.setup(context)

    assert source_connector.context is context
    assert source_connector.context.config.base_url == "https://example.invalid"


async def test_healthcheck_returns_a_health_status(source_connector) -> None:
    status = await source_connector.healthcheck()

    assert isinstance(status, HealthStatus)
    assert status.is_healthy


async def test_list_changed_returns_changes_and_the_next_watermark(source_connector) -> None:
    start = Watermark.initial(source_connector.name, EntityType.DATA_PRODUCT)

    first = await source_connector.list_changed(EntityType.DATA_PRODUCT, start)

    assert isinstance(first, ListChangedResult)
    assert first.next_watermark.is_after(start)
    assert first.has_more
    assert not first.is_exhausted

    second = await source_connector.list_changed(EntityType.DATA_PRODUCT, first.next_watermark)

    assert second.is_empty
    assert second.is_exhausted
    assert second.next_watermark.is_after(first.next_watermark)


async def test_read_returns_a_neutral_entity(source_connector, source_ref: IdentityRef) -> None:
    """A connector may narrow the return type; `DataProduct` is a `NeutralEntity`."""
    entity = await source_connector.read(source_ref)

    assert isinstance(entity, NeutralEntity)
    assert isinstance(entity, DataProduct)
    assert entity.identity_for("stub_source") == source_ref


async def test_close_defaults_to_a_no_op() -> None:
    class Minimal(Connector):
        name = "minimal"

        def capabilities(self) -> CapabilityManifestBase:  # pragma: no cover - not exercised
            raise AssertionError

        async def setup(self, ctx) -> None:
            return None

        async def healthcheck(self) -> HealthStatus:  # pragma: no cover - not exercised
            raise AssertionError

        async def list_changed(self, entity_type, since):  # pragma: no cover - not exercised
            raise AssertionError

        async def read(self, ref):  # pragma: no cover - not exercised
            raise AssertionError

    assert await Minimal().close() is None


# --------------------------------------------------------------------------------------
# Capability honesty
# --------------------------------------------------------------------------------------


async def test_a_read_only_connector_refuses_writes_without_calling_the_api(
    source_connector, source_ref: IdentityRef
) -> None:
    """The Databricks/Collibra/Snowflake shape: no write path implemented at all."""
    diff = FieldDiff(entity_type=EntityType.DATA_PRODUCT)

    with pytest.raises(CapabilityError) as created:
        await source_connector.create(DataProduct(name="Retail Sales"))
    with pytest.raises(CapabilityError) as updated:
        await source_connector.update(source_ref, diff)
    with pytest.raises(CapabilityError) as deleted:
        await source_connector.delete(source_ref)

    assert source_connector.api_calls == []
    for excinfo, operation in ((created, "create"), (updated, "update"), (deleted, "delete")):
        error = excinfo.value
        assert error.operation == operation
        assert error.endpoint == "stub_source"
        assert error.entity_type is EntityType.DATA_PRODUCT


def test_capability_errors_are_sdk_typed_exceptions() -> None:
    assert issubclass(CapabilityError, ConnectorError)
    assert issubclass(ConnectorError, Exception)


async def test_a_write_connector_writes_a_declared_field(target_connector, target_ref) -> None:
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[
            FieldChange(
                field="description",
                mode=FieldUpdateMode.PATCH,
                value=TextField.plain("New text").model_dump(mode="json"),
            )
        ],
        expected_revision="etag-1",
    )

    result = await target_connector.update(target_ref, diff)

    assert result.outcome is WriteOutcome.UPDATED
    assert result.written_fields == ["description"]
    assert result.source_revision == "etag-2"
    assert target_connector.api_calls == ["update"]


async def test_writing_a_field_the_manifest_does_not_declare_raises_before_the_api(
    target_connector, target_ref
) -> None:
    """RS-08 section 9 capability honesty, enforced by the contract's own guard."""
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="owners", value=[])],
    )

    with pytest.raises(CapabilityError) as excinfo:
        await target_connector.update(target_ref, diff)

    assert excinfo.value.field == "owners"
    assert excinfo.value.entity_type is EntityType.DATA_PRODUCT
    assert target_connector.api_calls == []


async def test_writing_an_unsupported_entity_type_raises_before_the_api(target_connector) -> None:
    with pytest.raises(CapabilityError) as excinfo:
        await target_connector.create(Dataset(name="orders"))

    assert excinfo.value.operation == "create"
    assert excinfo.value.entity_type is EntityType.DATASET
    assert target_connector.api_calls == []


async def test_an_unchanged_diff_is_a_no_op_not_a_write(target_connector, target_ref) -> None:
    """Re-applying nothing must be distinguishable from actually writing."""
    result = await target_connector.update(
        target_ref, FieldDiff(entity_type=EntityType.DATA_PRODUCT)
    )

    assert result.outcome is WriteOutcome.NO_OP
    assert not result.changed
    assert target_connector.api_calls == []


def test_ensure_supported_accepts_a_declared_entity_type(target_connector) -> None:
    assert target_connector.ensure_supported(EntityType.DATA_PRODUCT) is None

    with pytest.raises(CapabilityError):
        target_connector.ensure_supported(EntityType.GLOSSARY_TERM, operation="read")
