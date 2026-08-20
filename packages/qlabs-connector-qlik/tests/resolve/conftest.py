"""Shared fixtures for the Qlik connector's reference-resolution tests (``resolve.py``,
T3.9).

Same shape as ``tests/read/conftest.py``: a plain ``HttpEndpoint`` with a static bearer
token, since ``resolve.py`` never builds its own ``HttpEndpoint`` — the orchestrator
wires it to the connector's already-authenticated ``self.http``, exactly like
``read.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable

import pytest

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_qlik.resolve import DatasetIdentityLookup

TENANT_BASE_URL = "https://acme.eu.qlikcloud.example"
ENDPOINT = "qlik"
TENANT_ID = "acme"
SPACE_ID = "space-123"
OTHER_SPACE_ID = "space-456"

ITEMS_URL = f"{TENANT_BASE_URL}/api/v1/items"
USERS_URL = f"{TENANT_BASE_URL}/api/v1/users"


@pytest.fixture
async def http() -> AsyncIterator[HttpEndpoint]:
    endpoint = HttpEndpoint(TENANT_BASE_URL, auth=("Bearer", "test-token"))
    yield endpoint
    await endpoint.aclose()


def always_miss() -> DatasetIdentityLookup:
    """A tier-1 identity lookup that never has an answer — every member falls through
    to the tier-2 name match."""

    async def _lookup(neutral_id: uuid.UUID) -> str | None:
        del neutral_id
        return None

    return _lookup


def fixed_lookup(mapping: dict[uuid.UUID, str]) -> DatasetIdentityLookup:
    """A tier-1 identity lookup backed by an in-memory mapping, for asserting that a
    hit never falls through to an HTTP call."""

    async def _lookup(neutral_id: uuid.UUID) -> str | None:
        return mapping.get(neutral_id)

    return _lookup


@pytest.fixture
def make_lookup() -> Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup]:
    def _factory(mapping: dict[uuid.UUID, str] | None = None) -> DatasetIdentityLookup:
        return fixed_lookup(mapping) if mapping else always_miss()

    return _factory
