"""Shared fixtures and realistic UC payload builders for the ``changes.py`` tests.

Mirrors the SDK's own ``tests/http/conftest.py`` (``make_endpoint`` factory wired to a
fake sleep and a small bounded attempt count, so retry tests stay fast and
deterministic) plus a Databricks-specific piece: builders for catalog/schema/table JSON
shaped exactly like ``databricks-sdk``'s own ``CatalogInfo``/``SchemaInfo``/``TableInfo``
dataclasses (confirmed by reading the installed package — field names, epoch-millisecond
timestamps, ``full_name`` composition), and route-registration helpers for respx that
speak Databricks' ``max_results``/``next_page_token`` pagination shape.

Identity in these payloads matters now, not just their content: ``schema_id``/
``table_id`` are the native key ``changes.py`` diffs on (via ``read.py``'s identity
builders — see ``changes.py``'s module docstring), so every distinct object in a test
needs a distinct stable id, the same way it would in a real workspace. The builders
default each id to a deterministic, ``full_name``-derived value
(:func:`default_schema_id`/:func:`default_table_id`) so ordinary tests never collide by
accident; a rename test passes the *same* explicit id across two calls with a different
``name`` to prove a rename is one change, not a delete-plus-create.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import httpx
import pytest
import respx

from qlabs_catalog_sync_sdk.http import HttpEndpoint

BASE_URL = "https://acme.cloud.databricks.com"
ENDPOINT = "databricks"

#: The default ``metastore_id`` every builder below stamps onto its payload, and
#: therefore the ``IdentityRef.tenant_id`` every object built from one carries (see
#: ``read.py``'s ``build_schema_identity_ref``/``build_table_identity_ref``).
METASTORE_ID = "ms-1"

CATALOGS_PATH = "/api/2.1/unity-catalog/catalogs"
SCHEMAS_PATH = "/api/2.1/unity-catalog/schemas"
TABLES_PATH = "/api/2.1/unity-catalog/tables"

DEFAULT_TS = 1_700_000_000_000  # 2023-11-14T22:13:20Z, epoch milliseconds


# ------------------------------------------------------------------------------------
# HttpEndpoint factory — no real sleeping, no real network
# ------------------------------------------------------------------------------------


class RecordingSleep:
    """A fake async sleep: records the requested duration instead of waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def recording_sleep() -> RecordingSleep:
    return RecordingSleep()


@pytest.fixture
async def make_http(recording_sleep: RecordingSleep) -> AsyncIterator[Callable[..., HttpEndpoint]]:
    """Factory for an :class:`HttpEndpoint` pointed at :data:`BASE_URL`.

    Small bounded attempts and a fake sleep so a "retries exhausted" test makes few
    calls and no test ever really waits. Endpoints built through the factory are closed
    automatically at teardown.
    """
    made: list[HttpEndpoint] = []

    def _make(**overrides: Any) -> HttpEndpoint:
        kwargs: dict[str, Any] = {
            "sleep": recording_sleep,
            "max_attempts": 3,
            "backoff_base_seconds": 0.01,
            "backoff_max_seconds": 0.02,
        }
        kwargs.update(overrides)
        endpoint = HttpEndpoint(BASE_URL, **kwargs)
        made.append(endpoint)
        return endpoint

    yield _make
    for endpoint in made:
        await endpoint.aclose()


@pytest.fixture
def http(make_http: Callable[..., HttpEndpoint]) -> HttpEndpoint:
    """The common case: a default-configured endpoint, no auth needed for these tests
    (list_changed does not touch auth; that is T4.1's surface)."""
    return make_http()


# ------------------------------------------------------------------------------------
# Realistic UC payload builders
# ------------------------------------------------------------------------------------


def default_schema_id(catalog_name: str, name: str) -> str:
    """The stable id a test uses for a schema by default: deterministic from
    ``catalog.schema`` so distinct schemas across a test never collide by accident, but
    distinct from the schema's own ``full_name`` string (a real ``schema_id`` is an
    opaque id, never the qualified name)."""
    return f"sch-id::{catalog_name}.{name}"


def default_table_id(catalog_name: str, schema_name: str, name: str) -> str:
    """The stable id a test uses for a table by default — see :func:`default_schema_id`."""
    return f"tbl-id::{catalog_name}.{schema_name}.{name}"


def catalog(
    name: str,
    *,
    comment: str = "",
    owner: str = "data-eng-sp",
    updated_at: int = DEFAULT_TS,
) -> dict[str, Any]:
    """Shaped like ``databricks.sdk.service.catalog.CatalogInfo.as_dict()``."""
    return {
        "name": name,
        "full_name": name,
        "comment": comment,
        "owner": owner,
        "metastore_id": METASTORE_ID,
        "created_at": updated_at,
        "created_by": owner,
        "updated_at": updated_at,
        "updated_by": owner,
    }


def schema(
    catalog_name: str,
    name: str,
    *,
    comment: str = "",
    owner: str = "data-eng-sp",
    properties: dict[str, str] | None = None,
    schema_id: str | None = None,
    metastore_id: str = METASTORE_ID,
    updated_at: int = DEFAULT_TS,
) -> dict[str, Any]:
    """Shaped like ``databricks.sdk.service.catalog.SchemaInfo.as_dict()``.

    ``schema_id`` defaults to :func:`default_schema_id` — pass it explicitly (holding it
    fixed while ``name``/``comment``/etc. change between two calls) to simulate a rename
    or any other in-place edit of the *same* schema across polls.
    """
    return {
        "catalog_name": catalog_name,
        "name": name,
        "full_name": f"{catalog_name}.{name}",
        "comment": comment,
        "owner": owner,
        "properties": properties or {},
        "schema_id": schema_id or default_schema_id(catalog_name, name),
        "metastore_id": metastore_id,
        "created_at": updated_at,
        "created_by": owner,
        "updated_at": updated_at,
        "updated_by": owner,
    }


def table(
    catalog_name: str,
    schema_name: str,
    name: str,
    *,
    comment: str = "",
    owner: str = "data-eng-sp",
    table_type: str = "MANAGED",
    table_id: str | None = None,
    metastore_id: str = METASTORE_ID,
    columns: list[dict[str, Any]] | None = None,
    updated_at: int = DEFAULT_TS,
) -> dict[str, Any]:
    """Shaped like ``databricks.sdk.service.catalog.TableInfo.as_dict()``.

    ``table_id`` defaults to :func:`default_table_id` — see :func:`schema` for why a
    rename test passes it explicitly instead.
    """
    return {
        "catalog_name": catalog_name,
        "schema_name": schema_name,
        "name": name,
        "full_name": f"{catalog_name}.{schema_name}.{name}",
        "comment": comment,
        "owner": owner,
        "table_type": table_type,
        "data_source_format": "DELTA",
        "table_id": table_id or default_table_id(catalog_name, schema_name, name),
        "columns": columns
        or [{"name": "id", "type_name": "LONG", "nullable": False, "position": 0}],
        "properties": {},
        "metastore_id": metastore_id,
        "created_at": updated_at,
        "created_by": owner,
        "updated_at": updated_at,
        "updated_by": owner,
    }


# ------------------------------------------------------------------------------------
# respx route helpers speaking max_results / next_page_token
# ------------------------------------------------------------------------------------


def _paged_responses(
    items_key: str, pages: Sequence[Sequence[dict[str, Any]]]
) -> list[httpx.Response]:
    responses: list[httpx.Response] = []
    for index, page_items in enumerate(pages):
        body: dict[str, Any] = {items_key: list(page_items)}
        if index < len(pages) - 1:
            body["next_page_token"] = f"page-{index + 2}"
        responses.append(httpx.Response(200, json=body))
    return responses


def mock_list(
    respx_mock: respx.MockRouter,
    path: str,
    *,
    params: dict[str, Any],
    items_key: str,
    pages: Sequence[Sequence[dict[str, Any]]],
) -> respx.Route:
    """Register a route serving ``pages`` in order, one per request, Databricks-style:
    every page but the last carries a ``next_page_token``."""
    return respx_mock.get(f"{BASE_URL}{path}", params=params).mock(
        side_effect=_paged_responses(items_key, pages)
    )


def mock_single_page(
    respx_mock: respx.MockRouter,
    path: str,
    *,
    params: dict[str, Any],
    items_key: str,
    items: Sequence[dict[str, Any]],
) -> respx.Route:
    """Register a route serving exactly one page, no ``next_page_token``.

    Uses a static ``return_value`` (not a one-shot ``side_effect`` queue) so the same
    route keeps answering identically across multiple ``list_changed`` calls in one
    test — required by the idempotency tests, which poll the same mocked state twice.
    """
    return respx_mock.get(f"{BASE_URL}{path}", params=params).mock(
        return_value=httpx.Response(200, json={items_key: list(items)})
    )


def mock_infinite_list(
    respx_mock: respx.MockRouter,
    path: str,
    *,
    params: dict[str, Any],
    items_key: str,
    item: dict[str, Any],
) -> respx.Route:
    """A listing that never stops paginating: every response carries a fresh
    ``next_page_token``, proving the connector's own page cap — not the mock — is what
    stops the loop."""
    return respx_mock.get(f"{BASE_URL}{path}", params=params).mock(
        return_value=httpx.Response(200, json={items_key: [item], "next_page_token": "always-more"})
    )
