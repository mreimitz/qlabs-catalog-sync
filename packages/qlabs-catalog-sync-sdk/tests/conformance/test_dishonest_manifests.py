"""The second half of T1.8's definition of done: the kit must *fail* a dishonest
connector, not just go green against an honest one.

Each test below calls a suite check method directly (bypassing pytest fixture
injection — these are plain coroutine methods, so calling them with an explicit
``connector=`` argument works exactly like any other method call) against one of the
three liars in ``_liars.py``, and asserts the kit raises rather than passing.
"""

from __future__ import annotations

import httpx
import pytest

from qlabs_catalog_sync_sdk.config import ConnectorContext
from qlabs_catalog_sync_sdk.conformance import ConnectorConformanceSuite, capture_requests
from qlabs_catalog_sync_sdk.conformance.harness import assert_if_match_sent
from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import EntityType, FieldChange, FieldDiff, IdentityRef
from qlabs_catalog_sync_sdk.testing import FakeConnectorConfig

from ._liars import (
    ClaimsEntitySupportButRefusesToWrite,
    ClaimsEtagButNeverSendsIfMatch,
    ClaimsWritableButDropsTheWrite,
)


async def test_kit_catches_a_field_declared_writable_that_is_never_actually_written() -> None:
    """Lie: ``DATA_PRODUCT.name`` is declared ``rw``; ``update`` silently drops it."""
    connector = ClaimsWritableButDropsTheWrite()
    await connector.setup(
        ConnectorContext.build(config=FakeConnectorConfig(), endpoint=connector.name)
    )
    suite = ConnectorConformanceSuite()

    with pytest.raises(AssertionError, match="was not updated on read"):
        await suite.test_update_of_a_writable_field_is_reflected_on_read(connector=connector)


async def test_kit_catches_an_entity_declared_supported_that_writes_actually_refuse() -> None:
    """Lie: ``CATEGORY`` is declared ``supported=True`` with a writable field, but
    ``create`` (never overridden) refuses every time — the manifest and the write path
    disagree about whether this connector can create a category at all."""
    connector = ClaimsEntitySupportButRefusesToWrite()
    await connector.setup(
        ConnectorContext.build(config=FakeConnectorConfig(), endpoint=connector.name)
    )
    suite = ConnectorConformanceSuite()

    with pytest.raises(CapabilityError):
        await suite.test_create_then_read_round_trips_writable_fields(connector=connector)


async def test_kit_catches_a_connector_that_declares_etag_but_never_sends_if_match() -> None:
    """Lie: ``concurrency=etag``, but ``update`` never forwards the revision as
    ``If-Match`` — caught by the reusable HTTP harness a connector author points at their
    own write call, not by the base suite (which does not know this connector's wire
    shape)."""
    async with HttpEndpoint("https://lying.example.test") as http:
        connector = ClaimsEtagButNeverSendsIfMatch(http)
        await connector.setup(
            ConnectorContext.build(config=FakeConnectorConfig(), endpoint=connector.name)
        )
        ref = IdentityRef(
            endpoint=connector.name,
            entity_type=EntityType.DATA_PRODUCT,
            native_key="1",
            tenant_id="lying-tenant",
        )
        diff = FieldDiff(
            entity_type=EntityType.DATA_PRODUCT,
            changes=[FieldChange(field="name", value="new name")],
            expected_revision="etag-before",
        )

        with capture_requests(response=httpx.Response(200, json={"ok": True})) as route:
            await connector.update(ref, diff)
        assert route.call_count == 1  # the write really did happen...

        with pytest.raises(AssertionError, match="If-Match"):
            assert_if_match_sent(route.calls, required=True)  # ...just not honestly.
