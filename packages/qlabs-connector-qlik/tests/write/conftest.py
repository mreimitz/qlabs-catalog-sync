"""Shared fixtures for the Qlik connector's write-path tests (``write.py``, T3.4).

Same shape as ``tests/read/conftest.py`` and ``tests/resolve/conftest.py``: a plain
``HttpEndpoint`` with a static bearer token, because ``write.py`` never builds its own —
the orchestrator wires it to the connector's already-authenticated ``self.http``.

Nothing here stubs ``write.py``'s collaborators: every writer is built through the real
:func:`~qlabs_connector_qlik.write.build_writer`, so the real
:class:`~qlabs_connector_qlik.resolve.QlikReferenceResolver` and the real
:func:`~qlabs_connector_qlik.manifest.qlik_capability_manifest` are in the loop and the
only thing mocked is HTTP (``respx``). The Qlik payloads below are the ones from RS-02
(``qlik-two-way-sync-readiness.md`` section 2 and
``qlik-catalog-api-reference.md`` section 3.5), not invented shapes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import httpx
import pytest

from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import DataProduct, Party, PartyRole, Tag, TextField
from qlabs_connector_qlik.write import DatasetNameLookup, QlikWriter, build_writer

TENANT_BASE_URL = "https://acme.eu.qlikcloud.example"
ENDPOINT = "qlik"
TENANT_ID = "acme"
SPACE_ID = "a1b2c3d4e5f6g7h8i9j0k1l2"

DATA_PRODUCTS_URL = f"{TENANT_BASE_URL}/api/data-governance/data-products"
ITEMS_URL = f"{TENANT_BASE_URL}/api/v1/items"
USERS_URL = f"{TENANT_BASE_URL}/api/v1/users"

#: The identity fields RS-02 documents on a data-product create response.
CREATED_ID = "6672d8b7a182224cbb3f1c26"
CREATED_QRI = f"qri:data-product://{CREATED_ID}"
CREATED_MAIN_ID = "6672d8b7a182224cbb3f1c27"


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


def identity_lookup(
    mapping: Mapping[uuid.UUID, str] | None = None,
) -> Callable[[uuid.UUID], Any]:
    """A tier-1 IdentityMap lookup backed by an in-memory mapping (``{}`` = always miss)."""
    resolved = dict(mapping or {})

    async def _lookup(neutral_id: uuid.UUID) -> str | None:
        return resolved.get(neutral_id)

    return _lookup


def name_lookup(mapping: Mapping[uuid.UUID, str] | None = None) -> DatasetNameLookup:
    """A :data:`~qlabs_connector_qlik.write.DatasetNameLookup` backed by a mapping."""
    names = dict(mapping or {})

    async def _lookup(neutral_id: uuid.UUID) -> str | None:
        return names.get(neutral_id)

    return _lookup


@pytest.fixture
def make_writer(http: HttpEndpoint) -> Callable[..., QlikWriter]:
    """Factory for a real :class:`QlikWriter` over the shared ``http`` fixture.

    ``identity_map`` seeds tier-1 dataset resolution, ``dataset_names`` seeds the tier-2
    name seam, and ``manifest`` overrides the (real, by default) capability manifest.
    """

    def _make(
        *,
        identity_map: Mapping[uuid.UUID, str] | None = None,
        dataset_names: Mapping[uuid.UUID, str] | None = None,
        manifest: Any = None,
        endpoint: HttpEndpoint | None = None,
    ) -> QlikWriter:
        return build_writer(
            endpoint if endpoint is not None else http,
            endpoint=ENDPOINT,
            tenant_id=TENANT_ID,
            space_id=SPACE_ID,
            dataset_identity_lookup=identity_lookup(identity_map),
            dataset_name_lookup=name_lookup(dataset_names),
            manifest=manifest,
        )

    return _make


def created_response(**overrides: Any) -> dict[str, Any]:
    """The full create response RS-02 documents — identity, audit stamps, ``activated:
    false``, and empty association arrays."""
    body: dict[str, Any] = {
        "id": CREATED_ID,
        "qri": CREATED_QRI,
        "mainId": CREATED_MAIN_ID,
        "name": "Sales Analytics Data Product",
        "description": "Curated sales datasets",
        "readMe": "# Sales\nCurated sales datasets.",
        "spaceId": SPACE_ID,
        "tags": ["sales", "revenue"],
        "ownerId": "6909d8524392dbbab822c7f7",
        "tenantId": TENANT_ID,
        "activated": False,
        "activatedOn": [],
        "datasetIds": [],
        "apiConsumableDatasetIds": [],
        "glossaryIds": [],
        "keyContacts": [],
        "pendingChangesCount": 0,
        "quality": {"validity": 0, "completeness": 0},
        "createdAt": "2026-08-20T09:00:00Z",
        "createdBy": "6909d8524392dbbab822c7f7",
        "updatedAt": "2026-08-20T09:00:00Z",
        "updatedBy": "6909d8524392dbbab822c7f7",
    }
    body.update(overrides)
    return body


def mock_create(respx_mock: Any, **overrides: Any) -> Any:
    """Mock a successful ``POST /api/data-governance/data-products``."""
    headers = overrides.pop("headers", None)
    status_code = overrides.pop("status_code", 201)
    response = httpx.Response(status_code, json=created_response(**overrides), headers=headers)
    return respx_mock.post(DATA_PRODUCTS_URL).mock(return_value=response)


def mock_users(respx_mock: Any, users: Mapping[str, str]) -> Any:
    """Mock ``GET /api/v1/users?filter=email eq '<email>'`` for a known email -> id map."""

    def handler(request: httpx.Request) -> httpx.Response:
        raw_filter = request.url.params.get("filter", "")
        email = raw_filter.partition("'")[2].rpartition("'")[0]
        user_id = users.get(email)
        data = [] if user_id is None else [{"id": user_id, "email": email}]
        return httpx.Response(200, json={"data": data, "links": {}})

    return respx_mock.get(USERS_URL).mock(side_effect=handler)


def mock_datasets_by_name(respx_mock: Any, datasets: Mapping[str, str]) -> Any:
    """Mock the Items API name lookup ``resolve.py`` uses for tier-2 dataset matching."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("name", "")
        dataset_id = datasets.get(name)
        data = (
            []
            if dataset_id is None
            else [{"id": f"item-{dataset_id}", "resourceId": dataset_id, "name": name}]
        )
        return httpx.Response(200, json={"data": data, "links": {}})

    return respx_mock.get(ITEMS_URL).mock(side_effect=handler)


def sales_product(**overrides: Any) -> DataProduct:
    """The worked example from RS-02, as a neutral entity: name, description, readMe,
    tags, and one owner. Member datasets are left to each test."""
    values: dict[str, Any] = {
        "name": "Sales Analytics Data Product",
        "description": TextField.plain("Curated sales datasets"),
        "documentation": TextField.markdown("# Sales\nCurated sales datasets."),
        "tags": [Tag(key="sales"), Tag(key="revenue")],
    }
    values.update(overrides)
    return DataProduct(**values)


def owner(
    email: str,
    *,
    role: PartyRole = PartyRole.OWNER,
    display_name: str | None = None,
) -> Party:
    return Party(email=email, role=role, display_name=display_name)


def refs(count: int) -> list[uuid.UUID]:
    """``count`` distinct neutral dataset ids, deterministic per position."""
    return [uuid.UUID(int=index + 1) for index in range(count)]


def sent_body(respx_mock: Any) -> dict[str, Any]:
    """The JSON body of the last request ``respx`` captured."""
    return dict(_json_of(respx_mock.calls.last.request))


def sent_bodies(respx_mock: Any) -> list[dict[str, Any]]:
    """The JSON bodies of every request ``respx`` captured, in order."""
    return [dict(_json_of(call.request)) for call in respx_mock.calls]


def _json_of(request: httpx.Request) -> Any:
    return json.loads(request.content or b"{}")
