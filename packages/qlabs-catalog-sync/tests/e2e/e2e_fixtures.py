"""The two fake vendor tenants the WP8 pilot runs against, and the wiring around them.

WP8 / T8.1. This module holds everything that is *not* an assertion: a fake Unity
Catalog, a fake Qlik Cloud tenant, the ``respx`` routes that put both on the wire, the
engine config file the CLI is pointed at, and the connector registry the CLI builds its
connectors from. The test modules beside it hold only the claims being proved.

Nothing here is captured from a live tenant
-------------------------------------------

RM-01 is built without live tenants (``decision-databricks-to-qlik-mvp.md``: "all tests
run against respx mocks, an SDK-provided fake connector, and hand-authored cassettes").
Every payload below is hand-authored from the research outputs:

* Unity Catalog shapes -- ``catalogs``/``schemas``/``tables`` list envelopes,
  ``schema_id``/``table_id``/``metastore_id``, epoch-millisecond audit stamps,
  ``full_name`` composition, the Statement Execution API's
  ``status.state``/``result.data_array`` envelope -- from
  ``planning/Research/RS-01-databricks-catalog-api/outputs/databricks-catalog-api-reference.md``
  sections 1.2, 1.3, 3.1, 3.6 and 4.1.
* Qlik shapes -- the data-product create response (``id``/``qri``/``activated``/audit
  stamps), the Items API item envelope, the users-API list envelope, ``links.next.href``
  cursor pagination -- from
  ``planning/Research/RS-02-qlik-catalog-api/outputs/qlik-catalog-api-reference.md``
  section 1.1 and ``notes/qlik-two-way-sync-readiness.md`` section 2.

Both fakes are *stateful*, for the same reason the Qlik connector's own conformance
suite gives: the pilot's central claim is that a second cycle over unchanged source data
writes nothing, and the only honest way to check that is for the second cycle to see
exactly what the first one left behind -- both in Qlik and in the engine's state store.

The one substitution this pilot makes, and why
----------------------------------------------

Everything is real: the real ``qlabs-catalog-sync`` CLI, real entry-point discovery
(:func:`~qlabs_catalog_sync.discovery.discover_connectors`), the real Databricks and
Qlik connector classes, real Alembic migrations against a real (temporary) SQLite state
store. Exactly one thing is substituted, and it is a seam the connector itself declares:
:class:`~qlabs_connector_databricks.Connector`'s ``workspace_client_factory``.

``Connector.healthcheck()`` probes Unity Catalog through ``databricks-sdk``'s
``WorkspaceClient``, which talks over ``requests`` -- not ``httpx`` -- so ``respx``
cannot intercept it and the probe would reach the real network. ``auth.py``'s own
docstring names this and offers the factory as the seam ("Tests inject a fake factory
(to exercise ``healthcheck()`` without a live workspace)"). :class:`PilotDatabricksConnector`
takes that seam and nothing else: it is the real connector class with a zero-argument
constructor that supplies the fake factory, so ``cli/wiring.py``'s ``connector_cls()``
call is untouched and every other code path -- OAuth, Unity Catalog REST, the Statement
Execution API, mapping, identity -- is the shipped one, answered by ``respx``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import respx
import yaml

from qlabs_catalog_sync.discovery import ConnectorRegistry, discover_connectors
from qlabs_connector_databricks import Connector as RealDatabricksConnector

__all__ = [
    "CUSTOMERS_VIEW_ID",
    "DATABRICKS_ENDPOINT_KEY",
    "DATABRICKS_HOST",
    "HR_SCHEMA_ID",
    "METASTORE_ID",
    "ORDERS_TABLE_ID",
    "OWNER_EMAIL",
    "OWNER_USER_ID",
    "PAIR_NAME",
    "QLIK_BASE_URL",
    "QLIK_ENDPOINT_KEY",
    "RETAIL_SCHEMA_ID",
    "SPACE_ID",
    "SQL_WAREHOUSE_ID",
    "FakeQlikCloud",
    "FakeUnityCatalog",
    "PilotDatabricksConnector",
    "PilotTenants",
    "calls_so_far",
    "databricks_requests",
    "databricks_unity_catalog_requests",
    "pilot_connector_registry",
    "qlik_mutations",
    "qlik_requests",
    "register_pilot_routes",
    "request_bodies",
    "write_pilot_config",
]

# --------------------------------------------------------------------------------------
# Identifiers. Hand-chosen, opaque, and stable across the whole pilot.
# --------------------------------------------------------------------------------------

DATABRICKS_HOST = "https://acme.cloud.databricks.example"
QLIK_BASE_URL = "https://acme.eu.qlikcloud.example"

DATABRICKS_ENDPOINT_KEY = "dbx_prod"
QLIK_ENDPOINT_KEY = "qlik_acme"
PAIR_NAME = "retail-to-qlik"

#: Unity Catalog identity is ``(metastore_id, schema_id/table_id)`` -- never the
#: renameable ``full_name`` (``read.py``'s "Identity" section, RS-01 section 4.1).
METASTORE_ID = "8f2c1a90-5b3d-4e77-9a10-6c4be2d10f31"
RETAIL_SCHEMA_ID = "3a7d9c14-2f65-4b08-8e51-90ab7c6d4e22"
HR_SCHEMA_ID = "5c19e740-8a3b-4d92-b077-1e6f2a9c8b03"
ORDERS_TABLE_ID = "b41f6d02-7c85-49ae-93d6-2a0e5f8c71b4"
CUSTOMERS_VIEW_ID = "d92a3e57-1b64-4f10-8c27-7e5d0b9a4362"

SQL_WAREHOUSE_ID = "0a1b2c3d4e5f6789"

#: Qlik ids are opaque 24-hex-character strings (RS-02 section 1.1's own examples).
SPACE_ID = "610a2f3b4c5d6e7f8a9b0c1d"
OWNER_USER_ID = "6909d8524392dbbab822c7f7"
OWNER_EMAIL = "data-eng@acme.example"

#: 2026-02-11T08:00:00Z as epoch milliseconds -- UC audit stamps are millisecond
#: integers (RS-01 sections 1.2 / 4.1).
_UC_TS = 1_770_796_800_000
_QLIK_TS = "2026-02-11T08:00:00Z"

_CATALOGS_URL = f"{DATABRICKS_HOST}/api/2.1/unity-catalog/catalogs"
_SCHEMAS_URL = f"{DATABRICKS_HOST}/api/2.1/unity-catalog/schemas"
_TABLES_URL = f"{DATABRICKS_HOST}/api/2.1/unity-catalog/tables"
_STATEMENTS_URL = f"{DATABRICKS_HOST}/api/2.0/sql/statements"
_DBX_TOKEN_URL = f"{DATABRICKS_HOST}/oidc/v1/token"

_QLIK_TOKEN_URL = f"{QLIK_BASE_URL}/oauth/token"
_QLIK_SPACE_URL = f"{QLIK_BASE_URL}/api/v1/spaces/{SPACE_ID}"
#: No ``/v1`` segment: the data-governance family is its own (RS-02 section 3.1).
_DATA_PRODUCTS_URL = f"{QLIK_BASE_URL}/api/data-governance/data-products"
_ITEMS_URL = f"{QLIK_BASE_URL}/api/v1/items"
_USERS_URL = f"{QLIK_BASE_URL}/api/v1/users"

_TABLE_DETAIL_RE = re.compile(rf"^{re.escape(_TABLES_URL)}/(?P<full_name>[^/?]+)$")
_DATA_PRODUCT_RE = re.compile(rf"^{re.escape(_DATA_PRODUCTS_URL)}/(?P<native_id>[^/?]+)$")
#: Every documented data-product lifecycle action (RS-02 notes section 2). D4 and D7 say
#: none of these is ever issued in v1, so they are routed only so that issuing one shows
#: up as a recorded call instead of as an unmatched-route error.
_DATA_PRODUCT_ACTION_RE = re.compile(
    rf"^{re.escape(_DATA_PRODUCTS_URL)}/(?P<native_id>[^/?]+)/actions/(?P<action>[^/?]+)$"
)


# --------------------------------------------------------------------------------------
# Unity Catalog payload builders (RS-01 sections 1.2 / 4.1)
# --------------------------------------------------------------------------------------


def _catalog(name: str, *, comment: str) -> dict[str, Any]:
    return {
        "name": name,
        "metastore_id": METASTORE_ID,
        "catalog_type": "MANAGED_CATALOG",
        "comment": comment,
        "owner": "data-platform@acme.example",
        "created_at": _UC_TS,
        "created_by": "admin@acme.example",
        "updated_at": _UC_TS,
        "updated_by": "admin@acme.example",
    }


def _schema(
    catalog_name: str,
    name: str,
    *,
    schema_id: str,
    comment: str,
    owner: str,
    properties: dict[str, str],
) -> dict[str, Any]:
    return {
        "catalog_name": catalog_name,
        "name": name,
        "full_name": f"{catalog_name}.{name}",
        "comment": comment,
        "owner": owner,
        "properties": properties,
        "schema_id": schema_id,
        "metastore_id": METASTORE_ID,
        "created_at": _UC_TS,
        "created_by": "admin@acme.example",
        "updated_at": _UC_TS,
        "updated_by": owner,
    }


def _table(
    catalog_name: str,
    schema_name: str,
    name: str,
    *,
    table_id: str,
    table_type: str,
    comment: str,
    owner: str,
    columns: list[dict[str, Any]],
    data_source_format: str | None = "DELTA",
    view_definition: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "catalog_name": catalog_name,
        "schema_name": schema_name,
        "name": name,
        "full_name": f"{catalog_name}.{schema_name}.{name}",
        "table_type": table_type,
        "columns": columns,
        "comment": comment,
        "owner": owner,
        "properties": {"delta.minReaderVersion": "1"},
        "table_id": table_id,
        "metastore_id": METASTORE_ID,
        "created_at": _UC_TS,
        "created_by": "admin@acme.example",
        "updated_at": _UC_TS,
        "updated_by": owner,
    }
    if data_source_format is not None:
        payload["data_source_format"] = data_source_format
    if view_definition is not None:
        payload["view_definition"] = view_definition
    return payload


#: The one schema the pilot syncs. Decision D1: this ``catalog.schema`` is the data
#: product, and the two tables below are its datasets.
RETAIL_SCHEMA = _schema(
    "sales",
    "retail",
    schema_id=RETAIL_SCHEMA_ID,
    comment="Curated retail sales tables, refreshed nightly.",
    owner=OWNER_EMAIL,
    properties={"team": "retail-analytics"},
)

#: A schema in a second catalog, deliberately outside the pair's ``sales.*`` selector.
HR_SCHEMA = _schema(
    "hr",
    "people",
    schema_id=HR_SCHEMA_ID,
    comment="Workforce records. Not in scope for the retail sync.",
    owner="people-ops@acme.example",
    properties={"team": "people-ops"},
)

ORDERS_TABLE = _table(
    "sales",
    "retail",
    "orders",
    table_id=ORDERS_TABLE_ID,
    table_type="MANAGED",
    comment="One row per customer order.",
    owner=OWNER_EMAIL,
    columns=[
        {"name": "order_id", "type_name": "LONG", "nullable": False, "position": 0},
        {"name": "customer_id", "type_name": "LONG", "nullable": False, "position": 1},
        {"name": "order_total", "type_name": "DECIMAL", "nullable": True, "position": 2},
    ],
)

CUSTOMERS_VIEW = _table(
    "sales",
    "retail",
    "customers_v",
    table_id=CUSTOMERS_VIEW_ID,
    table_type="VIEW",
    comment="Active customers only.",
    owner=OWNER_EMAIL,
    columns=[
        {"name": "customer_id", "type_name": "LONG", "nullable": False, "position": 0},
        {"name": "email", "type_name": "STRING", "nullable": True, "position": 1},
    ],
    data_source_format=None,
    view_definition="SELECT * FROM sales.retail.customers WHERE active",
)

EMPLOYEES_TABLE = _table(
    "hr",
    "people",
    "employees",
    table_id="7e0d2b93-4a16-4c58-9f31-8b2c5e07a1d6",
    table_type="MANAGED",
    comment="Workforce roster.",
    owner="people-ops@acme.example",
    columns=[{"name": "employee_id", "type_name": "LONG", "nullable": False, "position": 0}],
)

#: ``INFORMATION_SCHEMA.SCHEMA_TAGS`` rows, per catalog (decision D6). Column order is
#: ``(catalog_name, schema_name, tag_name, tag_value)`` -- what ``sql_tags.py`` selects.
_SCHEMA_TAG_ROWS: dict[str, list[list[str]]] = {
    "sales": [
        ["sales", "retail", "domain", "retail"],
        ["sales", "retail", "pii", "false"],
    ],
    "hr": [["hr", "people", "pii", "true"]],
}

#: ``INFORMATION_SCHEMA.TABLE_TAGS`` rows: ``(catalog, schema, table, name, value)``.
_TABLE_TAG_ROWS: dict[str, list[list[str]]] = {
    "sales": [
        ["sales", "retail", "orders", "certified", "gold"],
        ["sales", "retail", "customers_v", "pii", "true"],
    ],
    "hr": [["hr", "people", "employees", "pii", "true"]],
}


# --------------------------------------------------------------------------------------
# The fake Unity Catalog
# --------------------------------------------------------------------------------------


#: Query-parameter keys the *cheap* table listing in ``changes.py`` adds. Honoring them
#: is what makes the checksum snapshot realistic: a real UC response to
#: ``omit_columns=true`` genuinely omits ``columns``, so this fake must too.
_OMIT_FIELDS = {
    "omit_columns": ("columns",),
    "omit_properties": ("properties",),
    "omit_username": ("owner", "created_by", "updated_by"),
}


@dataclass
class FakeUnityCatalog:
    """A Unity Catalog metastore with two catalogs, answering the calls the connector
    actually makes: ``/catalogs``, ``/schemas``, ``/tables``, ``/tables/{full_name}``
    and the Statement Execution API used for tags (decision D6).

    Mutable on purpose. :meth:`set_schema_comment` is how the pilot proves that the
    engine *does* notice a real change, so that "the re-run wrote nothing" is a
    statement about the source being unchanged and not about the engine being inert.
    """

    schemas: list[dict[str, Any]] = field(
        default_factory=lambda: [dict(RETAIL_SCHEMA), dict(HR_SCHEMA)]
    )
    tables: list[dict[str, Any]] = field(
        default_factory=lambda: [
            dict(ORDERS_TABLE),
            dict(CUSTOMERS_VIEW),
            dict(EMPLOYEES_TABLE),
        ]
    )

    # -- mutation seams -----------------------------------------------------------------

    def set_schema_comment(self, full_name: str, comment: str) -> None:
        """Edit a schema's ``comment`` (and bump its audit stamp), as a UC edit would."""
        for schema in self.schemas:
            if schema["full_name"] == full_name:
                schema["comment"] = comment
                schema["updated_at"] = _UC_TS + 60_000
                return
        raise KeyError(full_name)

    # -- handlers -------------------------------------------------------------------------

    def handle_catalogs(self, request: httpx.Request) -> httpx.Response:
        del request
        names = sorted({str(schema["catalog_name"]) for schema in self.schemas})
        return httpx.Response(
            200,
            json={"catalogs": [_catalog(name, comment=f"{name} catalog") for name in names]},
        )

    def handle_schemas(self, request: httpx.Request) -> httpx.Response:
        catalog_name = request.url.params.get("catalog_name")
        matched = [
            dict(schema) for schema in self.schemas if schema["catalog_name"] == catalog_name
        ]
        return httpx.Response(200, json={"schemas": matched})

    def handle_tables(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        catalog_name = params.get("catalog_name")
        schema_name = params.get("schema_name")
        matched = [
            table
            for table in self.tables
            if table["catalog_name"] == catalog_name and table["schema_name"] == schema_name
        ]
        shaped: list[dict[str, Any]] = []
        for table in matched:
            payload = dict(table)
            for param, dropped in _OMIT_FIELDS.items():
                if params.get(param) == "true":
                    for key in dropped:
                        payload.pop(key, None)
            shaped.append(payload)
        return httpx.Response(200, json={"tables": shaped})

    def handle_table_detail(self, request: httpx.Request, **_: str) -> httpx.Response:
        full_name = str(request.url.path).rsplit("/", maxsplit=1)[-1]
        for table in self.tables:
            if table["full_name"] == full_name:
                return httpx.Response(200, json=dict(table))
        return httpx.Response(404, json={"error_code": "TABLE_DOES_NOT_EXIST", "message": ""})

    def handle_statement(self, request: httpx.Request) -> httpx.Response:
        """The Statement Execution API, answering the two ``INFORMATION_SCHEMA`` reads
        ``sql_tags.py`` issues (RS-01 section 3.1 response envelope).

        Answers ``SUCCEEDED`` on the submit itself, which the connector's poll loop
        handles without a second call -- the polling path has its own dedicated unit
        tests in ``packages/qlabs-connector-databricks/tests/sql_tags/``.
        """
        body = json.loads(request.content or b"{}")
        statement = str(body.get("statement", ""))
        catalog = statement.partition("`")[2].partition("`")[0]
        wanted = {
            str(parameter["value"])
            for parameter in body.get("parameters", [])
            if isinstance(parameter, dict) and "value" in parameter
        }
        if "SCHEMA_TAGS" in statement:
            rows = [row for row in _SCHEMA_TAG_ROWS.get(catalog, []) if row[1] in wanted]
        elif "TABLE_TAGS" in statement:
            rows = [row for row in _TABLE_TAG_ROWS.get(catalog, []) if row[1] in wanted]
        else:  # pragma: no cover - the connector issues no other statement
            rows = []
        return httpx.Response(
            200,
            json={
                "statement_id": "01f0-pilot-statement",
                "status": {"state": "SUCCEEDED"},
                "manifest": {"format": "JSON_ARRAY", "total_row_count": len(rows)},
                "result": {"row_count": len(rows), "data_array": rows},
            },
        )


# --------------------------------------------------------------------------------------
# The fake Qlik Cloud tenant
# --------------------------------------------------------------------------------------


@dataclass
class _StoredProduct:
    body: dict[str, Any]
    etag: str


@dataclass
class FakeQlikCloud:
    """One Qlik space, starting **empty** -- the honest state of a first-ever sync.

    Answers the space health check, the users lookup that resolves ``keyContacts``
    (D3), the Items lookup that resolves ``datasetIds`` (D2), and the data-product
    create/read/patch calls. Delete and every lifecycle action are routed too, purely so
    that if the engine ever issued one it would appear as a recorded call rather than as
    an unmatched-route error -- D4 and D7 say it never does, and the pilot asserts that.
    """

    space_id: str = SPACE_ID
    users_by_email: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            OWNER_EMAIL.casefold(): {
                "id": OWNER_USER_ID,
                "email": OWNER_EMAIL,
                "name": "Retail Data Engineering",
                "status": "active",
                "tenantId": "acme-eu",
            }
        }
    )
    datasets_by_name: dict[str, dict[str, Any]] = field(default_factory=dict)
    products: dict[str, _StoredProduct] = field(default_factory=dict)
    _next_product: int = 1
    _revision: int = 0

    # -- introspection ------------------------------------------------------------------

    def product_named(self, name: str) -> dict[str, Any]:
        """The single stored product called ``name``. Raises if there is not exactly one."""
        matches = [
            stored.body for stored in self.products.values() if stored.body.get("name") == name
        ]
        if len(matches) != 1:
            raise AssertionError(
                f"expected exactly one Qlik data product named {name!r}, found {len(matches)}"
            )
        return matches[0]

    # -- handlers -------------------------------------------------------------------------

    def handle_space(self, request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "id": self.space_id,
                "name": "Retail Analytics",
                "type": "managed",
                "tenantId": "acme-eu",
                "createdAt": _QLIK_TS,
            },
        )

    def handle_users_search(self, request: httpx.Request) -> httpx.Response:
        raw_filter = request.url.params.get("filter", "")
        email = raw_filter.partition("'")[2].rpartition("'")[0]
        user = self.users_by_email.get(email.casefold())
        return httpx.Response(200, json={"data": [] if user is None else [user], "links": {}})

    def handle_items_search(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        name = params.get("name")
        if params.get("resourceType") == "dataset" and name in self.datasets_by_name:
            return httpx.Response(
                200, json={"data": [self.datasets_by_name[name]], "links": {}}
            )
        return httpx.Response(200, json={"data": [], "links": {}})

    def handle_data_product_list(self, request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "data": [dict(stored.body) for stored in self.products.values()],
                "links": {},
            },
        )

    def handle_create(self, request: httpx.Request) -> httpx.Response:
        """``POST /api/data-governance/data-products`` -- RS-02 notes section 2.

        The response is the full object: server-assigned ``id``/``qri``/audit stamps,
        ``activated: false``, and empty arrays for every association the body did not
        carry.
        """
        payload = json.loads(request.content or b"{}")
        native_id = f"{self._next_product:024x}"
        self._next_product += 1
        body: dict[str, Any] = {
            "id": native_id,
            "qri": f"qri:data-product://{native_id}",
            "name": payload.get("name", ""),
            "spaceId": payload.get("spaceId", self.space_id),
            "ownerId": OWNER_USER_ID,
            "tenantId": "acme-eu",
            "activated": False,
            "activatedOn": [],
            "datasetIds": payload.get("datasetIds", []),
            "apiConsumableDatasetIds": payload.get("apiConsumableDatasetIds", []),
            "glossaryIds": payload.get("glossaryIds", []),
            "keyContacts": payload.get("keyContacts", []),
            "createdAt": _QLIK_TS,
            "createdBy": OWNER_USER_ID,
            "updatedAt": _QLIK_TS,
            "updatedBy": OWNER_USER_ID,
        }
        for optional in ("description", "readMe", "tags"):
            if optional in payload:
                body[optional] = payload[optional]
        etag = self._mint_etag()
        self.products[native_id] = _StoredProduct(body=body, etag=etag)
        return httpx.Response(201, json=dict(body), headers={"ETag": etag})

    def handle_get(self, request: httpx.Request, **_: str) -> httpx.Response:
        native_id = str(request.url.path).rsplit("/", maxsplit=1)[-1]
        stored = self.products.get(native_id)
        if stored is None:
            return httpx.Response(404, json={"errors": [{"detail": "not found"}]})
        return httpx.Response(200, json=dict(stored.body), headers={"ETag": stored.etag})

    def handle_patch(self, request: httpx.Request, **_: str) -> httpx.Response:
        native_id = str(request.url.path).rsplit("/", maxsplit=1)[-1]
        stored = self.products.get(native_id)
        if stored is None:
            return httpx.Response(404, json={"errors": [{"detail": "not found"}]})
        if_match = request.headers.get("if-match")
        if if_match is not None and if_match != stored.etag:
            return httpx.Response(412, headers={"ETag": stored.etag})
        operations = json.loads(request.content or b"[]")
        for operation in operations:
            if operation.get("op") != "replace":
                return httpx.Response(
                    400, json={"errors": [{"detail": "only 'replace' is supported"}]}
                )
            stored.body[str(operation["path"]).removeprefix("/")] = operation["value"]
        stored.body["updatedAt"] = _QLIK_TS
        stored.etag = self._mint_etag()
        return httpx.Response(204, headers={"ETag": stored.etag})

    def handle_forbidden_mutation(self, request: httpx.Request, **_: str) -> httpx.Response:
        """Answers ``DELETE`` and every ``/actions/*`` call. Reaching this handler is
        itself the failure the pilot checks for (D4, D7); it answers 200 rather than
        raising so the assertion is made on the recorded call, not on an exception the
        engine would have swallowed into a run-report error."""
        del request
        return httpx.Response(200, json={})

    def _mint_etag(self) -> str:
        self._revision += 1
        return f'W/"pilot-rev-{self._revision}"'


# --------------------------------------------------------------------------------------
# respx wiring
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PilotTenants:
    """The two fakes one pilot run talks to."""

    databricks: FakeUnityCatalog
    qlik: FakeQlikCloud


def register_pilot_routes(router: respx.MockRouter, tenants: PilotTenants) -> None:
    """Wire every endpoint either connector can call, on one router.

    Registered against an ``assert_all_mocked=True`` router, so a call to anything not
    listed here fails loudly instead of escaping to the real network -- which is exactly
    the guarantee that makes "these are all the requests that were made" assertable.
    """
    databricks, qlik = tenants.databricks, tenants.qlik

    router.post(_DBX_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "pilot-databricks-token",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )
    )
    router.get(_CATALOGS_URL).mock(side_effect=databricks.handle_catalogs)
    router.get(_SCHEMAS_URL).mock(side_effect=databricks.handle_schemas)
    router.get(_TABLES_URL).mock(side_effect=databricks.handle_tables)
    router.route(method="GET", url__regex=_TABLE_DETAIL_RE).mock(
        side_effect=databricks.handle_table_detail
    )
    router.post(_STATEMENTS_URL).mock(side_effect=databricks.handle_statement)

    router.post(_QLIK_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "pilot-qlik-token",
                "token_type": "bearer",
                "expires_in": 3600,
            },
        )
    )
    router.get(_QLIK_SPACE_URL).mock(side_effect=qlik.handle_space)
    router.get(_USERS_URL).mock(side_effect=qlik.handle_users_search)
    router.get(_ITEMS_URL).mock(side_effect=qlik.handle_items_search)
    router.get(_DATA_PRODUCTS_URL).mock(side_effect=qlik.handle_data_product_list)
    router.post(_DATA_PRODUCTS_URL).mock(side_effect=qlik.handle_create)
    router.route(method="GET", url__regex=_DATA_PRODUCT_RE).mock(side_effect=qlik.handle_get)
    router.route(method="PATCH", url__regex=_DATA_PRODUCT_RE).mock(side_effect=qlik.handle_patch)
    router.route(method="DELETE", url__regex=_DATA_PRODUCT_RE).mock(
        side_effect=qlik.handle_forbidden_mutation
    )
    router.route(method="POST", url__regex=_DATA_PRODUCT_ACTION_RE).mock(
        side_effect=qlik.handle_forbidden_mutation
    )


# --------------------------------------------------------------------------------------
# Reading the recorded traffic back
# --------------------------------------------------------------------------------------


#: The Qlik paths the connector may write through. Every one of these carries a
#: mutation: a data-product create (``POST``), a field update (``PATCH``), a delete
#: (``DELETE``, D4) or a lifecycle action (``POST .../actions/...``, D7).
_QLIK_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _requests(router: respx.MockRouter, since: int) -> Iterator[httpx.Request]:
    for call in list(router.calls)[since:]:
        yield call.request


def calls_so_far(router: respx.MockRouter) -> int:
    """How many requests have been recorded. Pass it back as ``since=`` to scope a
    later assertion to one CLI invocation."""
    return len(router.calls)


def qlik_requests(
    router: respx.MockRouter,
    *,
    method: str | None = None,
    path_prefix: str | None = None,
    since: int = 0,
) -> list[httpx.Request]:
    """Every recorded request to the Qlik tenant, optionally narrowed."""
    matched: list[httpx.Request] = []
    for request in _requests(router, since):
        if not str(request.url).startswith(QLIK_BASE_URL):
            continue
        if method is not None and request.method != method:
            continue
        if path_prefix is not None and not str(request.url.path).startswith(path_prefix):
            continue
        matched.append(request)
    return matched


def qlik_mutations(router: respx.MockRouter, *, since: int = 0) -> list[httpx.Request]:
    """Every recorded request that could have changed anything in the Qlik tenant.

    This -- not the engine's own ``dry_run`` flag, and not a connector's call log -- is
    what the pilot's "applied nothing" and "the re-run wrote nothing" claims are made
    against. The OAuth token exchange is a ``POST`` too and is excluded by path: it
    mutates nothing in the catalog.
    """
    return [
        request
        for request in _requests(router, since)
        if str(request.url).startswith(QLIK_BASE_URL)
        and request.method in _QLIK_MUTATING_METHODS
        and str(request.url.path) != "/oauth/token"
    ]


def databricks_requests(router: respx.MockRouter, *, since: int = 0) -> list[httpx.Request]:
    """Every recorded request to the Databricks workspace, of any kind."""
    return [
        request
        for request in _requests(router, since)
        if str(request.url).startswith(DATABRICKS_HOST)
    ]


def databricks_unity_catalog_requests(
    router: respx.MockRouter, *, since: int = 0
) -> list[httpx.Request]:
    """Every recorded request to a Databricks Unity Catalog REST path.

    Excludes the OAuth token exchange (``/oidc/v1/token``) and the Statement Execution
    API (``/api/2.0/sql/statements``): both are ``POST``\\ s that are nonetheless reads,
    so the caller checks them separately rather than letting the method alone decide.
    """
    return [
        request
        for request in _requests(router, since)
        if str(request.url).startswith(DATABRICKS_HOST)
        and str(request.url.path).startswith("/api/2.1/unity-catalog/")
    ]


def request_bodies(requests: list[httpx.Request]) -> list[Any]:
    """The parsed JSON body of each request, in order."""
    return [json.loads(request.content or b"null") for request in requests]


# --------------------------------------------------------------------------------------
# Connectors and config
# --------------------------------------------------------------------------------------


class _FakeCatalogsAPI:
    """Just enough of ``databricks.sdk.service.catalog.CatalogsAPI`` for
    ``auth.probe_unity_catalog``'s single ``.catalogs.list(max_results=1)``."""

    def list(self, **_: Any) -> Iterator[dict[str, Any]]:
        return iter([{"name": "sales"}])


@dataclass
class _FakeWorkspaceClient:
    """Stands in for ``databricks.sdk.WorkspaceClient``. Construction does no I/O in the
    real client either (``auth.py``'s docstring), so this fake needs no network story."""

    host: str
    token: str
    catalogs: _FakeCatalogsAPI = field(default_factory=_FakeCatalogsAPI)


def _fake_workspace_client_factory(*, host: str, token: str) -> Any:
    return _FakeWorkspaceClient(host=host, token=token)


class PilotDatabricksConnector(RealDatabricksConnector):
    """The real Databricks connector, zero-argument constructible, with the vendor
    ``WorkspaceClient`` swapped for a fake.

    See this module's docstring for why this one substitution is necessary and why it is
    the only one. ``cli/wiring.py`` builds every connector as ``connector_cls()``, so
    this subclass slots into real discovery without the wiring knowing anything.
    """

    def __init__(self) -> None:
        super().__init__(workspace_client_factory=_fake_workspace_client_factory)


def pilot_connector_registry() -> ConnectorRegistry:
    """Real entry-point discovery, with the Databricks class replaced by the subclass above.

    Deliberately built from :func:`~qlabs_catalog_sync.discovery.discover_connectors`
    rather than hand-assembled: the pilot then also proves that both connectors are
    genuinely installed and registered under the ``qlabs_catalog_sync.connectors``
    entry-point group the engine looks them up by.
    """
    discovered = discover_connectors()
    connectors = {name: discovered.get_connector(name) for name in discovered.names()}
    connectors["databricks"] = PilotDatabricksConnector
    return ConnectorRegistry(connectors, discovered.broken())


def write_pilot_config(
    directory: Path,
    *,
    catalog_schema_patterns: tuple[str, ...] = ("sales.*",),
    entity_types: tuple[str, ...] = ("data_product",),
    endpoint_catalog_schema_patterns: tuple[str, ...] | None = None,
    sql_warehouse_id: str | None = SQL_WAREHOUSE_ID,
    activation_opt_in: bool = False,
) -> Path:
    """Write the operator-facing engine config the CLI is pointed at, and return its path.

    Secrets are inlined under ``settings`` rather than referenced through ``secrets``:
    the secret-backend indirection has its own tests in ``tests/config``, and resolving
    it here would only add environment variables to this pilot without exercising
    anything the pilot is about.

    ``endpoint_catalog_schema_patterns`` defaults to the pair's own patterns. It is a
    separate knob because the two are genuinely separate settings -- the endpoint-level
    list bounds what the *connector* will read at all, the pair-level list is decision
    D1's per-pair selector -- and one of this pilot's findings is about what happens when
    they differ.
    """
    endpoint_patterns = (
        catalog_schema_patterns
        if endpoint_catalog_schema_patterns is None
        else endpoint_catalog_schema_patterns
    )
    databricks_settings: dict[str, Any] = {
        "host": DATABRICKS_HOST,
        "client_id": "pilot-service-principal",
        "client_secret": "pilot-service-principal-secret",
        "catalog_schema_patterns": list(endpoint_patterns),
    }
    if sql_warehouse_id is not None:
        databricks_settings["sql_warehouse_id"] = sql_warehouse_id

    config: dict[str, Any] = {
        "endpoints": {
            DATABRICKS_ENDPOINT_KEY: {
                "connector": "databricks",
                "settings": databricks_settings,
            },
            QLIK_ENDPOINT_KEY: {
                "connector": "qlik",
                "settings": {
                    "base_url": QLIK_BASE_URL,
                    "client_id": "pilot-qlik-client",
                    "client_secret": "pilot-qlik-secret",
                    "scope": "user_default",
                    "space_id": SPACE_ID,
                },
            },
        },
        "pairs": [
            {
                "name": PAIR_NAME,
                "source": DATABRICKS_ENDPOINT_KEY,
                "target": QLIK_ENDPOINT_KEY,
                "catalog_schema_patterns": list(catalog_schema_patterns),
                # Must equal the Qlik endpoint's own `space_id`: the pair names where a
                # created product belongs, and the connector writes into its configured
                # space. Nothing validates that the two agree -- see the pilot report.
                "target_space": SPACE_ID,
                "entity_types": list(entity_types),
                "activation_opt_in": activation_opt_in,
            }
        ],
    }
    path = directory / "engine.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path
