"""Shared fixtures for T3.8's conformance suite: the SDK's
``ConnectorConformanceSuite`` run against a real Qlik ``Connector`` in write mode, plus
the connector-specific supplementary tests this task adds beside it
(``test_capability_honesty.py``, ``test_concurrency.py``, ``test_read_cassettes.py``).

Self-contained within ``tests/conformance`` — T3.8 owns exactly this directory and
``tests/cassettes/`` and nothing else in this package, matching every other per-task
conftest here (``tests/write/conftest.py``, ``tests/read/conftest.py``, ...): its own
minimal ``QlikConfig``/``ConnectorContext`` builder and a small in-memory fake Qlik
tenant that answers respx, so this suite never touches a real tenant or another task's
owned test module.

**Why a stateful fake tenant, not one-off canned responses.** The conformance kit's
round-trip and idempotency checks (``qlabs_catalog_sync_sdk/conformance/suite.py``) work
by creating an object, reading it back, updating it, and reading it back again — the only
way to certify that honestly is to have ``read()`` actually reflect what ``create()``/
``update()`` actually sent, not a canned response that happens to look right. So
:class:`FakeQlikTenant` is a tiny durable store keyed by the data-product id ``POST``
mints: ``PATCH`` mutates it in place (JSON Patch ``replace`` only, matching the real
endpoint's own closed semantics — RS-02 ``qlik-two-way-sync-readiness.md`` section 2),
``GET`` returns current state, and every mutation advances the ``ETag`` so the concurrency
tests (``assert_if_match_sent``, a 412 conflict) exercise a real, not simulated, revision
sequence. This is what lets the suite's checks tell the difference between "the connector
lied about what it wrote" and "the connector wrote it and the round trip genuinely holds."

**No live tenant.** Every payload shape below — the data-product create/read/PATCH
bodies, the Items API item envelope, the users-API list envelope — is hand-derived from
RS-02 (``planning/Research/RS-02-qlik-catalog-api/notes/qlik-two-way-sync-readiness.md``
section 2 and ``outputs/qlik-catalog-api-reference.md`` section 1.1), never captured from
a real Qlik Cloud tenant (``decision-databricks-to-qlik-mvp.md`` D8 / the agent guide's
"no live tenants" rule). The hand-authored ``tests/cassettes/*.yaml`` files this same
directory's ``test_read_cassettes.py`` plays back are built from the exact same sources —
see that module's docstring.

One deliberate simplification, stated once here: RS-02's own worked example notes that a
*real* Qlik tenant's create response comes back with ``datasetIds``/``keyContacts``/etc.
still empty even when the ``POST`` body carried values for them (section 2 — association
resolution is apparently not synchronous with the create call on a live tenant). This
fake tenant does not model that asynchrony: it stores exactly what was sent and reflects
it back immediately, matching the simpler assumption ``tests/write/conftest.py`` (T3.4/
T3.5's own suite) already makes. That choice does not change any of this task's findings
about ``dataset_refs``/``owners``/``status`` — see the report — since those are driven by
what ``read.py`` maps into the neutral model, not by tenant timing.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import vcr

from qlabs_catalog_sync_sdk.config import ConnectorContext
from qlabs_catalog_sync_sdk.conformance.harness import vcr_config
from qlabs_connector_qlik import Connector
from qlabs_connector_qlik.config import QlikConfig

ENDPOINT = "qlik"
TENANT_ID = "acme-conformance-tenant"
SPACE_ID = "610a2f3b4c5d6e7f8a9b0c1d"
OWNER_ID = "6909d8524392dbbab822c7f7"

TENANT_BASE_URL = "https://acme-conformance.eu.qlikcloud.example"
TOKEN_URL = f"{TENANT_BASE_URL}/oauth/token"
SPACE_URL = f"{TENANT_BASE_URL}/api/v1/spaces/{SPACE_ID}"
#: No ``/v1`` segment — the data-governance family (RS-02 section 3.1/write.py's own
#: module docstring).
DATA_PRODUCTS_URL = f"{TENANT_BASE_URL}/api/data-governance/data-products"
ITEMS_URL = f"{TENANT_BASE_URL}/api/v1/items"
USERS_URL = f"{TENANT_BASE_URL}/api/v1/users"

#: Where the hand-authored cassettes live — the sibling directory T3.8 also owns.
CASSETTE_DIR = Path(__file__).resolve().parent.parent / "cassettes"

_NOW = "2026-08-20T09:00:00Z"

_DATA_PRODUCT_URL_RE = re.compile(rf"^{re.escape(DATA_PRODUCTS_URL)}/(?P<native_id>[^/]+)$")
_ITEM_URL_RE = re.compile(rf"^{re.escape(ITEMS_URL)}/(?P<item_id>[^/]+)$")


# --------------------------------------------------------------------------------------
# QlikConfig / ConnectorContext builders
# --------------------------------------------------------------------------------------


def build_config(**overrides: Any) -> QlikConfig:
    """A minimally valid :class:`QlikConfig`, with any field overridden."""
    values: dict[str, Any] = {
        "base_url": TENANT_BASE_URL,
        "client_id": "client-conformance",
        "client_secret": "secret-conformance",
        "scope": "user_default",
        "space_id": SPACE_ID,
    }
    values.update(overrides)
    return QlikConfig(**values)


def build_ctx(config: QlikConfig | None = None) -> ConnectorContext[QlikConfig]:
    return ConnectorContext.build(
        config=config or build_config(), endpoint=ENDPOINT, tenant=TENANT_ID
    )


# --------------------------------------------------------------------------------------
# FakeQlikTenant — a tiny, honest, in-memory Qlik tenant
# --------------------------------------------------------------------------------------


@dataclass
class _StoredProduct:
    body: dict[str, Any]
    etag: str


@dataclass
class FakeQlikTenant:
    """The state one configured Qlik space holds, and the respx handlers that answer
    for it. See the module docstring for why this is stateful rather than canned.
    """

    space_id: str = SPACE_ID
    tenant_id: str = TENANT_ID
    owner_id: str = OWNER_ID
    #: Pre-seeded datasets a member-dataset name lookup (tier 2, D2) can resolve
    #: against, keyed by name — empty by default: a brand-new synced product's members
    #: do not yet exist by that name in the target space, which is the honest starting
    #: state for a first sync cycle. Tests that want a hit call :meth:`seed_dataset`.
    _datasets_by_name: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Item-detail lookups by Items-API item id (``GET /api/v1/items/{itemId}``).
    _items_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Pre-seeded users an owner-email lookup (D3) can resolve against, keyed by
    #: case-folded email — empty by default for the same reason as datasets.
    _users_by_email: dict[str, dict[str, Any]] = field(default_factory=dict)
    _products: dict[str, _StoredProduct] = field(default_factory=dict)
    _next_id: int = 1
    _revision: int = 0

    # -- seeding (used by supplementary tests that need a resolvable reference) ------

    def seed_dataset(self, *, name: str, resource_id: str, item_id: str | None = None) -> None:
        resolved_item_id = item_id if item_id is not None else f"item-{resource_id}"
        self._datasets_by_name[name] = {
            "id": resolved_item_id,
            "resourceId": resource_id,
            "resourceType": "dataset",
            "name": name,
        }

    def seed_user(self, *, email: str, user_id: str) -> None:
        self._users_by_email[email.casefold()] = {"id": user_id, "email": email}

    # -- id/etag minting ----------------------------------------------------------------

    def _mint_id(self) -> str:
        native_id = f"conformance-dp-{self._next_id:03d}"
        self._next_id += 1
        return native_id

    def _mint_etag(self) -> str:
        self._revision += 1
        return f'W/"rev-{self._revision}"'

    # -- data-products: POST / GET / PATCH / DELETE -------------------------------------

    def handle_create(self, request: httpx.Request) -> httpx.Response:
        payload = _json_body(request)
        native_id = self._mint_id()
        body: dict[str, Any] = {
            "id": native_id,
            "qri": f"qri:data-product://{native_id}",
            "mainId": f"{native_id}-main",
            "name": payload.get("name", ""),
            "spaceId": payload.get("spaceId", self.space_id),
            "ownerId": self.owner_id,
            "tenantId": self.tenant_id,
            "activated": False,
            "activatedOn": [],
            "datasetIds": payload.get("datasetIds", []),
            "apiConsumableDatasetIds": payload.get("apiConsumableDatasetIds", []),
            "glossaryIds": payload.get("glossaryIds", []),
            "keyContacts": payload.get("keyContacts", []),
            "pendingChangesCount": 0,
            "quality": {"validity": 0, "completeness": 0},
            "createdAt": _NOW,
            "createdBy": self.owner_id,
            "updatedAt": _NOW,
            "updatedBy": self.owner_id,
        }
        for key in ("description", "readMe", "tags"):
            if key in payload:
                body[key] = payload[key]
        etag = self._mint_etag()
        self._products[native_id] = _StoredProduct(body=body, etag=etag)
        return httpx.Response(201, json=dict(body), headers={"ETag": etag})

    def handle_get(self, request: httpx.Request, native_id: str) -> httpx.Response:
        del request
        stored = self._products.get(native_id)
        if stored is None:
            return httpx.Response(404, json={"errors": [{"detail": "not found"}]})
        return httpx.Response(200, json=dict(stored.body), headers={"ETag": stored.etag})

    def handle_patch(self, request: httpx.Request, native_id: str) -> httpx.Response:
        stored = self._products.get(native_id)
        if stored is None:
            return httpx.Response(404, json={"errors": [{"detail": "not found"}]})
        if_match = request.headers.get("if-match")
        if if_match is not None and if_match != stored.etag:
            # HTTP 412 — the revision this request guarded against is stale. Carry the
            # *current* ETag back, exactly like write.py's 412 recovery path needs.
            return httpx.Response(412, headers={"ETag": stored.etag})
        operations = _json_body(request)
        if not isinstance(operations, list):
            return httpx.Response(400, json={"errors": [{"detail": "expected a JSON Patch array"}]})
        for operation in operations:
            if operation.get("op") != "replace":
                return httpx.Response(
                    400, json={"errors": [{"detail": "only 'replace' is supported"}]}
                )
            path = str(operation["path"]).removeprefix("/")
            stored.body[path] = operation["value"]
        stored.body["updatedAt"] = _NOW
        stored.body["updatedBy"] = self.owner_id
        new_etag = self._mint_etag()
        stored.etag = new_etag
        return httpx.Response(204, headers={"ETag": new_etag})

    def handle_delete(self, request: httpx.Request, native_id: str) -> httpx.Response:
        del request
        # Registered for completeness (assert_all_mocked=True needs a route for every
        # method this connector could in principle send) — decision D4 means the
        # connector never actually issues this call in this suite; see
        # test_capability_honesty.py's delete-refusal test.
        self._products.pop(native_id, None)
        return httpx.Response(204)

    # -- Items API: dataset name search + item detail ------------------------------------

    def handle_items_search(self, request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("name")
        resource_type = request.url.params.get("resourceType")
        if resource_type == "dataset" and name and name in self._datasets_by_name:
            item = self._datasets_by_name[name]
            self._items_by_id[str(item["id"])] = _full_item(item, space_id=self.space_id)
            return httpx.Response(200, json={"data": [item], "links": {}})
        return httpx.Response(200, json={"data": [], "links": {}})

    def handle_item_detail(self, request: httpx.Request, item_id: str) -> httpx.Response:
        del request
        item = self._items_by_id.get(item_id)
        if item is None:
            return httpx.Response(404, json={"errors": [{"detail": "not found"}]})
        return httpx.Response(200, json=item, headers={"ETag": 'W/"item-rev-1"'})

    # -- Users API: email search ---------------------------------------------------------

    def handle_users_search(self, request: httpx.Request) -> httpx.Response:
        raw_filter = request.url.params.get("filter", "")
        email = raw_filter.partition("'")[2].rpartition("'")[0]
        user = self._users_by_email.get(email.casefold())
        data = [] if user is None else [user]
        return httpx.Response(200, json={"data": data, "links": {}})


def _full_item(item: Mapping[str, Any], *, space_id: str) -> dict[str, Any]:
    """A single-item ``GET /api/v1/items/{itemId}`` body for a seeded dataset — the
    envelope shape RS-02 section 1.1 documents, including ``resourceAttributes.secureQri``
    (the durable dataset key ``read.py`` looks for)."""
    resource_id = item["resourceId"]
    return {
        "id": item["id"],
        "resourceId": resource_id,
        "resourceType": "dataset",
        "resourceSubType": "qix-df",
        "name": item["name"],
        "spaceId": space_id,
        "ownerId": OWNER_ID,
        "meta": {"tags": []},
        "createdAt": _NOW,
        "updatedAt": _NOW,
        "resourceAttributes": {
            "secureQri": f"qdf-secure:{TENANT_ID}:{space_id}:{resource_id}",
            "qri": f"qdf:{TENANT_ID}:{space_id}:{resource_id}",
        },
    }


def _json_body(request: httpx.Request) -> Any:
    return json.loads(request.content or b"{}")


# --------------------------------------------------------------------------------------
# respx wiring
# --------------------------------------------------------------------------------------


def register_routes(router: respx.MockRouter, tenant: FakeQlikTenant) -> None:
    """Every endpoint this connector can call, wired to ``tenant``.

    Registered once per test against an ``assert_all_mocked=True`` router (below), so an
    HTTP call this connector was not supposed to make (an unmatched method/host, or a
    stray call from code this suite did not expect to run) fails loudly as "no matching
    route" rather than silently reaching the real network.
    """
    router.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "conformance-kit-token",
                "token_type": "bearer",
                "expires_in": 3600,
            },
        )
    )
    router.get(SPACE_URL).mock(
        return_value=httpx.Response(200, json={"id": SPACE_ID, "name": "Conformance Space"})
    )

    router.post(DATA_PRODUCTS_URL).mock(side_effect=tenant.handle_create)
    router.route(method="GET", url__regex=_DATA_PRODUCT_URL_RE).mock(side_effect=tenant.handle_get)
    router.route(method="PATCH", url__regex=_DATA_PRODUCT_URL_RE).mock(
        side_effect=tenant.handle_patch
    )
    router.route(method="DELETE", url__regex=_DATA_PRODUCT_URL_RE).mock(
        side_effect=tenant.handle_delete
    )

    router.get(ITEMS_URL).mock(side_effect=tenant.handle_items_search)
    router.route(method="GET", url__regex=_ITEM_URL_RE).mock(side_effect=tenant.handle_item_detail)

    router.get(USERS_URL).mock(side_effect=tenant.handle_users_search)


@contextlib.asynccontextmanager
async def setup_connector(
    *,
    tenant: FakeQlikTenant | None = None,
    dataset_identity_lookup: Any = None,
    dataset_name_lookup: Any = None,
    enabled_destructive_actions: frozenset[Any] = frozenset(),
    config: QlikConfig | None = None,
) -> AsyncIterator[Connector]:
    """Build, ``setup()`` and yield a real :class:`Connector` against a fresh
    :class:`FakeQlikTenant` — the exact shape every real connector's conformance fixture
    takes (SDK ``conformance/suite.py``'s own docstring: "build it, setup() it, yield it,
    close() it").

    The respx router stays active for the connector's entire lifetime (unlike T4.6's
    Databricks fixture, which only needs respx during ``setup()``): Qlik's
    ``Connector.setup()`` does no I/O at all (the OAuth2 token is fetched lazily on the
    first real request — ``__init__.py``'s own docstring), so every one of this suite's
    checks — ``healthcheck()``, ``create()``, ``read()``, ``update()`` — needs the mock
    active for as long as the connector is in use.

    **A respx caveat this fixture's design has to account for.** The SDK harness's own
    ``assert_no_http_calls``/``capture_requests`` each open a second, nested
    ``respx.mock()`` router inside a test that uses this fixture. Nesting itself works —
    but *not* the way it looks like it should: respx dispatches a request by trying every
    currently-registered router **in registration order** (``respx.mocks.Mocker.handler``)
    and stops at the first one with a matching route, falling through to the next
    registered router only when the current one has no match at all. The *outermost*
    (first-registered, i.e. this fixture's) router therefore always wins for any URL it
    already has a route for — an inner ``assert_no_http_calls()``/``capture_requests()``
    catch-all registered *after* it is only ever reached for a URL this fixture's router
    does not recognize. In practice this only weakens one check in this whole suite: the
    "no HTTP request was sent for the replay" half of
    ``test_reapplying_an_unchanged_diff_is_a_no_op`` does not reliably observe a real
    call here (the outer tenant router answers it first), so that half of the check does
    not meaningfully execute under this fixture — the test still correctly fails on this
    connector, but via the ``WriteOutcome`` comparison that runs right after it, not via
    the call-count assertion. It does **not** weaken any capability-honesty check: every
    one of those raises :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError` from
    a guard clause that runs before ``self._http.request(...)`` is ever called (verified
    by reading ``write.py``/``lifecycle.py`` directly, not inferred from respx's
    silence), so zero httpx calls are made on *any* router, nesting quirk or not. This
    task's own supplementary tests that need a reliable "was a real request sent"
    signal — ``test_concurrency.py``'s ``assert_if_match_sent`` checks and
    ``test_capability_honesty.py``'s zero-call proofs — deliberately avoid this fixture
    and build a connector with a single respx router instead (see ``build_connector``
    below), so nothing in this suite relies on the nesting behavior working the way it
    first appears to.

    ``dataset_identity_lookup``/``dataset_name_lookup`` are left at the connector's own
    defaults (always-miss) unless a caller overrides them — that default is the
    *realistic* one: a brand-new synced entity has no IdentityMap binding yet, exactly
    like a real first sync cycle. ``enabled_destructive_actions`` defaults to empty,
    matching decision D4/D7 — see ``test_capability_honesty.py``.
    """
    fake_tenant = tenant if tenant is not None else FakeQlikTenant()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        register_routes(router, fake_tenant)
        connector = Connector()
        if dataset_identity_lookup is not None:
            connector.dataset_identity_lookup = dataset_identity_lookup
        if dataset_name_lookup is not None:
            connector.dataset_name_lookup = dataset_name_lookup
        connector.enabled_destructive_actions = enabled_destructive_actions
        await connector.setup(build_ctx(config))
        try:
            yield connector
        finally:
            await connector.close()


# --------------------------------------------------------------------------------------
# build_connector — a single-router connector for tests that need a reliable respx call
# count (concurrency / capability-honesty). See setup_connector's docstring for why this
# is deliberately a *different* fixture rather than a mode of that one.
# --------------------------------------------------------------------------------------


async def build_connector(
    *,
    dataset_identity_lookup: Any = None,
    dataset_name_lookup: Any = None,
    enabled_destructive_actions: frozenset[Any] = frozenset(),
    config: QlikConfig | None = None,
) -> Connector:
    """Build and ``setup()`` a real :class:`Connector` with **no** ambient router of its
    own — the caller's test provides the sole active respx router (typically the
    ``pytest-respx`` plugin's ``respx_mock`` fixture, exactly like
    ``tests/auth/conftest.py``'s ``connector`` fixture and ``tests/write/conftest.py``'s
    ``make_writer``). Callers own registering whatever routes their one test needs and
    closing the connector when done (``await connector.close()``).

    Use this — not :func:`setup_connector` — whenever a test needs
    ``qlabs_catalog_sync_sdk.conformance.harness.capture_requests``/
    ``assert_no_http_calls``/``assert_if_match_sent`` to reliably see (or reliably prove
    the absence of) a real request: with only one router ever registered, there is no
    "which router answers first" ambiguity for respx to resolve.
    """
    connector = Connector()
    if dataset_identity_lookup is not None:
        connector.dataset_identity_lookup = dataset_identity_lookup
    if dataset_name_lookup is not None:
        connector.dataset_name_lookup = dataset_name_lookup
    connector.enabled_destructive_actions = enabled_destructive_actions
    await connector.setup(build_ctx(config))
    return connector


def mock_token(router: respx.MockRouter, *, access_token: str = "conformance-kit-token") -> None:
    """Mock the OAuth2 token exchange every real request lazily triggers first."""
    router.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": access_token, "token_type": "bearer", "expires_in": 3600}
        )
    )


# --------------------------------------------------------------------------------------
# vcrpy: hand-authored cassette playback (test_read_cassettes.py)
# --------------------------------------------------------------------------------------


@pytest.fixture
def qlik_vcr() -> vcr.VCR:
    """The SDK's own pre-configured ``vcr.VCR`` (``qlabs_catalog_sync_sdk.conformance
    .harness.vcr_config``), pointed at this task's owned ``tests/cassettes/`` directory.
    ``record_mode="once"`` (the harness helper's default) means: play back the
    hand-authored cassette that is already on disk, never silently re-hit a network."""
    return vcr_config(CASSETTE_DIR)
