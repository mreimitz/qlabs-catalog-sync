"""Databricks ``list_changed``: UC schema/table listing with a checksum-snapshot fallback.

WP4 / T4.3. Implements decision D1 (a Unity Catalog *schema* is the data product; its
tables and views are its datasets) against ``/api/2.1/unity-catalog/schemas`` and
``/api/2.1/unity-catalog/tables``, and decision D8 (``list_changed`` returns the
candidate changes *and* the connector's proposed next watermark, so the engine commits
both in one transaction).

Design
------

**There is no server-side "modified since" filter.** UC's list endpoints accept only
``catalog_name``/``schema_name`` scoping and pagination — confirmed by reading the
installed ``databricks-sdk``'s ``SchemasAPI.list``/``TablesAPI.list`` signatures, which
carry no timestamp parameter. Every call to this module therefore does a **complete**
traversal of what the service principal can see (catalogs -> schemas -> tables) and
decides "changed" by comparing what it just observed against what it last observed —
not by asking the API to filter for it.

**The comparison is exact-equality over a canonical checksum, not a timestamp
threshold.** Each schema/table's UC REST payload is hashed with the SDK's
``compute_checksum`` (canonical JSON, SHA-256 — see ``envelope.py``). A native key is a
candidate change when its fresh checksum differs from the last one this connector saw
for that key, full stop. ``updated_at`` is *not* used as a separate, first gate ahead of
the checksum: it is simply one of the fields inside the payload being hashed, so
whenever UC bumps it, the checksum changes too — the "primary signal" is absorbed into
the same mechanism that also catches everything it misses. Concretely, this covers every
gap named in the task:

* **A tag change applied through SQL does not move ``updated_at``** on the catalog/
  schema/table object at all (tags live in ``INFORMATION_SCHEMA.*_TAGS``, a separate
  governance surface — RS-01 section 1.3). This connector does not read tags here (see
  "Deliberately out of scope" below), so a tag-only edit is a real, documented gap in
  this fallback, not one it silently claims to close.
* **A child change that does not touch the parent's timestamp** — adding, dropping or
  renaming a table under a schema does not bump that *schema's* ``updated_at``, yet the
  data product's dataset membership (decision D1) has changed. This is why a schema's
  checksum input is not just its own REST payload: it also includes the sorted list of
  full names of the tables currently under it (a second, cheap listing per schema with
  ``omit_columns``/``omit_properties``/``omit_username`` to keep it light). Any table
  added, removed or renamed under a schema therefore changes that schema's checksum even
  though nothing on the schema object itself moved.
* **Clock skew between the caller and the workspace** cannot cause a missed change,
  because nothing is ever compared against *our* clock. There is no "now" and no
  inequality anywhere in the decision — only exact equality between two checksums UC's
  own responses produced. This is stronger than a numeric safety margin: a margin only
  bounds the *probability* of a boundary miss, an exact-equality comparison has no
  boundary to miss at.

**The watermark carries the snapshot.** Connectors hold no sync state (RS-08 section 5)
— the *only* state a connector may carry across polls is whatever the engine persists
and hands back inside the ``Watermark`` it returned last time. So the "snapshot" here is
not a private cache; it is a :attr:`~qlabs_catalog_sync_sdk.contract.WatermarkKind.CURSOR`
watermark whose opaque ``cursor`` is
``{"v": 1, "checksums": {"<full_name>": "<sha256:...>", ...}}`` — meaningful only to this
module, exactly what ``WatermarkKind.CURSOR`` is documented for. Consequences:

* An ``initial`` watermark has no snapshot, so every object observed is a candidate —
  the required "from an initial watermark, everything is returned" behavior falls out
  without a special case.
* Re-running with the returned ``next_watermark`` against unchanged data recomputes
  every checksum, finds each one equal to what the snapshot already recorded, and
  returns no candidates — the DoD, proved as a test in ``tests/changes``.
* A native key present in the previous snapshot but absent from the fresh traversal is
  reported as :attr:`~qlabs_catalog_sync_sdk.contract.ChangeKind.DELETED` (decision D4:
  v1 never deletes in Qlik, so the engine reports it as an orphan) — a byproduct of the
  same diff, at no extra API cost.
* ``has_more`` is always ``False`` on a normal return: a call is a complete traversal,
  not one page of an ongoing scan, so there genuinely is no next page. It only would
  differ if a defensive cap below were changed from "raise" to "truncate", which it
  deliberately is not (see below).

**Paging defensively.** Each individual UC list call (catalogs, schemas-of-a-catalog,
tables-of-a-schema) is walked with its own hard cap on page count
(``max_pages_per_listing``), *not* a cap on item count: Databricks documents that "a page
may contain zero results while still providing a ``next_page_token``", so an item-count
cap would never trip against a pathological all-empty-pages loop. Hitting the cap raises
:class:`~qlabs_catalog_sync_sdk.exceptions.TransientError` rather than silently returning
a partial result — a partial traversal must never be mistaken for a complete one, because
this module's whole "delete detection" and "re-run is idempotent" story depends on
having actually seen everything. ``databricks-sdk``'s own pagination helper is deliberately
not reused for this: it has no page-count visibility to hook a cap into, only item
visibility, which is exactly the shape that misses the empty-page pathology. The
underlying request/retry primitive (the SDK's ``HttpEndpoint.get``, with its
``Retry-After``-aware backoff on 429/5xx) *is* reused, at every single page fetch.

**Deliberately out of scope for this fallback:**

* **Tags.** Reading them needs a SQL warehouse and a round trip through the Statement
  Execution API (decision D6), which is real per-poll cost this cheap listing-based scan
  should not carry, and which is gated on config (``sql_warehouse_id``) this module does
  not own. A tag-only edit is not caught here; it will be caught whenever ``read()``
  (T4.4) is asked to hydrate the object for some other reason, but not proactively by
  this poll. Flagged, not silently assumed away.
* **Delta Sharing / Marketplace listings.** Decision D1 defers these; only UC schemas and
  tables/views are in scope.

Traversal helpers page catalogs, then schemas within each catalog, then (for the
``DATASET`` stream) tables within each schema — mirroring the UC namespace
``catalog.schema.table`` (RS-01 section 1.1, section 4.1). ``GLOSSARY_TERM`` and
``CATEGORY`` are not native to Databricks (decision D5) and raise
:class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import structlog

from qlabs_catalog_sync_sdk.contract import (
    ChangeKind,
    ChangeRef,
    EntityType,
    IdentityRef,
    ListChangedResult,
    Watermark,
    WatermarkKind,
)
from qlabs_catalog_sync_sdk.envelope import compute_checksum
from qlabs_catalog_sync_sdk.exceptions import (
    AuthError,
    CapabilityError,
    ConnectorError,
    TransientError,
)
from qlabs_catalog_sync_sdk.http import HttpEndpoint

from .config import DatabricksConfig

__all__ = [
    "DEFAULT_MAX_PAGES_PER_LISTING",
    "SNAPSHOT_FORMAT_VERSION",
    "derive_tenant_id",
    "list_changed",
]

_logger = structlog.get_logger("qlabs_connector_databricks.changes")

#: Schema version stamped into every cursor payload this module writes, so a future
#: incompatible change to the snapshot's shape can be detected instead of silently
#: mis-parsed. Bump this and update :func:`_decode_snapshot` if the shape ever changes.
SNAPSHOT_FORMAT_VERSION: Final = 1

#: Per-listing-call page cap (catalogs; schemas-of-a-catalog; tables-of-a-schema). Each
#: individual UC list endpoint gets its own fresh budget. 1000 pages comfortably covers
#: any real workspace at Databricks' server-chosen page sizes; hitting it means the
#: endpoint is not terminating pagination, not that the workspace is merely large.
DEFAULT_MAX_PAGES_PER_LISTING: Final = 1000

_CATALOGS_PATH: Final = "/api/2.1/unity-catalog/catalogs"
_SCHEMAS_PATH: Final = "/api/2.1/unity-catalog/schemas"
_TABLES_PATH: Final = "/api/2.1/unity-catalog/tables"


def derive_tenant_id(config: DatabricksConfig) -> str:
    """A stable ``IdentityRef.tenant_id`` when the engine has not set ``ctx.tenant``.

    Uses the workspace host with its scheme stripped: UC native keys (``full_name``) are
    unique only within one workspace's metastore (RS-01 section 4.1), so the workspace
    itself is the natural tenant boundary for a connector that talks to exactly one
    workspace per configured endpoint.
    """
    return config.host.removeprefix("https://").removeprefix("http://")


# ----------------------------------------------------------------------------------------
# Observed object + snapshot codec
# ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Observed:
    """One object seen during a traversal: enough to diff and to build a ``ChangeRef``."""

    checksum: str
    last_modified_at: datetime | None
    display_name: str
    secondary_keys: dict[str, str] = field(default_factory=dict)


def _decode_snapshot(since: Watermark) -> dict[str, str]:
    """The ``{native_key: checksum}`` map carried by a previous ``next_watermark``.

    Empty for an ``initial`` watermark (nothing observed yet — everything is a
    candidate) and for anything that is not a well-formed cursor of our own making: a
    watermark this module cannot make sense of is treated the same as "no prior
    knowledge" rather than raised, because that fails safe (over-report candidates,
    which is cheap) rather than failing the whole cycle over a state-store artifact.
    """
    if since.kind is not WatermarkKind.CURSOR or not since.cursor:
        return {}
    try:
        payload: Any = json.loads(since.cursor)
        checksums = payload["checksums"] if isinstance(payload, dict) else None
        if not isinstance(checksums, dict):
            raise TypeError("cursor payload has no 'checksums' object")
        return {str(key): str(value) for key, value in checksums.items()}
    except Exception:
        _logger.warning(
            "databricks.list_changed.snapshot_unparseable",
            stream=since.stream_key,
        )
        return {}


def _encode_snapshot(fresh: Mapping[str, _Observed]) -> str:
    checksums = {native_key: observed.checksum for native_key, observed in fresh.items()}
    return json.dumps(
        {"v": SNAPSHOT_FORMAT_VERSION, "checksums": checksums},
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_epoch_millis(value: object) -> datetime | None:
    """UC's audit timestamps (``created_at``/``updated_at``) are epoch milliseconds."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


# ----------------------------------------------------------------------------------------
# Error translation
# ----------------------------------------------------------------------------------------


def _translate_http_error(
    exc: httpx.HTTPStatusError | httpx.TransportError,
    *,
    endpoint: str,
    entity_type: EntityType,
) -> ConnectorError:
    """Map a terminal ``httpx`` failure from ``HttpEndpoint`` onto an SDK typed exception.

    ``HttpEndpoint`` already retried anything retryable (429/5xx/transport errors); by
    the time an error reaches here, it is terminal. 401/403 -> :class:`AuthError` (not
    retryable — the engine quarantines the endpoint); 429/5xx (retries exhausted) or any
    other transport failure -> :class:`TransientError`; any other 4xx also becomes
    :class:`TransientError` as the honest fallback, matching this connector's own
    ``auth.translate_databricks_error``: we don't know the plan was wrong, only that
    this attempt failed.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return AuthError(
                f"Databricks UC API returned HTTP {status}",
                endpoint=endpoint,
                entity_type=entity_type,
                cause=exc,
            )
        retry_after: float | None = None
        header = exc.response.headers.get("retry-after")
        if header is not None:
            try:
                retry_after = max(float(header), 0.0)
            except ValueError:
                retry_after = None
        return TransientError(
            f"Databricks UC API returned HTTP {status}",
            endpoint=endpoint,
            entity_type=entity_type,
            cause=exc,
            retry_after_seconds=retry_after,
        )
    return TransientError(
        "Databricks UC API request failed",
        endpoint=endpoint,
        entity_type=entity_type,
        cause=exc,
    )


# ----------------------------------------------------------------------------------------
# Paging
# ----------------------------------------------------------------------------------------


async def _paginate_pages(
    http: HttpEndpoint,
    path: str,
    *,
    params: Mapping[str, Any],
    items_key: str,
    max_pages: int,
    endpoint: str,
    entity_type: EntityType,
    swallow_missing: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Walk one UC list endpoint, yielding items and enforcing a hard page-count cap.

    ``swallow_missing`` treats a 404 as "nothing here" instead of raising: a schema or
    catalog can vanish between the moment we listed it as a parent and the moment we
    list its children (a real, if narrow, race in a multi-call traversal with no
    snapshot isolation across calls), and that should not fail the whole cycle.
    """
    page_token: str | None = None
    for _page_number in range(max_pages):
        call_params = dict(params)
        if page_token is not None:
            call_params["page_token"] = page_token
        try:
            response = await http.get(path, params=call_params)
        except httpx.HTTPStatusError as exc:
            if swallow_missing and exc.response.status_code == 404:
                _logger.warning(
                    "databricks.list_changed.listing_vanished",
                    path=path,
                    params=dict(params),
                )
                return
            raise _translate_http_error(exc, endpoint=endpoint, entity_type=entity_type) from exc
        except httpx.TransportError as exc:
            raise _translate_http_error(exc, endpoint=endpoint, entity_type=entity_type) from exc

        body: Any = response.json()
        items = body.get(items_key) if isinstance(body, dict) else None
        for item in items or []:
            if isinstance(item, dict):
                yield item

        next_token = body.get("next_page_token") if isinstance(body, dict) else None
        if not next_token:
            return
        page_token = str(next_token)

    raise TransientError(
        f"GET {path} did not terminate pagination within {max_pages} pages; the "
        "endpoint may be returning next_page_token indefinitely",
        endpoint=endpoint,
        entity_type=entity_type,
    )


def _list_params(**extra: Any) -> dict[str, Any]:
    """Common query params for every UC list call: paginated mode, browse-visible rows
    included so a service principal with only BROWSE on some objects is not silently
    skipped."""
    return {"max_results": 0, "include_browse": True, **extra}


# ----------------------------------------------------------------------------------------
# Traversal
# ----------------------------------------------------------------------------------------


async def _iter_catalog_names(
    http: HttpEndpoint, *, max_pages: int, endpoint: str, entity_type: EntityType
) -> AsyncIterator[str]:
    async for catalog in _paginate_pages(
        http,
        _CATALOGS_PATH,
        params=_list_params(),
        items_key="catalogs",
        max_pages=max_pages,
        endpoint=endpoint,
        entity_type=entity_type,
    ):
        name = catalog.get("name")
        if isinstance(name, str) and name:
            yield name


async def _iter_schemas(
    http: HttpEndpoint,
    *,
    catalog_name: str,
    max_pages: int,
    endpoint: str,
    entity_type: EntityType,
) -> AsyncIterator[dict[str, Any]]:
    async for schema in _paginate_pages(
        http,
        _SCHEMAS_PATH,
        params=_list_params(catalog_name=catalog_name),
        items_key="schemas",
        max_pages=max_pages,
        endpoint=endpoint,
        entity_type=entity_type,
        swallow_missing=True,
    ):
        yield schema


async def _iter_tables(
    http: HttpEndpoint,
    *,
    catalog_name: str,
    schema_name: str,
    max_pages: int,
    endpoint: str,
    entity_type: EntityType,
    cheap: bool,
) -> AsyncIterator[dict[str, Any]]:
    params = _list_params(catalog_name=catalog_name, schema_name=schema_name)
    if cheap:
        params.update(omit_columns=True, omit_properties=True, omit_username=True)
    async for table in _paginate_pages(
        http,
        _TABLES_PATH,
        params=params,
        items_key="tables",
        max_pages=max_pages,
        endpoint=endpoint,
        entity_type=entity_type,
        swallow_missing=True,
    ):
        yield table


async def _scan_schemas(
    http: HttpEndpoint, *, endpoint: str, max_pages: int
) -> dict[str, _Observed]:
    """Traverse catalogs -> schemas, folding each schema's table-membership fingerprint
    into its checksum so a child table's arrival, departure or rename registers as a
    data-product-level change even though it never touches the schema's own fields."""
    entity_type = EntityType.DATA_PRODUCT
    fresh: dict[str, _Observed] = {}
    async for catalog_name in _iter_catalog_names(
        http, max_pages=max_pages, endpoint=endpoint, entity_type=entity_type
    ):
        async for schema in _iter_schemas(
            http,
            catalog_name=catalog_name,
            max_pages=max_pages,
            endpoint=endpoint,
            entity_type=entity_type,
        ):
            full_name = schema.get("full_name")
            schema_name = schema.get("name")
            if not isinstance(full_name, str) or not full_name:
                continue

            table_names: list[str] = []
            if isinstance(schema_name, str) and schema_name:
                async for table in _iter_tables(
                    http,
                    catalog_name=catalog_name,
                    schema_name=schema_name,
                    max_pages=max_pages,
                    endpoint=endpoint,
                    entity_type=entity_type,
                    cheap=True,
                ):
                    table_full_name = table.get("full_name")
                    if isinstance(table_full_name, str) and table_full_name:
                        table_names.append(table_full_name)

            checksum = compute_checksum(
                {"schema": schema, "dataset_membership": sorted(table_names)}
            )
            secondary_keys: dict[str, str] = {}
            schema_id = schema.get("schema_id")
            if isinstance(schema_id, str) and schema_id:
                secondary_keys["schema_id"] = schema_id

            fresh[full_name] = _Observed(
                checksum=checksum,
                last_modified_at=_parse_epoch_millis(schema.get("updated_at")),
                display_name=full_name,
                secondary_keys=secondary_keys,
            )
    return fresh


async def _scan_tables(
    http: HttpEndpoint, *, endpoint: str, max_pages: int
) -> dict[str, _Observed]:
    """Traverse catalogs -> schemas -> tables, one checksum per table/view."""
    entity_type = EntityType.DATASET
    fresh: dict[str, _Observed] = {}
    async for catalog_name in _iter_catalog_names(
        http, max_pages=max_pages, endpoint=endpoint, entity_type=entity_type
    ):
        async for schema in _iter_schemas(
            http,
            catalog_name=catalog_name,
            max_pages=max_pages,
            endpoint=endpoint,
            entity_type=entity_type,
        ):
            schema_name = schema.get("name")
            if not isinstance(schema_name, str) or not schema_name:
                continue

            async for table in _iter_tables(
                http,
                catalog_name=catalog_name,
                schema_name=schema_name,
                max_pages=max_pages,
                endpoint=endpoint,
                entity_type=entity_type,
                cheap=False,
            ):
                full_name = table.get("full_name")
                if not isinstance(full_name, str) or not full_name:
                    continue

                secondary_keys: dict[str, str] = {}
                table_id = table.get("table_id")
                if isinstance(table_id, str) and table_id:
                    secondary_keys["table_id"] = table_id

                fresh[full_name] = _Observed(
                    checksum=compute_checksum(table),
                    last_modified_at=_parse_epoch_millis(table.get("updated_at")),
                    display_name=full_name,
                    secondary_keys=secondary_keys,
                )
    return fresh


# ----------------------------------------------------------------------------------------
# Diff
# ----------------------------------------------------------------------------------------


def _diff(
    fresh: Mapping[str, _Observed],
    prev_checksums: Mapping[str, str],
    *,
    endpoint: str,
    entity_type: EntityType,
    tenant_id: str,
) -> list[ChangeRef]:
    changes: list[ChangeRef] = []
    for native_key in sorted(fresh):
        observed = fresh[native_key]
        if prev_checksums.get(native_key) == observed.checksum:
            continue
        changes.append(
            ChangeRef(
                ref=IdentityRef(
                    endpoint=endpoint,
                    entity_type=entity_type,
                    native_key=native_key,
                    tenant_id=tenant_id,
                    secondary_keys=observed.secondary_keys,
                ),
                kind=ChangeKind.UPSERT,
                last_modified_at=observed.last_modified_at,
                display_name=observed.display_name,
            )
        )

    for native_key in sorted(set(prev_checksums) - set(fresh)):
        changes.append(
            ChangeRef(
                ref=IdentityRef(
                    endpoint=endpoint,
                    entity_type=entity_type,
                    native_key=native_key,
                    tenant_id=tenant_id,
                ),
                kind=ChangeKind.DELETED,
                display_name=native_key,
            )
        )
    return changes


# ----------------------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------------------


def _require_matching_stream(since: Watermark, *, endpoint: str, entity_type: EntityType) -> None:
    if since.endpoint != endpoint or since.entity_type != entity_type:
        raise ValueError(
            f"since watermark is for stream {since.stream_key!r}, not "
            f"{endpoint}:{entity_type.value!r}"
        )


async def list_changed(
    http: HttpEndpoint,
    entity_type: EntityType,
    since: Watermark,
    *,
    endpoint: str,
    tenant_id: str,
    max_pages_per_listing: int = DEFAULT_MAX_PAGES_PER_LISTING,
) -> ListChangedResult:
    """List UC schemas (``DATA_PRODUCT``) or tables/views (``DATASET``) changed since
    ``since``, plus the proposed next watermark.

    ``http`` must already be authenticated (``HttpEndpoint(config.host,
    auth=<a Databricks bearer token provider>)`` — see this module's docstring and the
    connector's ``__init__.py`` wiring note). ``entity_type`` other than
    ``DATA_PRODUCT``/``DATASET`` raises :class:`CapabilityError`: Databricks has no
    native glossary (decision D5).
    """
    _require_matching_stream(since, endpoint=endpoint, entity_type=entity_type)
    log = _logger.bind(endpoint=endpoint, entity_type=entity_type.value, tenant_id=tenant_id)
    prev_checksums = _decode_snapshot(since)

    if entity_type is EntityType.DATA_PRODUCT:
        fresh = await _scan_schemas(http, endpoint=endpoint, max_pages=max_pages_per_listing)
    elif entity_type is EntityType.DATASET:
        fresh = await _scan_tables(http, endpoint=endpoint, max_pages=max_pages_per_listing)
    else:
        raise CapabilityError(
            f"connector 'databricks' does not support entity_type {entity_type.value!r} "
            "for list_changed (no native glossary — decision D5)",
            endpoint=endpoint,
            entity_type=entity_type,
            operation="list_changed",
        )

    changes = _diff(
        fresh, prev_checksums, endpoint=endpoint, entity_type=entity_type, tenant_id=tenant_id
    )
    next_watermark = Watermark.from_cursor(
        endpoint,
        entity_type,
        _encode_snapshot(fresh),
        observed_at=datetime.now(UTC),
    )
    log.info(
        "databricks.list_changed.complete",
        objects_observed=len(fresh),
        candidates=len(changes),
    )
    return ListChangedResult(changes=changes, next_watermark=next_watermark, has_more=False)
