"""Shared fixtures for the Qlik connector's read-path tests (``read.py``, T3.3).

Unlike the auth/setup tests, these drive ``read.py``'s free functions directly against
a plain ``HttpEndpoint`` with a static bearer token — there is no OAuth2 token exchange
to mock, since ``read.py`` never builds its own ``HttpEndpoint`` (T3.1's
``build_http_endpoint`` already covers that; the orchestrator wires ``read.py``'s
functions to the connector's already-authenticated ``self.http``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from qlabs_catalog_sync_sdk.http import HttpEndpoint

TENANT_BASE_URL = "https://acme.eu.qlikcloud.example"
ENDPOINT = "qlik"
TENANT_ID = "acme"
SPACE_ID = "space-123"


@pytest.fixture
async def http() -> AsyncIterator[HttpEndpoint]:
    endpoint = HttpEndpoint(TENANT_BASE_URL, auth=("Bearer", "test-token"))
    yield endpoint
    await endpoint.aclose()
