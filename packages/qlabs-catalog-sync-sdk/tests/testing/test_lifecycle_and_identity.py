"""Identity (distinct endpoint keys for two connectors used side by side), the
setup/healthcheck/close lifecycle, and seeding as test arrangement rather than logged
connector behavior.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.config import ConnectorContext
from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.manifest import CapabilityManifest
from qlabs_catalog_sync_sdk.models import DataProduct
from qlabs_catalog_sync_sdk.testing import FakeConnector, FakeConnectorConfig


def test_default_name_is_fake() -> None:
    connector = FakeConnector(manifest=CapabilityManifest())
    assert connector.name == "fake"


def test_named_gives_two_instances_distinct_endpoint_keys() -> None:
    manifest = CapabilityManifest()
    a = FakeConnector.named("databricks", manifest=manifest)
    b = FakeConnector.named("qlik", manifest=manifest)

    assert a.name == "databricks"
    assert b.name == "qlik"
    assert isinstance(a, Connector)
    assert isinstance(b, Connector)


def test_read_only_source_and_write_target_use_their_own_default_names(
    source: FakeConnector, target: FakeConnector
) -> None:
    assert source.name == "fake-source"
    assert target.name == "fake-target"
    assert source.name != target.name


async def test_setup_records_the_context_and_the_call(target: FakeConnector) -> None:
    ctx: ConnectorContext[FakeConnectorConfig] = ConnectorContext.build(
        config=FakeConnectorConfig(), endpoint=target.name
    )

    await target.setup(ctx)

    assert target.context is ctx
    assert target.call_count("setup") == 1


async def test_close_marks_closed_and_defaults_to_a_no_op_return(target: FakeConnector) -> None:
    assert target.closed is False

    result = await target.close()

    assert result is None
    assert target.closed is True
    assert target.call_count("close") == 1


async def test_healthcheck_reports_healthy_by_default(target: FakeConnector) -> None:
    status = await target.healthcheck()
    assert status.is_healthy
    assert status.endpoint == target.name


def test_seed_is_not_recorded_on_the_call_log(source: FakeConnector) -> None:
    source.seed(DataProduct(name="Retail Sales"))

    assert source.call_log == []


def test_seed_returns_a_ref_that_read_can_use(source: FakeConnector) -> None:
    ref = source.seed(DataProduct(name="Retail Sales"))

    assert ref.endpoint == source.name
    assert ref.native_key


def test_seed_with_an_explicit_native_key(source: FakeConnector) -> None:
    ref = source.seed(DataProduct(name="Retail Sales"), native_key="main.retail")

    assert ref.native_key == "main.retail"
