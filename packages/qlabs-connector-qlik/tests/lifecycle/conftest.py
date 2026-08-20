"""Shared fixtures for the Qlik connector's lifecycle-actions tests (``lifecycle.py``, T3.7).

Same shape as ``tests/write/conftest.py``: a plain ``HttpEndpoint`` with a static bearer
token and instant, deterministic retry backoff, because ``lifecycle.py`` never builds its
own ``HttpEndpoint`` — the orchestrator wires it to the connector's already-authenticated
``self.http``. Nothing here is imported from ``tests/write/`` (a different task's owned
test directory); the handful of helpers this module needs are duplicated locally, same as
``lifecycle.py`` duplicates ``_secondary_keys`` rather than reaching across the ownership
line for it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, IdentityRef
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_qlik.lifecycle import (
    DestructiveAction,
    LifecycleActions,
    build_lifecycle_actions,
)

TENANT_BASE_URL = "https://acme.eu.qlikcloud.example"
ENDPOINT = "qlik"
TENANT_ID = "acme"
SPACE_ID = "a1b2c3d4e5f6g7h8i9j0k1l2"
MANAGED_SPACE_ID = "m1a2n3a4g5e6d7s8p9a0c1e2"

DATA_PRODUCTS_URL = f"{TENANT_BASE_URL}/api/data-governance/data-products"

#: A data product native id used across the lifecycle tests — arbitrary but stable.
PRODUCT_ID = "6672d8b7a182224cbb3f1c26"
PRODUCT_QRI = f"qri:data-product://{PRODUCT_ID}"


async def _instant(seconds: float) -> None:
    """A ``sleep`` that does not: retry tests must not really wait out the backoff."""
    del seconds


@pytest.fixture
def make_http() -> Callable[..., HttpEndpoint]:
    """Factory for an ``HttpEndpoint`` with instant, deterministic retry backoff."""

    def _make(**kwargs: Any) -> HttpEndpoint:
        kwargs.setdefault("max_attempts", 3)
        return HttpEndpoint(
            TENANT_BASE_URL, auth=("Bearer", "test-token"), sleep=_instant, **kwargs
        )

    return _make


@pytest.fixture
async def http(make_http: Callable[..., HttpEndpoint]) -> AsyncIterator[HttpEndpoint]:
    endpoint = make_http()
    yield endpoint
    await endpoint.aclose()


@pytest.fixture
def make_lifecycle(http: HttpEndpoint) -> Callable[..., LifecycleActions]:
    """Factory for a real :class:`LifecycleActions` over the shared ``http`` fixture.

    ``enabled_actions`` defaults to empty (nothing opted in) so every test has to name,
    explicitly, which destructive action(s) it means to exercise — the same discipline
    the module itself enforces.
    """

    def _make(
        *,
        enabled_actions: frozenset[DestructiveAction] = frozenset(),
        endpoint: HttpEndpoint | None = None,
    ) -> LifecycleActions:
        return build_lifecycle_actions(
            endpoint if endpoint is not None else http,
            endpoint=ENDPOINT,
            enabled_actions=enabled_actions,
        )

    return _make


def product_ref(native_id: str = PRODUCT_ID, **overrides: Any) -> IdentityRef:
    """A :class:`IdentityRef` for a Qlik data product, shaped exactly like ``read.py``/
    ``write.py`` build one: ``native_key`` is the id, ``secondary_keys['id']`` rides
    along, ``qri`` when present."""
    values: dict[str, Any] = {
        "endpoint": ENDPOINT,
        "entity_type": EntityType.DATA_PRODUCT,
        "native_key": native_id,
        "tenant_id": TENANT_ID,
        "secondary_keys": {"id": native_id, "qri": f"qri:data-product://{native_id}"},
    }
    values.update(overrides)
    return IdentityRef(**values)


def dataset_ref(native_id: str = "some-secure-qri") -> IdentityRef:
    """A non-data-product ref, for the "wrong entity type" guard tests."""
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key=native_id,
        tenant_id=TENANT_ID,
        secondary_keys={"id": "item-1"},
    )


def activate_response(**overrides: Any) -> dict[str, Any]:
    """The fields RS-02's readiness notes confirm present on an activate response:
    ``activated``/``activatedAt``/``activatedOn`` plus a trust score."""
    body: dict[str, Any] = {
        "id": PRODUCT_ID,
        "qri": PRODUCT_QRI,
        "activated": True,
        "activatedAt": "2026-08-20T09:00:00Z",
        "activatedOn": [MANAGED_SPACE_ID],
        "trustScore": {"value": 82},
        "pendingChangesCount": 0,
    }
    body.update(overrides)
    return body


def sent_body(respx_mock: Any) -> dict[str, Any]:
    """The JSON body of the last request ``respx`` captured."""
    return dict(_json_of(respx_mock.calls.last.request))


def _json_of(request: httpx.Request) -> Any:
    return json.loads(request.content or b"{}")
