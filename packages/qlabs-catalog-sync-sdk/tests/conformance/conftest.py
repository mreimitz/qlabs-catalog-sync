"""Shared helper for the conformance kit's own test suite: wiring a
:class:`~qlabs_catalog_sync_sdk.config.ConnectorContext` for :meth:`Connector.setup`.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.config import ConnectorContext
from qlabs_catalog_sync_sdk.contract import Connector


async def setup_connector(connector: Connector, *, config: object) -> None:
    """Build a minimal :class:`ConnectorContext` and call ``connector.setup`` with it.

    Every ``connector`` fixture in this test package does exactly this one line, so it
    is factored out rather than repeated in every fixture.
    """
    await connector.setup(ConnectorContext.build(config=config, endpoint=connector.name))
