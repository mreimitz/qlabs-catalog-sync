"""Databricks read path: UC schema -> ``DataProduct``, tables/views -> ``Dataset``.

WP4 / T4.4 (Sonnet). Implements decision D1 of the RM-01 Databricks-to-Qlik decision: a
Unity Catalog schema (``catalog.schema``) becomes one neutral :class:`DataProduct`; the
tables and views inside it become that product's member :class:`Dataset` objects. This
module owns the *structural* read only -- entity identity, containment (which tables
belong to which schema), and ``table_type`` -> :class:`AssetType` classification -- and
pages defensively over ``GET /api/2.1/unity-catalog/schemas`` and ``.../tables``,
honoring the sync pair's ``catalog.schema`` glob selector patterns. ``list_changed``
(T4.3) lives in this package's ``changes.py``, not here.

Two seams are deliberately left for later tasks:

* **T4.5** (``mapping.py``) owns *content* mapping: ``comment`` -> ``description``,
  ``owner`` -> ``Party``, ``full_name`` -> ``physicalRef``, ``properties`` ->
  ``customAttributes`` cleanup. Every Databricks field this module does not
  structurally consume (``comment``, ``owner``, ``properties``, audit stamps, ...) is
  preserved verbatim in ``custom_attributes``, so T4.5 has lossless raw material to
  remap and nothing a connector reads is ever silently dropped in the meantime.
* **T4.7** (``sql_tags.py``) owns UC tags, read over the Statement Execution API
  (decision D6) rather than the endpoints this module pages over. ``tags`` stays ``[]``
  here.

Built entirely on already-landed SDK primitives rather than reinventing them:
:meth:`~qlabs_catalog_sync_sdk.http.HttpEndpoint.paginate_offset` for defensive
``max_results``/``next_page_token`` paging over both ``/schemas`` and ``/tables``
(never the ``databricks-sdk`` ``WorkspaceClient`` used by ``auth.py`` -- its transport
is not ``respx``-mockable, per that module's own docstring, and this module's tests
must be), and :func:`~qlabs_catalog_sync_sdk.envelope.build_field_envelopes` /
:func:`~qlabs_catalog_sync_sdk.envelope.compute_checksum` for every populated field's
provenance envelope.

## Identity: the stable object id is the join key, ``full_name`` travels alongside it

RS-01 section 4.1 is explicit that Unity Catalog names can be renamed (``new_name``)
and ids cannot, and the engine's own ``IdentityResolver.resolve`` (WP7/T7.1) looks a
neutral id up strictly by ``(endpoint, entity_type, tenant_id, native_key)`` -- once
bound, nothing about a rename changes that lookup. That guarantee only holds if
``native_key`` itself survives a rename, so :data:`IdentityRef.native_key` here is the
**stable object id** (``schema_id`` / ``table_id``), never ``full_name``. ``full_name``
still travels in ``secondary_keys["full_name"]``: it is not the join key, but it is not
optional either -- Unity Catalog has no "get schema/table by id" endpoint, so a read
still needs it to know what to fetch. Putting ``full_name`` in ``native_key`` instead
would silently break identity continuity across a Databricks rename, exactly what D1
and RS-01 4.1 warn against.

## Tenant: the metastore id, not the workspace host

A ``tenant_id`` scopes a native key to the domain it is actually unique within (RS-03
section 4: native keys "are only unique within a tenant/account"). Unity Catalog object
ids are metastore-scoped, not workspace-scoped -- RS-01 1.1: "one metastore can be
attached to many workspaces [in a region]". Using the workspace host as ``tenant_id``
would silently split one physical schema's identity across every workspace sharing its
metastore. ``metastore_id`` is returned on every schema/table row Unity Catalog's list
endpoints emit, so it is read directly off each row rather than fetched separately.

## Selector patterns: a parameter, never an import from the engine

``catalog.schema`` glob selection is configured on the sync pair
(``qlabs_catalog_sync.config.SyncPairConfig.catalog_schema_patterns``), but this
connector may depend on nothing outside the SDK plus its own vendor libraries -- so
every function here takes ``catalog_schema_patterns`` as a plain ``Sequence[str]`` and
matches with its own ``fnmatch``-based :func:`matches_catalog_schema`, mirroring
``qlabs_catalog_sync.config.matches_catalog_schema`` in behavior (case-sensitive glob
per side) without importing it, so the two independently-maintained matchers cannot
silently drift apart.
"""

from __future__ import annotations

import fnmatch
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, NoReturn

import httpx
import structlog
from pydantic import JsonValue

from qlabs_catalog_sync_sdk.envelope import build_field_envelopes
from qlabs_catalog_sync_sdk.exceptions import AuthError, CapabilityError, NotFound, TransientError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import (
    AssetType,
    DataProduct,
    Dataset,
    EntityType,
    IdentityRef,
    NeutralEntity,
)

__all__ = [
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_PAGE_SIZE",
    "SCHEMAS_PATH",
    "TABLES_PATH",
    "SchemaRead",
    "asset_type_for_table",
    "build_data_product",
    "build_dataset",
    "build_schema_identity_ref",
    "build_table_identity_ref",
    "iter_matching_schemas",
    "literal_catalog_names",
    "matches_any_pattern",
    "matches_catalog_schema",
    "read_dataset",
    "read_entity",
    "read_schema",
    "split_schema_full_name",
    "split_table_full_name",
]

_logger = structlog.get_logger("qlabs_connector_databricks.read")

SCHEMAS_PATH: Final[str] = "/api/2.1/unity-catalog/schemas"
TABLES_PATH: Final[str] = "/api/2.1/unity-catalog/tables"

DEFAULT_PAGE_SIZE: Final[int] = 100
"""Explicit ``max_results`` sent on every list call. Never omitted: RS-01 section 3.6
warns some UC list endpoints default to an unbounded/large page on their own, and
relying on that default would be the opposite of paging defensively."""

DEFAULT_MAX_ITEMS: Final[int] = 20_000
"""A safety backstop, not a business limit. If a listing has not terminated within this
many yielded items, something is wrong (a broken ``next_page_token`` loop, a server
bug) and this module raises rather than looping forever -- a runaway pager fails a test
(or a production run) loudly instead of hanging it."""


# ----------------------------------------------------------------------------------
# catalog.schema selector patterns
# ----------------------------------------------------------------------------------


def matches_catalog_schema(pattern: str, catalog: str, schema: str) -> bool:
    """True when ``catalog.schema`` matches the ``catalog.schema`` glob ``pattern``.

    Mirrors ``qlabs_catalog_sync.config.matches_catalog_schema`` in behavior --
    case-sensitive ``fnmatch.fnmatchcase`` per side, exactly one ``.`` separating the
    two glob segments -- without importing it (see this module's docstring).
    Deliberately forgiving of a malformed ``pattern``: anything without exactly one
    ``.`` simply matches nothing, rather than raising, since the engine's
    ``SyncPairConfig`` is the authoritative validator and already rejected a bad
    pattern at config-load time, long before it could reach a connector. Likewise,
    Unity Catalog names never contain a literal ``.`` themselves (RS-01), so a
    ``catalog``/``schema`` argument that does -- e.g. a table's three-part
    ``full_name`` passed by mistake where a schema's two-part one belongs -- matches
    nothing rather than letting a stray ``*`` glob across the extra segment.
    """
    catalog_glob, sep, schema_glob = pattern.partition(".")
    if not sep or "." in schema_glob or "." in catalog or "." in schema:
        return False
    return fnmatch.fnmatchcase(catalog, catalog_glob) and fnmatch.fnmatchcase(schema, schema_glob)


def matches_any_pattern(catalog: str, schema: str, patterns: Sequence[str]) -> bool:
    """True when ``catalog.schema`` matches at least one of ``patterns``."""
    return any(matches_catalog_schema(pattern, catalog, schema) for pattern in patterns)


_GLOB_METACHARACTERS: Final[frozenset[str]] = frozenset("*?[]!")


def literal_catalog_names(catalog_schema_patterns: Sequence[str]) -> list[str]:
    """The distinct, non-wildcarded catalog segments named by ``catalog_schema_patterns``,
    in first-seen order.

    A convenience for the common case (``"sales.*"``, ``"prod.sales_*"``) where every
    pattern names a literal catalog. See :func:`iter_matching_schemas` for why a
    wildcarded catalog segment (``"prod_*.finance"``) is excluded here rather than
    expanded -- that needs ``/api/2.1/unity-catalog/catalogs``, outside decision D1's
    ``/schemas`` + ``/tables`` pair; a caller with such a pattern must resolve the
    catalog set itself and pass ``catalog_names`` explicitly.
    """
    seen: list[str] = []
    for pattern in catalog_schema_patterns:
        catalog, sep, _schema = pattern.partition(".")
        if sep and catalog and catalog not in seen and not _has_glob_metacharacters(catalog):
            seen.append(catalog)
    return seen


def _has_glob_metacharacters(segment: str) -> bool:
    return any(char in _GLOB_METACHARACTERS for char in segment)


# ----------------------------------------------------------------------------------
# full_name parsing
# ----------------------------------------------------------------------------------


def split_schema_full_name(full_name: str) -> tuple[str, str]:
    """Split a schema ``full_name`` (``"catalog.schema"``) into its two segments.

    Raises :class:`ValueError` for anything else, including a three-part table
    ``full_name`` -- which must never be treated as a schema's.
    """
    parts = full_name.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"expected a 'catalog.schema' full name, got {full_name!r}")
    return parts[0], parts[1]


def split_table_full_name(full_name: str) -> tuple[str, str, str]:
    """Split a table ``full_name`` (``"catalog.schema.table"``) into its three segments."""
    parts = full_name.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"expected a 'catalog.schema.table' full name, got {full_name!r}")
    return parts[0], parts[1], parts[2]


# ----------------------------------------------------------------------------------
# table_type -> AssetType
# ----------------------------------------------------------------------------------

_VIEW_TABLE_TYPES: Final[frozenset[str]] = frozenset({"VIEW", "MATERIALIZED_VIEW"})
_STANDARD_TABLE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "MANAGED",
        "EXTERNAL",
        "FOREIGN",
        "STREAMING_TABLE",
        "MANAGED_SHALLOW_CLONE",
        "EXTERNAL_SHALLOW_CLONE",
    }
)


def asset_type_for_table(table_type: object) -> AssetType:
    """Map a Unity Catalog ``table_type`` to the neutral :class:`AssetType`.

    ``VIEW`` and ``MATERIALIZED_VIEW`` become :attr:`AssetType.VIEW`; every physical
    table shape (``MANAGED``, ``EXTERNAL``, ``FOREIGN``, ``STREAMING_TABLE`` and their
    shallow-clone variants) becomes :attr:`AssetType.TABLE`. Volumes are a distinct UC
    object type never returned by the Tables API this module pages over, so
    :attr:`AssetType.VOLUME` never appears here -- decision D1 scopes v1 to tables and
    views. Anything else (a future/unrecognized ``table_type``, or a missing one)
    becomes :attr:`AssetType.OTHER` rather than guessing.
    """
    if table_type in _VIEW_TABLE_TYPES:
        return AssetType.VIEW
    if table_type in _STANDARD_TABLE_TYPES:
        return AssetType.TABLE
    return AssetType.OTHER


# ----------------------------------------------------------------------------------
# Raw payload -> neutral entity
# ----------------------------------------------------------------------------------

_SCHEMA_STRUCTURAL_FIELDS: Final[frozenset[str]] = frozenset(
    {"name", "full_name", "catalog_name", "schema_id", "metastore_id"}
)
_TABLE_STRUCTURAL_FIELDS: Final[frozenset[str]] = frozenset(
    {"name", "full_name", "catalog_name", "schema_name", "table_id", "metastore_id"}
)


def _require_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Databricks response is missing required field {key!r}: {raw!r}")
    return value


def _passthrough(raw: Mapping[str, Any], *, exclude: frozenset[str]) -> dict[str, JsonValue]:
    """Every field of ``raw`` this module does not structurally consume, verbatim.

    This is what makes an unrecognized/unmapped source field round-trip losslessly
    into ``custom_attributes`` instead of being silently dropped.
    """
    return {key: value for key, value in raw.items() if key not in exclude}


def _parse_databricks_timestamp(value: object) -> datetime | None:
    """Epoch-milliseconds -> aware UTC ``datetime``, or ``None`` if unparseable.

    Best-effort and forgiving on purpose: an unparseable or absent ``updated_at`` only
    costs the field envelope's ``last_modified_at`` provenance hint, not the read
    itself -- Databricks is "snapshot + checksum only" provenance regardless (see
    :class:`~qlabs_catalog_sync_sdk.models.FieldEnvelope`), so this is never
    load-bearing for change detection.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def build_schema_identity_ref(raw_schema: Mapping[str, Any], *, endpoint: str) -> IdentityRef:
    """The stable :class:`IdentityRef` for one Unity Catalog schema.

    See this module's docstring for why ``native_key`` is ``schema_id`` (not
    ``full_name``) and ``tenant_id`` is ``metastore_id`` (not the workspace host).
    """
    return IdentityRef(
        endpoint=endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=_require_str(raw_schema, "schema_id"),
        tenant_id=_require_str(raw_schema, "metastore_id"),
        secondary_keys={"full_name": _require_str(raw_schema, "full_name")},
    )


def build_table_identity_ref(raw_table: Mapping[str, Any], *, endpoint: str) -> IdentityRef:
    """The stable :class:`IdentityRef` for one Unity Catalog table or view.

    Same reasoning as :func:`build_schema_identity_ref`, with ``table_id`` in place of
    ``schema_id``.
    """
    return IdentityRef(
        endpoint=endpoint,
        entity_type=EntityType.DATASET,
        native_key=_require_str(raw_table, "table_id"),
        tenant_id=_require_str(raw_table, "metastore_id"),
        secondary_keys={"full_name": _require_str(raw_table, "full_name")},
    )


def build_data_product(raw_schema: Mapping[str, Any], *, endpoint: str) -> DataProduct:
    """Build the neutral :class:`DataProduct` for one Unity Catalog schema (decision D1).

    Structural only: ``name`` and ``identities``, plus every field this module does
    not itself consume preserved verbatim in ``custom_attributes`` (``comment``,
    ``owner``, ``properties``, audit stamps, ...). ``description``, ``owners``,
    ``tags``, ``documentation``, ``status`` and ``placement`` are left at their
    defaults -- T4.5 and T4.7's seams. ``dataset_refs`` is deliberately left empty; see
    :class:`SchemaRead`.
    """
    ref = build_schema_identity_ref(raw_schema, endpoint=endpoint)
    name = _require_str(raw_schema, "name")
    custom_attributes = _passthrough(raw_schema, exclude=_SCHEMA_STRUCTURAL_FIELDS)
    values: dict[str, Any] = {"name": name, "custom_attributes": custom_attributes}
    field_envelopes = build_field_envelopes(
        values,
        source_endpoint=endpoint,
        last_modified_at=_parse_databricks_timestamp(raw_schema.get("updated_at")),
    )
    return DataProduct(
        identities=[ref],
        name=name,
        custom_attributes=custom_attributes,
        field_envelopes=field_envelopes,
    )


def build_dataset(raw_table: Mapping[str, Any], *, endpoint: str) -> Dataset:
    """Build the neutral :class:`Dataset` for one Unity Catalog table or view.

    Structural: ``name``, ``identities``, and ``asset_type`` (mapped from
    ``table_type``, see :func:`asset_type_for_table`), plus a lossless
    ``custom_attributes`` passthrough of everything else -- ``columns`` included.
    ``description``, ``owners``, ``tags``, ``classifications`` and ``physical_ref`` are
    left at their defaults -- T4.5 and T4.7's seams.
    """
    ref = build_table_identity_ref(raw_table, endpoint=endpoint)
    name = _require_str(raw_table, "name")
    asset_type = asset_type_for_table(raw_table.get("table_type"))
    custom_attributes = _passthrough(raw_table, exclude=_TABLE_STRUCTURAL_FIELDS)
    values: dict[str, Any] = {
        "name": name,
        "asset_type": asset_type,
        "custom_attributes": custom_attributes,
    }
    field_envelopes = build_field_envelopes(
        values,
        source_endpoint=endpoint,
        last_modified_at=_parse_databricks_timestamp(raw_table.get("updated_at")),
    )
    return Dataset(
        identities=[ref],
        name=name,
        asset_type=asset_type,
        custom_attributes=custom_attributes,
        field_envelopes=field_envelopes,
    )


# ----------------------------------------------------------------------------------
# HTTP error translation
# ----------------------------------------------------------------------------------


def _try_parse_retry_after(value: str | None) -> float | None:
    """Best-effort ``Retry-After`` parse (delta-seconds form only).

    By the time this module sees an ``httpx.HTTPStatusError``, :class:`HttpEndpoint`
    has already retried a 429/5xx and honored ``Retry-After`` internally -- this is
    only surfaced onward as a hint for the engine's own backoff, not relied on to
    drive a retry here.
    """
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def _raise_for_status(
    exc: httpx.HTTPStatusError, *, endpoint: str, entity_type: EntityType, native_key: str
) -> NoReturn:
    status = exc.response.status_code
    if status == 404:
        raise NotFound(
            f"{native_key!r} was not found (HTTP 404)",
            endpoint=endpoint,
            entity_type=entity_type,
            cause=exc,
            native_key=native_key,
        ) from exc
    if status in (401, 403):
        raise AuthError(
            f"authentication/authorization failed for {native_key!r} (HTTP {status})",
            endpoint=endpoint,
            entity_type=entity_type,
            cause=exc,
        ) from exc
    # 429/5xx (retries already exhausted by HttpEndpoint) and any other unexpected
    # status: the honest fallback is "this attempt failed", not "the object is gone"
    # or "the plan was wrong" -- mirrors auth.py's translate_databricks_error.
    raise TransientError(
        f"request for {native_key!r} failed with HTTP {status}",
        endpoint=endpoint,
        entity_type=entity_type,
        cause=exc,
        retry_after_seconds=_try_parse_retry_after(exc.response.headers.get("retry-after")),
    ) from exc


# ----------------------------------------------------------------------------------
# Defensive pagination
# ----------------------------------------------------------------------------------


async def _paginate_defensively(
    items: AsyncIterator[dict[str, Any]], *, max_items: int
) -> AsyncIterator[dict[str, Any]]:
    count = 0
    async for item in items:
        count += 1
        if count > max_items:
            raise TransientError(
                f"pagination did not terminate within {max_items} items; aborting defensively"
            )
        yield item


async def _iter_schemas_in_catalog(
    http: HttpEndpoint,
    *,
    catalog_name: str,
    endpoint: str,
    page_size: int,
    max_items: int,
) -> AsyncIterator[dict[str, Any]]:
    try:
        async for raw in _paginate_defensively(
            http.paginate_offset(
                "GET",
                SCHEMAS_PATH,
                items_key="schemas",
                params={"catalog_name": catalog_name},
                max_results=page_size,
            ),
            max_items=max_items,
        ):
            yield raw
    except httpx.HTTPStatusError as exc:
        _raise_for_status(
            exc, endpoint=endpoint, entity_type=EntityType.DATA_PRODUCT, native_key=catalog_name
        )
    except httpx.TransportError as exc:
        raise TransientError(
            f"transport error listing schemas in catalog {catalog_name!r}: {exc}",
            endpoint=endpoint,
            entity_type=EntityType.DATA_PRODUCT,
            cause=exc,
        ) from exc


async def _iter_tables_in_schema(
    http: HttpEndpoint,
    *,
    catalog_name: str,
    schema_name: str,
    endpoint: str,
    page_size: int,
    max_items: int,
) -> AsyncIterator[dict[str, Any]]:
    full_schema_name = f"{catalog_name}.{schema_name}"
    try:
        async for raw in _paginate_defensively(
            http.paginate_offset(
                "GET",
                TABLES_PATH,
                items_key="tables",
                params={"catalog_name": catalog_name, "schema_name": schema_name},
                max_results=page_size,
            ),
            max_items=max_items,
        ):
            yield raw
    except httpx.HTTPStatusError as exc:
        _raise_for_status(
            exc, endpoint=endpoint, entity_type=EntityType.DATASET, native_key=full_schema_name
        )
    except httpx.TransportError as exc:
        raise TransientError(
            f"transport error listing tables in schema {full_schema_name!r}: {exc}",
            endpoint=endpoint,
            entity_type=EntityType.DATASET,
            cause=exc,
        ) from exc


async def _get_table(http: HttpEndpoint, *, full_name: str, endpoint: str) -> dict[str, Any]:
    try:
        response = await http.get(f"{TABLES_PATH}/{full_name}")
    except httpx.HTTPStatusError as exc:
        _raise_for_status(
            exc, endpoint=endpoint, entity_type=EntityType.DATASET, native_key=full_name
        )
    except httpx.TransportError as exc:
        raise TransientError(
            f"transport error reading table {full_name!r}: {exc}",
            endpoint=endpoint,
            entity_type=EntityType.DATASET,
            cause=exc,
        ) from exc
    result: dict[str, Any] = response.json()
    return result


async def _find_one(
    items: AsyncIterator[dict[str, Any]], *, predicate: Callable[[dict[str, Any]], bool]
) -> dict[str, Any] | None:
    """The first item matching ``predicate``, stopping (and so paging no further) as
    soon as it is found."""
    async for item in items:
        if predicate(item):
            return item
    return None


async def iter_matching_schemas(
    http: HttpEndpoint,
    *,
    catalog_names: Sequence[str],
    catalog_schema_patterns: Sequence[str],
    endpoint: str,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> AsyncIterator[dict[str, Any]]:
    """Page over ``/schemas`` for each of ``catalog_names`` and yield the raw payload
    of every schema whose ``catalog.schema`` matches at least one of
    ``catalog_schema_patterns``.

    A near-miss (wrong catalog, or a schema name the pattern's glob does not cover) is
    silently excluded, never yielded. ``catalog_names`` is caller-supplied rather than
    discovered here -- see :func:`literal_catalog_names` for why.
    """
    seen_catalogs: set[str] = set()
    for catalog_name in catalog_names:
        if catalog_name in seen_catalogs:
            continue
        seen_catalogs.add(catalog_name)
        async for raw in _iter_schemas_in_catalog(
            http,
            catalog_name=catalog_name,
            endpoint=endpoint,
            page_size=page_size,
            max_items=max_items,
        ):
            name = raw.get("name")
            if isinstance(name, str) and matches_any_pattern(
                catalog_name, name, catalog_schema_patterns
            ):
                yield raw
            else:
                _logger.debug(
                    "databricks.read.schema_excluded",
                    endpoint=endpoint,
                    catalog=catalog_name,
                    schema=name,
                )


# ----------------------------------------------------------------------------------
# Connector.read() entry points
# ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemaRead:
    """The result of reading one UC schema: the :class:`DataProduct` plus its member
    :class:`Dataset` objects (tables/views), read together because Unity Catalog has
    no "list this schema's tables" primitive other than filtering ``/tables`` by
    ``catalog_name`` + ``schema_name`` -- producing one without the other would throw
    that work away.

    ``data_product.dataset_refs`` is deliberately left empty:
    :class:`~qlabs_catalog_sync_sdk.models.NeutralEntity` mints a fresh random
    ``neutral_id`` on every read, so writing these ``Dataset``s' ids into
    ``dataset_refs`` would make that field's checksum change on every cycle even when
    nothing changed -- breaking the exact idempotency property
    :func:`~qlabs_catalog_sync_sdk.envelope.compute_checksum` exists to guarantee.
    Resolving product-to-dataset membership into stable engine ids is IdentityMap-shaped
    work for the sync loop, not something a connector can do from one API response.
    """

    data_product: DataProduct
    datasets: list[Dataset]


def _require_entity_type(ref: IdentityRef, expected: EntityType) -> None:
    if ref.entity_type is not expected:
        raise ValueError(f"expected a {expected!r} ref, got {ref.entity_type!r}")


def _require_secondary_key(ref: IdentityRef, key: str) -> str:
    value = ref.secondary_keys.get(key)
    if not value:
        raise ValueError(
            f"ref.secondary_keys[{key!r}] is required to read this Databricks object "
            f"(native_key={ref.native_key!r})"
        )
    return value


async def read_schema(
    http: HttpEndpoint,
    ref: IdentityRef,
    *,
    catalog_schema_patterns: Sequence[str],
    page_size: int = DEFAULT_PAGE_SIZE,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> SchemaRead:
    """Read one UC schema (``ref.entity_type`` must be ``DATA_PRODUCT``) into a
    :class:`SchemaRead`.

    ``ref.secondary_keys["full_name"]`` says which schema to fetch (Unity Catalog has
    no "get by id" endpoint). Raises :class:`NotFound` when ``full_name`` falls outside
    ``catalog_schema_patterns`` (this connector has no business reading it) or when the
    schema does not turn up while paging ``/schemas``.
    """
    _require_entity_type(ref, EntityType.DATA_PRODUCT)
    full_name = _require_secondary_key(ref, "full_name")
    catalog, schema = split_schema_full_name(full_name)
    if not matches_any_pattern(catalog, schema, catalog_schema_patterns):
        raise NotFound(
            f"{full_name!r} is outside the configured catalog.schema selector patterns",
            endpoint=ref.endpoint,
            entity_type=ref.entity_type,
            native_key=ref.native_key,
        )
    raw_schema = await _find_one(
        _iter_schemas_in_catalog(
            http,
            catalog_name=catalog,
            endpoint=ref.endpoint,
            page_size=page_size,
            max_items=max_items,
        ),
        predicate=lambda raw: bool(raw.get("name") == schema),
    )
    if raw_schema is None:
        raise NotFound(
            f"schema {full_name!r} was not found",
            endpoint=ref.endpoint,
            entity_type=ref.entity_type,
            native_key=ref.native_key,
        )
    data_product = build_data_product(raw_schema, endpoint=ref.endpoint)
    datasets = [
        build_dataset(raw_table, endpoint=ref.endpoint)
        async for raw_table in _iter_tables_in_schema(
            http,
            catalog_name=catalog,
            schema_name=schema,
            endpoint=ref.endpoint,
            page_size=page_size,
            max_items=max_items,
        )
    ]
    _logger.info(
        "databricks.read.schema",
        endpoint=ref.endpoint,
        catalog=catalog,
        schema=schema,
        table_count=len(datasets),
    )
    return SchemaRead(data_product=data_product, datasets=datasets)


async def read_dataset(http: HttpEndpoint, ref: IdentityRef) -> Dataset:
    """Read one table or view (``ref.entity_type`` must be ``DATASET``) into a
    :class:`Dataset`, by a single ``GET /tables/{full_name}`` -- no paging, since the
    object is already uniquely identified.
    """
    _require_entity_type(ref, EntityType.DATASET)
    full_name = _require_secondary_key(ref, "full_name")
    split_table_full_name(full_name)  # validates the shape; raises ValueError if malformed
    raw_table = await _get_table(http, full_name=full_name, endpoint=ref.endpoint)
    return build_dataset(raw_table, endpoint=ref.endpoint)


async def read_entity(
    http: HttpEndpoint,
    ref: IdentityRef,
    *,
    catalog_schema_patterns: Sequence[str],
    page_size: int = DEFAULT_PAGE_SIZE,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> NeutralEntity:
    """Matches ``Connector.read(ref)``'s signature exactly -- the function the
    connector's ``read()`` override should delegate straight to.

    Dispatches on ``ref.entity_type``: a ``DATA_PRODUCT`` ref reads the schema (paging
    over ``/tables`` to build its member ``Dataset``s too, then keeping only the
    ``DataProduct`` -- the engine fetches each ``Dataset`` again through its own
    ``DATASET``-typed ``list_changed``/``read`` poll stream, so nothing here is wasted:
    :func:`read_schema` is also exported directly for a caller that wants both halves
    of :class:`SchemaRead` at once, e.g. an initial full-scan discovery path that
    would otherwise re-page ``/tables`` a second time). A ``DATASET`` ref reads one
    table directly, by full name, with no paging.
    """
    if ref.entity_type is EntityType.DATA_PRODUCT:
        schema_read = await read_schema(
            http,
            ref,
            catalog_schema_patterns=catalog_schema_patterns,
            page_size=page_size,
            max_items=max_items,
        )
        return schema_read.data_product
    if ref.entity_type is EntityType.DATASET:
        return await read_dataset(http, ref)
    raise CapabilityError(
        f"the Databricks connector does not support reading {ref.entity_type!r}",
        endpoint=ref.endpoint,
        entity_type=ref.entity_type,
        operation="read",
    )
