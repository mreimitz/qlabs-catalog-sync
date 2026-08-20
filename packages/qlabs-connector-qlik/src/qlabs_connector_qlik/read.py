"""Qlik read path.

WP3 / T3.3 (Sonnet). Reads Qlik entities into neutral envelopes so the engine can
diff desired state against existing target state before writing.

Two API families, deliberately kept apart (RS-02 section 3.1, section 3.5 CRUD
reference):

* **Data products** live under ``/api/data-governance/data-products`` — no ``/v1``
  segment. :func:`read_data_product` maps a single ``GET .../{id}`` response;
  :func:`list_changed` (dispatched to a private ``_iter_data_product_changes``)
  drains ``GET /api/data-governance/data-products`` via the SDK's cursor-pagination
  helper.
* **Datasets** are read through the **Items API** (``/api/v1/items``), never
  ``/api/v1/data-sets`` directly: an item's ``resourceAttributes.secureQri`` is the
  forward-looking durable dataset key (RS-02 two-way-sync-readiness notes, section 3),
  and it is only exposed on the single-item ``GET /api/v1/items/{itemId}`` call, not
  documented on the list response. :func:`read_dataset` therefore always issues that
  detail GET.

Design decisions worth being explicit about, since none of them can be checked
against a live tenant (decision D8 / agent-guide "no live tenants" rule):

1. **Identity strategy for datasets.** A Dataset's neutral ``IdentityRef.native_key``
   is ``secureQri`` (falling back to the legacy ``qri``, then to the Items-API item id
   if neither is present — flag: TENANT_UNVERIFIED whether every dataset item always
   carries ``resourceAttributes``). ``secureQri`` is not a valid Items-API URL
   parameter, so every ``IdentityRef`` this module hands back also carries
   ``secondary_keys["id"]`` (the item id) and, when present, ``secondary_keys
   ["resourceId"]``. :func:`read_dataset` reads a dataset by ``ref.secondary_keys
   ["id"]``, not ``ref.native_key`` — see its docstring. Data products have no such
   split: ``id`` and ``qri`` are documented as "effectively equivalent join keys"
   (two-way-sync-readiness notes, section 3), so the data-product native key is simply
   ``id`` and reading uses it directly.
2. **The ETag seam.** Qlik returns an ``ETag`` response header on both single-resource
   GETs (data product and item). Rather than inventing a side channel, this module
   stores it as ``source_revision`` on *every* ``FieldEnvelope`` built for that read
   (via ``build_field_envelopes(..., source_revision=etag)``) — the SDK already
   defines that field for exactly this ("source_revision ... revision counter... not
   every endpoint exposes field-level provenance"), and ``FieldDiff.expected_revision``
   is the matching seam on the write side (T3.5). A resource-level ETag applied
   uniformly to every field envelope is honest: Qlik's concurrency token is
   whole-resource, not per-field. TENANT_UNVERIFIED: the two-way-sync-readiness notes
   confirm ``if-match`` on glossary/category/term writes but explicitly say the data
   products PATCH endpoint's ETag support is undocumented — T3.5 must confirm before
   relying on it there. This module only *captures* whatever ETag header comes back,
   if any; it never assumes one is present.
3. **``custom_attributes`` exclusion policy.** Every raw response key is preserved
   verbatim in ``custom_attributes`` *except* the keys this module maps losslessly onto
   a dedicated neutral field: ``name``, ``description``, ``readMe``/``spaceId`` (data
   product only), ``tags``. ``keyContacts``/``ownerId`` are int*entionally* **not**
   excluded even though they feed ``owners`` — mapping Qlik's free-text ``role`` string
   onto the closed :class:`~qlabs_catalog_sync_sdk.models.PartyRole` vocabulary is lossy,
   so the original strings stay recoverable in ``custom_attributes`` alongside the typed
   projection. This is what makes an unknown/future field (and anything this module
   does not yet understand) round-trip byte-identical.
4. **``dataset_refs`` is mapped back through an injected seam; ``glossary_term_refs``
   is left empty.** Both are ``list[UUID]`` fields on
   :class:`~qlabs_catalog_sync_sdk.models.DataProduct` — engine-assigned ``neutral_id``
   values — while Qlik's raw ``datasetIds``/``apiConsumableDatasetIds``/``glossaryIds``
   arrays hold opaque tenant-native ids. Neither is ever fabricated into a UUID, and
   every raw array keeps travelling verbatim in ``custom_attributes`` (decision D2:
   never invent resolution, only report).

   The manifest declares ``dataset_refs`` ``rw`` — readable **and** writable — so
   leaving it permanently empty made the manifest dishonest: ``create()``/``update()``
   write ``datasetIds`` and a read-after-write never showed the membership back.
   :data:`DatasetRefLookup` closes that gap without breaking the hard dependency rule.
   It is the exact mirror of ``resolve.py``'s :data:`~qlabs_connector_qlik.resolve
   .DatasetIdentityLookup`: a plain async callable, ``Callable[[str], Awaitable[UUID |
   None]]``, native Qlik dataset id in, engine neutral id out, defaulting to "no answer"
   (:func:`no_dataset_refs`) exactly as the forward seam defaults to
   ``_no_identity_binding``. It is declared here rather than in ``resolve.py`` only
   because ``resolve.py`` imports *from* this module, so the reverse import would be a
   cycle; ``resolve.py``'s docstring cross-references it.

   The reporting rule is all-or-nothing, and deliberately so: ``dataset_refs`` is
   reported (and therefore gets a ``FieldEnvelope``) **only** when every native id in
   ``datasetIds`` mapped to a neutral id. A partial list would assert "this product's
   members are exactly these", which is a different and false claim; an empty list would
   assert "this product has no members", which is the same lie ``write.py`` refuses to
   send (its point 10). When anything fails to map, the field is simply not reported
   this cycle — the same shape ``envelope.py``'s rule 10 gives any unreported field —
   the shortfall is logged, and the raw ``datasetIds`` stays in ``custom_attributes``
   for whoever needs the ground truth. A genuinely empty ``datasetIds`` maps completely
   (to ``[]``) and *is* reported.

   ``glossary_term_refs`` stays permanently empty: it is declared ``na`` (decision D5),
   so there is nothing for it to be honest or dishonest about, and no glossary resolver
   exists in v1.
5. **``status`` from ``activated``.** Qlik's documented data-product lifecycle is only
   a boolean ``activated`` flag (plus ``activatedAt``/``activatedOn``, kept in
   ``custom_attributes``); there is no separate "deactivated" signal distinguishable
   from "never activated". This module maps ``activated: true`` ->
   :attr:`DataProductStatus.ACTIVE` and ``activated: false`` ->
   :attr:`DataProductStatus.DRAFT`, which is a best-effort simplification —
   TENANT_UNVERIFIED whether a deactivated product reports anything that could
   distinguish it from a fresh draft.
6. **No true incremental polling yet.** :func:`list_changed` performs a full scan
   every call — it drains every page of the relevant listing endpoint and reports
   every object as :attr:`~qlabs_catalog_sync_sdk.contract.ChangeKind.UPSERT`,
   regardless of ``since``. RS-02 documents ``sort`` on Items ``updatedAt`` but not a
   confirmed "changed since timestamp" filter query syntax for either the Items API or
   the (largely undocumented) data-products list endpoint, so this module does not
   guess one. The engine's checksum-based idempotency (``envelope.has_changed``) makes
   a full scan safe — unchanged objects simply produce identical checksums and no
   writes — at the cost of O(n) reads every cycle rather than true incrementality.
   ``next_watermark`` is therefore a stable opaque
   :attr:`~qlabs_catalog_sync_sdk.contract.WatermarkKind.CURSOR` value (``"full-scan"``)
   rather than a timestamp, since there is no real resume position to encode.
   TENANT_UNVERIFIED / follow-up: confirm a documented ``updatedAt``-since filter on a
   live tenant to make this incremental.
7. **The data-products list endpoint itself.** RS-02 section 3.5's CRUD reference for
   data products does not explicitly document a bare ``GET
   /api/data-governance/data-products`` (only ``POST`` to create and ``GET .../{id}``
   to read one are listed). This module assumes the collection resource also supports
   a plain cursor-paginated ``GET`` — standard REST convention, and consistent with
   section 3.3's generic "list responses return a ``data`` array plus a ``links``
   object" description — but this is TENANT_UNVERIFIED and is the single riskiest
   assumption in this module. If it is wrong, only :func:`list_changed` for
   ``DATA_PRODUCT`` is affected; :func:`read_data_product` (a single documented ``GET
   .../{id}``) does not depend on it.

Every ``http.get``/pagination call in this module is wrapped with
``auth.classify_response_error``/``auth.classify_transport_error`` (already landed in
T3.1) so a 404 raises :class:`~qlabs_catalog_sync_sdk.exceptions.NotFound`, a 401/403
raises :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError`, and an exhausted
429/5xx or transport failure raises
:class:`~qlabs_catalog_sync_sdk.exceptions.TransientError` — no second classification
is invented here.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final, TypeAlias

import httpx
import structlog

from qlabs_catalog_sync_sdk.contract import (
    ChangeKind,
    ChangeRef,
    EntityType,
    IdentityRef,
    ListChangedResult,
    NeutralEntity,
    Watermark,
    WatermarkKind,
)
from qlabs_catalog_sync_sdk.envelope import build_field_envelopes
from qlabs_catalog_sync_sdk.exceptions import CapabilityError, ConnectorError, NotFound
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import (
    AssetType,
    DataProduct,
    DataProductStatus,
    Dataset,
    Party,
    PartyRole,
    Tag,
    TextField,
)

from .auth import classify_response_error, classify_transport_error

__all__ = [
    "DATA_PRODUCTS_PATH",
    "DEFAULT_PAGE_SIZE",
    "ITEMS_PATH",
    "DatasetRefLookup",
    "list_changed",
    "no_dataset_refs",
    "read",
    "read_data_product",
    "read_dataset",
]

_logger = structlog.get_logger("qlabs_connector_qlik.read")

#: Data-governance API base path — no ``/v1`` segment (RS-02 section 3.1).
DATA_PRODUCTS_PATH: Final = "/api/data-governance/data-products"

#: Tenant REST API base path for the unified catalog index (RS-02 section 1.1).
ITEMS_PATH: Final = "/api/v1/items"

#: Default page size for cursor-paginated listings. Qlik does not document a bound
#: (RS-02 section 3.3); this is a conservative, undocumented default —
#: TENANT_UNVERIFIED.
DEFAULT_PAGE_SIZE: Final = 100

#: The single opaque watermark value :func:`list_changed` ever proposes, since every
#: call performs a full scan (see module docstring, point 6).
_FULL_SCAN_CURSOR: Final = "full-scan"

#: Data-product response keys mapped losslessly onto a dedicated neutral field, and
#: therefore excluded from ``custom_attributes`` (module docstring, point 3).
_DATA_PRODUCT_MAPPED_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "description", "readMe", "tags", "spaceId"}
)

#: Dataset/item response keys mapped losslessly onto a dedicated neutral field, and
#: therefore excluded from ``custom_attributes``.
_DATASET_MAPPED_KEYS: Final[frozenset[str]] = frozenset({"name", "description"})

#: The exact shape the engine must satisfy to let a read map Qlik's native ``datasetIds``
#: back onto the neutral ``DataProduct.dataset_refs`` (module docstring, point 4). The
#: mirror image of ``resolve.DatasetIdentityLookup``: a Qlik dataset id — whatever
#: ``datasetIds`` actually carries, i.e. the ``resourceId`` the forward seam hands out —
#: in, the engine's ``neutral_id`` for it out, or ``None`` when the IdentityMap has no
#: binding for it. ``datasetIds`` is **not** excluded from ``custom_attributes``: this
#: mapping is lossy by construction (an unbound id has no neutral counterpart), so the
#: raw array stays recoverable, per the exclusion policy in point 3.
DatasetRefLookup: TypeAlias = Callable[[str], Awaitable["uuid.UUID | None"]]


async def no_dataset_refs(native_id: str) -> uuid.UUID | None:
    """The default :data:`DatasetRefLookup`: no Qlik dataset id has a known neutral id.

    Honest for a caller that has not wired the seam — the engine owns the IdentityMap and
    a connector may never import it. Every ``datasetIds`` entry then fails to map, so
    ``dataset_refs`` is simply not reported (module docstring, point 4) rather than
    reported empty, and the raw ids stay in ``custom_attributes``.
    """
    del native_id
    return None


# --------------------------------------------------------------------------------------
# Public dispatchers — match Connector.list_changed / Connector.read exactly, plus the
# extra context (http, endpoint, tenant_id, space_id) those methods read off `self`.
# --------------------------------------------------------------------------------------


async def list_changed(
    http: HttpEndpoint,
    entity_type: EntityType,
    since: Watermark,
    *,
    endpoint: str,
    tenant_id: str,
    space_id: str,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> ListChangedResult:
    """List candidate changes for ``entity_type``, plus the proposed next watermark.

    ``since`` is accepted for contract conformance (D8) but not otherwise used: every
    call performs a full scan (module docstring, point 6). Supports
    :attr:`EntityType.DATA_PRODUCT` and :attr:`EntityType.DATASET` only — glossary and
    category are out of the MVP (decision D5) and raise
    :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError`.
    """
    del since  # accepted for contract conformance; full scan every call (see module docstring)
    if entity_type is EntityType.DATA_PRODUCT:
        changes = [
            change
            async for change in _iter_data_product_changes(
                http, endpoint=endpoint, tenant_id=tenant_id, space_id=space_id, page_size=page_size
            )
        ]
    elif entity_type is EntityType.DATASET:
        changes = [
            change
            async for change in _iter_dataset_changes(
                http, endpoint=endpoint, tenant_id=tenant_id, space_id=space_id, page_size=page_size
            )
        ]
    else:
        raise _unsupported_entity_type(endpoint, entity_type, operation="list_changed")

    next_watermark = Watermark(
        endpoint=endpoint,
        entity_type=entity_type,
        kind=WatermarkKind.CURSOR,
        cursor=_FULL_SCAN_CURSOR,
    )
    return ListChangedResult(changes=changes, next_watermark=next_watermark, has_more=False)


async def read(
    http: HttpEndpoint,
    ref: IdentityRef,
    *,
    endpoint: str,
    dataset_ref_lookup: DatasetRefLookup | None = None,
) -> NeutralEntity:
    """Read one Qlik object into its matching neutral entity subclass.

    Dispatches on ``ref.entity_type``; the concrete return is a :class:`DataProduct` or
    a :class:`Dataset`, both valid narrowings of :class:`NeutralEntity` per the
    contract. Raises :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError` for
    any other entity type (glossary/category — decision D5).

    ``dataset_ref_lookup`` is the :data:`DatasetRefLookup` seam (module docstring, point
    4), and defaults to :func:`no_dataset_refs` — no binding known, so ``dataset_refs``
    is not reported at all rather than reported wrong.
    """
    if ref.entity_type is EntityType.DATA_PRODUCT:
        return await read_data_product(
            http, ref, endpoint=endpoint, dataset_ref_lookup=dataset_ref_lookup
        )
    if ref.entity_type is EntityType.DATASET:
        return await read_dataset(http, ref, endpoint=endpoint)
    raise _unsupported_entity_type(endpoint, ref.entity_type, operation="read")


# --------------------------------------------------------------------------------------
# Single-entity reads
# --------------------------------------------------------------------------------------


async def read_data_product(
    http: HttpEndpoint,
    ref: IdentityRef,
    *,
    endpoint: str,
    dataset_ref_lookup: DatasetRefLookup | None = None,
) -> DataProduct:
    """``GET /api/data-governance/data-products/{id}`` -> :class:`DataProduct`.

    Uses ``ref.secondary_keys.get("id", ref.native_key)`` as the path id — for a data
    product ``native_key`` already *is* the id (module docstring, point 1), so this is
    equivalent to ``ref.native_key`` in practice but stays consistent with
    :func:`read_dataset`'s lookup shape.

    ``dataset_ref_lookup`` maps Qlik's native ``datasetIds`` back onto the neutral
    ``dataset_refs`` (module docstring, point 4). It issues no HTTP of its own — it is a
    pure IdentityMap question the engine answers — so this stays exactly one ``GET``.
    """
    native_id = ref.secondary_keys.get("id", ref.native_key)
    raw, headers = await _get_json(
        http,
        f"{DATA_PRODUCTS_PATH}/{native_id}",
        endpoint=endpoint,
        entity_type=EntityType.DATA_PRODUCT,
    )
    dataset_refs = await _map_dataset_refs(
        raw.get("datasetIds"),
        dataset_ref_lookup if dataset_ref_lookup is not None else no_dataset_refs,
        endpoint=endpoint,
        native_key=native_id,
    )
    return _map_data_product(
        raw, headers, endpoint=endpoint, tenant_id=ref.tenant_id, dataset_refs=dataset_refs
    )


async def read_dataset(http: HttpEndpoint, ref: IdentityRef, *, endpoint: str) -> Dataset:
    """``GET /api/v1/items/{itemId}`` -> :class:`Dataset`.

    Reads by ``ref.secondary_keys["id"]`` (the Items-API item id), **not**
    ``ref.native_key`` — a dataset's native key is ``secureQri`` (module docstring,
    point 1), which is not a valid Items-API URL parameter. Raises
    :class:`~qlabs_catalog_sync_sdk.exceptions.NotFound` immediately, without any HTTP
    call, when ``ref`` carries no item id: that is a genuinely unresolvable reference,
    not a transient failure.
    """
    item_id = ref.secondary_keys.get("id")
    if not item_id:
        raise NotFound(
            "cannot read a Qlik dataset without the Items API item id "
            "(ref.secondary_keys['id']); secureQri alone is not a valid item lookup key",
            endpoint=endpoint,
            entity_type=EntityType.DATASET.value,
            native_key=ref.native_key,
        )
    raw, headers = await _get_json(
        http, f"{ITEMS_PATH}/{item_id}", endpoint=endpoint, entity_type=EntityType.DATASET
    )
    return _map_dataset(raw, headers, endpoint=endpoint, tenant_id=ref.tenant_id)


# --------------------------------------------------------------------------------------
# Listings (paged defensively via the SDK's cursor-pagination helper)
# --------------------------------------------------------------------------------------


async def _iter_data_product_changes(
    http: HttpEndpoint,
    *,
    endpoint: str,
    tenant_id: str,
    space_id: str,
    page_size: int,
) -> AsyncIterator[ChangeRef]:
    params = {"limit": page_size, "spaceId": space_id}
    try:
        async for item in http.paginate_cursor("GET", DATA_PRODUCTS_PATH, params=params):
            if not isinstance(item, dict):
                continue
            native_id = item.get("id")
            if not isinstance(native_id, str) or not native_id:
                continue
            name = item.get("name")
            yield ChangeRef(
                ref=IdentityRef(
                    endpoint=endpoint,
                    entity_type=EntityType.DATA_PRODUCT,
                    native_key=native_id,
                    tenant_id=tenant_id,
                    secondary_keys=_secondary_keys(id=native_id, qri=item.get("qri")),
                ),
                kind=ChangeKind.UPSERT,
                last_modified_at=_parse_instant(item.get("updatedAt")),
                display_name=name if isinstance(name, str) else None,
            )
    except httpx.HTTPStatusError as exc:
        raise classify_response_error(
            exc, endpoint=endpoint, entity_type=EntityType.DATA_PRODUCT.value
        ) from exc
    except httpx.TransportError as exc:
        raise classify_transport_error(
            exc, endpoint=endpoint, entity_type=EntityType.DATA_PRODUCT.value
        ) from exc


async def _iter_dataset_changes(
    http: HttpEndpoint,
    *,
    endpoint: str,
    tenant_id: str,
    space_id: str,
    page_size: int,
) -> AsyncIterator[ChangeRef]:
    # Cheap listing only — no secureQri here (module docstring point 1). The engine
    # calls read_dataset() on each ref to get the detail GET that resolves it.
    params = {"limit": page_size, "resourceType": "dataset", "spaceId": space_id}
    try:
        async for item in http.paginate_cursor("GET", ITEMS_PATH, params=params):
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                continue
            name = item.get("name")
            yield ChangeRef(
                ref=IdentityRef(
                    endpoint=endpoint,
                    entity_type=EntityType.DATASET,
                    native_key=item_id,
                    tenant_id=tenant_id,
                    secondary_keys=_secondary_keys(id=item_id, resourceId=item.get("resourceId")),
                ),
                kind=ChangeKind.UPSERT,
                last_modified_at=_parse_instant(item.get("updatedAt")),
                display_name=name if isinstance(name, str) else None,
            )
    except httpx.HTTPStatusError as exc:
        raise classify_response_error(
            exc, endpoint=endpoint, entity_type=EntityType.DATASET.value
        ) from exc
    except httpx.TransportError as exc:
        raise classify_transport_error(
            exc, endpoint=endpoint, entity_type=EntityType.DATASET.value
        ) from exc


# --------------------------------------------------------------------------------------
# Response -> neutral mapping
# --------------------------------------------------------------------------------------


async def _map_dataset_refs(
    raw_ids: object,
    lookup: DatasetRefLookup,
    *,
    endpoint: str,
    native_key: str,
) -> list[uuid.UUID] | None:
    """Qlik ``datasetIds`` -> neutral ``dataset_refs``, or ``None`` for "cannot say".

    All-or-nothing by design (module docstring, point 4). ``None`` means the field is not
    reported this cycle — because ``datasetIds`` was absent, was not an array of usable
    ids, or because at least one id has no IdentityMap binding. Reporting a partial list
    would assert a membership the connector cannot prove, and reporting ``[]`` would
    assert the product has no members at all; both are exactly the kind of invention
    decision D2 forbids. The raw ids remain in ``custom_attributes`` either way.

    Two native ids that map to the same neutral id collapse to one entry, order
    preserved — the mirror of ``write.py``'s ``_dedupe`` on the way out.
    """
    if raw_ids is None:
        return None
    if not isinstance(raw_ids, list):
        await _logger.awarning(
            "qlik.read.dataset_refs_unreported",
            endpoint=endpoint,
            native_key=native_key,
            reason="datasetIds is not an array",
        )
        return None
    if not all(isinstance(item, str) and item for item in raw_ids):
        await _logger.awarning(
            "qlik.read.dataset_refs_unreported",
            endpoint=endpoint,
            native_key=native_key,
            reason="datasetIds carries an entry that is not a usable id",
            requested=len(raw_ids),
        )
        return None

    refs: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    unbound = 0
    for dataset_id in raw_ids:
        neutral_id = await lookup(dataset_id)
        if neutral_id is None:
            unbound += 1
            continue
        if neutral_id not in seen:
            seen.add(neutral_id)
            refs.append(neutral_id)
    if unbound:
        await _logger.awarning(
            "qlik.read.dataset_refs_unreported",
            endpoint=endpoint,
            native_key=native_key,
            reason="no IdentityMap binding for every member",
            unbound=unbound,
            requested=len(raw_ids),
        )
        return None
    return refs


def _map_data_product(
    raw: Mapping[str, Any],
    headers: httpx.Headers,
    *,
    endpoint: str,
    tenant_id: str,
    dataset_refs: Sequence[uuid.UUID] | None = None,
) -> DataProduct:
    native_id = raw.get("id")
    if not isinstance(native_id, str) or not native_id:
        raise ConnectorError(
            "Qlik data-product response carries no usable 'id'",
            endpoint=endpoint,
            entity_type=EntityType.DATA_PRODUCT.value,
        )
    identity = IdentityRef(
        endpoint=endpoint,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=native_id,
        tenant_id=tenant_id,
        secondary_keys=_secondary_keys(qri=raw.get("qri"), mainId=raw.get("mainId")),
    )

    etag = headers.get("etag")
    updated_at = _parse_instant(raw.get("updatedAt"))

    values: dict[str, Any] = {"name": raw.get("name") or ""}
    if "description" in raw:
        values["description"] = _text_field(raw["description"], markdown=False)
    if "readMe" in raw:
        values["documentation"] = _text_field(raw["readMe"], markdown=True)
    if isinstance(raw.get("activated"), bool):
        values["status"] = (
            DataProductStatus.ACTIVE if raw["activated"] else DataProductStatus.DRAFT
        )
    owners = _data_product_owners(raw)
    if owners is not None:
        values["owners"] = owners
    if "tags" in raw:
        values["tags"] = _string_tags(raw["tags"])
    if dataset_refs is not None:
        # Only when every native id mapped — see _map_dataset_refs. Setting it here is
        # what also gives the field a `FieldEnvelope`, so "reported" and "has an
        # envelope" stay the same statement.
        values["dataset_refs"] = list(dataset_refs)
    if "spaceId" in raw:
        space_id = raw["spaceId"]
        values["placement"] = space_id if isinstance(space_id, str) else None

    custom_attributes = _custom_attributes(raw, exclude=_DATA_PRODUCT_MAPPED_KEYS)
    field_envelopes = build_field_envelopes(
        {**values, "custom_attributes": custom_attributes},
        source_endpoint=endpoint,
        source_revision=etag,
        last_modified_at=updated_at,
    )

    return DataProduct(
        identities=[identity],
        custom_attributes=custom_attributes,
        field_envelopes=field_envelopes,
        name=values["name"],
        description=values.get("description"),
        documentation=values.get("documentation"),
        status=values.get("status"),
        owners=values.get("owners", []),
        tags=values.get("tags", []),
        dataset_refs=values.get("dataset_refs", []),
        placement=values.get("placement"),
    )


def _map_dataset(
    raw: Mapping[str, Any],
    headers: httpx.Headers,
    *,
    endpoint: str,
    tenant_id: str,
) -> Dataset:
    item_id = raw.get("id")
    resource_id = raw.get("resourceId")
    resource_attributes = raw.get("resourceAttributes")
    secure_qri: str | None = None
    legacy_qri: str | None = None
    if isinstance(resource_attributes, dict):
        candidate_secure = resource_attributes.get("secureQri")
        if isinstance(candidate_secure, str) and candidate_secure:
            secure_qri = candidate_secure
        candidate_legacy = resource_attributes.get("qri")
        if isinstance(candidate_legacy, str) and candidate_legacy:
            legacy_qri = candidate_legacy

    native_key = secure_qri or legacy_qri or (item_id if isinstance(item_id, str) else None)
    if not native_key:
        raise ConnectorError(
            "Qlik dataset item carries no usable identity (secureQri, qri, and id all "
            "missing or empty)",
            endpoint=endpoint,
            entity_type=EntityType.DATASET.value,
        )

    identity = IdentityRef(
        endpoint=endpoint,
        entity_type=EntityType.DATASET,
        native_key=native_key,
        tenant_id=tenant_id,
        secondary_keys=_secondary_keys(id=item_id, resourceId=resource_id),
    )

    etag = headers.get("etag")
    updated_at = _parse_instant(raw.get("updatedAt"))

    values: dict[str, Any] = {"name": raw.get("name") or "", "asset_type": AssetType.DATASET}
    if "description" in raw:
        values["description"] = _text_field(raw["description"], markdown=False)
    owners = _dataset_owners(raw)
    if owners is not None:
        values["owners"] = owners
    tags = _dataset_tags(raw)
    if tags is not None:
        values["tags"] = tags
    if secure_qri:
        values["physical_ref"] = secure_qri

    custom_attributes = _custom_attributes(raw, exclude=_DATASET_MAPPED_KEYS)
    field_envelopes = build_field_envelopes(
        {**values, "custom_attributes": custom_attributes},
        source_endpoint=endpoint,
        source_revision=etag,
        last_modified_at=updated_at,
    )

    return Dataset(
        identities=[identity],
        custom_attributes=custom_attributes,
        field_envelopes=field_envelopes,
        name=values["name"],
        description=values.get("description"),
        owners=values.get("owners", []),
        tags=values.get("tags", []),
        physical_ref=values.get("physical_ref"),
        asset_type=values["asset_type"],
    )


# --------------------------------------------------------------------------------------
# Field-level helpers
# --------------------------------------------------------------------------------------


def _text_field(value: object, *, markdown: bool) -> TextField | None:
    if not isinstance(value, str):
        return None
    return TextField.markdown(value) if markdown else TextField.plain(value)


def _string_tags(value: object) -> list[Tag]:
    if not isinstance(value, list):
        return []
    return [Tag(key=item) for item in value if isinstance(item, str) and item]


def _data_product_owners(raw: Mapping[str, Any]) -> list[Party] | None:
    """``owners`` from ``ownerId`` (role OWNER) plus ``keyContacts`` (deduped by id).

    ``None`` when neither key is present at all (the source did not report owners this
    cycle, per envelope.py rule 10); an empty list is a real, present-but-empty value.
    """
    if "ownerId" not in raw and "keyContacts" not in raw:
        return None
    owners: list[Party] = []
    seen: set[str] = set()
    owner_id = raw.get("ownerId")
    if isinstance(owner_id, str) and owner_id:
        owners.append(Party(party_id=owner_id, role=PartyRole.OWNER))
        seen.add(owner_id)
    key_contacts = raw.get("keyContacts")
    if isinstance(key_contacts, list):
        for contact in key_contacts:
            if not isinstance(contact, dict):
                continue
            user_id = contact.get("userId")
            if not isinstance(user_id, str) or not user_id or user_id in seen:
                continue
            seen.add(user_id)
            owners.append(Party(party_id=user_id, role=_party_role(contact.get("role"))))
    return owners


def _dataset_owners(raw: Mapping[str, Any]) -> list[Party] | None:
    if "ownerId" not in raw:
        return None
    owner_id = raw.get("ownerId")
    if isinstance(owner_id, str) and owner_id:
        return [Party(party_id=owner_id, role=PartyRole.OWNER)]
    return []


def _dataset_tags(raw: Mapping[str, Any]) -> list[Tag] | None:
    """``tags`` from the item envelope's ``meta.tags`` (RS-02 section 1.1/1.4).

    ``None`` when ``meta`` carries no ``tags`` key at all (unreported this cycle).
    """
    meta = raw.get("meta")
    if not isinstance(meta, dict) or "tags" not in meta:
        return None
    raw_tags = meta.get("tags")
    if not isinstance(raw_tags, list):
        return []
    tags: list[Tag] = []
    for entry in raw_tags:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name:
                tags.append(Tag(key=name))
        elif isinstance(entry, str) and entry:
            tags.append(Tag(key=entry))
    return tags


def _party_role(raw_role: object) -> PartyRole:
    if not isinstance(raw_role, str):
        return PartyRole.OTHER
    lowered = raw_role.strip().lower()
    if "owner" in lowered:
        return PartyRole.OWNER
    if "steward" in lowered:
        return PartyRole.STEWARD
    if "contact" in lowered:
        return PartyRole.CONTACT
    return PartyRole.OTHER


def _custom_attributes(raw: Mapping[str, Any], *, exclude: frozenset[str]) -> dict[str, Any]:
    """Every raw response key not losslessly mapped elsewhere, untouched.

    This is what makes an unknown or future response field round-trip byte-identical
    (module docstring, point 3): nothing here is transformed, only filtered.
    """
    return {key: value for key, value in raw.items() if key not in exclude}


def _secondary_keys(**candidates: object) -> dict[str, str]:
    return {key: value for key, value in candidates.items() if isinstance(value, str) and value}


def _parse_instant(value: object) -> datetime | None:
    """Best-effort RFC 3339 parse. ``None`` for anything not a confidently-aware instant.

    Naive results (an offset-less string) are treated as unparseable rather than
    guessed at, since ``FieldEnvelope.last_modified_at`` is ``AwareDatetime``.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


async def _get_json(
    http: HttpEndpoint, url: str, *, endpoint: str, entity_type: EntityType
) -> tuple[dict[str, Any], httpx.Headers]:
    """``GET url``, classified via ``auth.py`` (T3.1) and never re-invented here."""
    try:
        response = await http.get(url)
    except httpx.HTTPStatusError as exc:
        raise classify_response_error(
            exc, endpoint=endpoint, entity_type=entity_type.value
        ) from exc
    except httpx.TransportError as exc:
        raise classify_transport_error(
            exc, endpoint=endpoint, entity_type=entity_type.value
        ) from exc
    body = response.json()
    if not isinstance(body, dict):
        raise ConnectorError(
            f"Qlik returned a non-object JSON body from {url}",
            endpoint=endpoint,
            entity_type=entity_type.value,
        )
    return body, response.headers


def _unsupported_entity_type(
    endpoint: str, entity_type: EntityType, *, operation: str
) -> CapabilityError:
    return CapabilityError(
        f"the Qlik connector's read path does not support {entity_type.value!r} "
        "(glossary/category reads are out of the MVP — decision D5)",
        endpoint=endpoint,
        entity_type=entity_type.value,
        operation=operation,
    )
