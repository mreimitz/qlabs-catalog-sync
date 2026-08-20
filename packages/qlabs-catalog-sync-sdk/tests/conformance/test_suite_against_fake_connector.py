"""The conformance kit's own certification run — the T1.8 definition of done.

:class:`~qlabs_catalog_sync_sdk.conformance.ConnectorConformanceSuite` runs green
against :class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` in both of its canned
roles: a Databricks-shaped read-only source (every write refuses honestly; nothing
round-trips because nothing is writable) and a Qlik-shaped write target (round-trip,
update, idempotency and capability honesty all exercised for real, against genuine
checksum/watermark semantics — see ``testing/fake_connector.py``'s own docstring for why
that double is trustworthy enough to certify against).

This is also the reference example for T3.8 (Qlik) and T4.6 (Databricks): the shape of
each ``connector`` fixture below — build it, ``setup()`` it, yield it, ``close()`` it —
is exactly what a real connector's fixture looks like, just pointed at a mock/sandbox
endpoint instead of an in-memory store.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from qlabs_catalog_sync_sdk.conformance import ConnectorConformanceSuite
from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.testing import FakeConnector, FakeConnectorConfig

from .conftest import setup_connector


class TestFakeReadOnlySourceConformance(ConnectorConformanceSuite):
    """The Databricks-shaped role: read-only, so only the honesty checks apply."""

    @pytest.fixture
    async def connector(self) -> AsyncIterator[Connector]:
        source = FakeConnector.read_only_source()
        await setup_connector(source, config=FakeConnectorConfig())
        yield source
        await source.close()


class TestFakeWriteTargetConformance(ConnectorConformanceSuite):
    """The Qlik-shaped role: the sole v1 write target, fully exercised."""

    @pytest.fixture
    async def connector(self) -> AsyncIterator[Connector]:
        target = FakeConnector.write_target()
        await setup_connector(target, config=FakeConnectorConfig())
        yield target
        await target.close()
