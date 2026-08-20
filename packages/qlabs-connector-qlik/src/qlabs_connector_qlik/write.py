"""Qlik write path — the only place in v1 that mutates a customer's catalog.

WP3 / T3.4 (this task): :meth:`QlikWriter.create` — turn a neutral
:class:`~qlabs_catalog_sync_sdk.models.DataProduct` into the documented
``POST /api/data-governance/data-products`` body (note: **no** ``/v1`` segment — that
family is ``/api/v1/...``; RS-02 ``qlik-catalog-api-reference.md`` section 3.1), send it,
and hand back a :class:`~qlabs_catalog_sync_sdk.contract.WriteResult` that reports every
reference the connector could not place.

Still to land in this file, in order:

* **T3.5 — ``update()`` via JSON Patch.** Add ``QlikWriter.update(ref, diff)`` directly
  below :meth:`QlikWriter.create`. Everything it needs is already here: the per-field
  wire converters (:func:`_text_value`, :func:`_tag_values`) produce exactly the
  ``value`` shapes ``/name``, ``/description``, ``/readMe`` and ``/tags`` want;
  :meth:`QlikWriter._resolve_datasets` / :meth:`QlikWriter._resolve_owners` produce the
  ``/datasetIds`` and ``/keyContacts`` arrays (full-replace, which is what JSON Patch
  ``replace`` on an array path means anyway); ``manifest.QLIK_DATA_PRODUCT_PATCH_PATHS``
  and ``max_update_operations=8`` are already declared on the manifest this class holds;
  and :meth:`QlikWriter._send` is the single classified-request seam a PATCH goes
  through. **The one thing :meth:`QlikWriter._send` does not do yet is map HTTP 412 to**
  :class:`~qlabs_catalog_sync_sdk.exceptions.ConflictError` — ``auth.classify_response_error``
  has no 412 branch (it lands on the generic ``ConnectorError``), because nothing in the
  create path can produce one. T3.5 owns adding that, either by widening its own call or
  by handling 412 before delegating.
* **T3.7 — ``delete()`` and the lifecycle actions** (activate/deactivate/move). Those
  live in their own ``lifecycle.py``, not here.

Decisions this module implements (``decision-databricks-to-qlik-mvp.md``):

* **D2 — never create a Qlik dataset.** Member datasets are resolved through
  :class:`~qlabs_connector_qlik.resolve.QlikReferenceResolver` (T3.9), which only ever
  issues ``GET``\\ s. A member that does not resolve is **omitted from the payload and
  reported**, never invented. If dropping a member would leave
  ``apiConsumableDatasetIds`` outside ``datasetIds``, the subset is narrowed rather than
  an invalid payload being sent — and that narrowing is reported too.
* **D3 — ``keyContacts`` needs a Qlik ``userId``, not an email.** Owner emails resolve
  through the same resolver; an unmatched owner is dropped and reported.
* **D5 — glossary is out of the MVP,** so ``glossaryIds`` is **never sent** (see
  :meth:`QlikWriter._build_create_body` for the full justification). A product that
  carries neutral ``glossary_term_refs`` reports them as skipped rather than silently
  losing them.
* **D7 — activation is opt-in and off by default.** ``create()`` never activates: it
  sends no activation field and calls no ``/actions/activate`` endpoint, so the product
  starts ``activated: false``. A neutral ``status`` asking for anything other than
  ``draft`` is reported as skipped. Activation is T3.7 (the action) plus T7.4 (the
  opt-in).

**How a dropped reference reaches the caller.** Never through a log line alone.
:class:`~qlabs_catalog_sync_sdk.contract.WriteResult` carries ``skipped_fields`` (the
neutral field names that did not fully make it onto the wire) and ``detail`` (one
human-readable sentence per reason, for the run report). This module's convention,
stated once so the engine can rely on it:

* a neutral field appears in ``written_fields`` when **any** part of its value reached
  Qlik, and in ``skipped_fields`` when **any** part of it did not — so a partially
  written array (three of five members resolved) appears in **both**, and ``detail``
  says which three;
* ``detail`` enumerates at most :data:`_MAX_REPORTED_EXAMPLES` examples per reason and
  then says how many more there were, so one badly-configured product cannot produce an
  unbounded run-report string;
* ``detail`` never contains an owner's email address (personal data, and the same reason
  ``resolve.py`` logs only the email's domain) — see :func:`_owner_label`.

Design decisions worth being explicit about, since none of them can be checked against a
live tenant (decision D8 / agent-guide "no live tenants" rule):

1. **``spaceId`` comes from config, never from the neutral entity.** ``QlikConfig.space_id``
   is documented as *the* target space ("data products are created in it"), and a neutral
   ``placement`` arriving from a source connector is a source-shaped value (a Databricks
   catalog name, say), not a Qlik space id. Config therefore wins; a ``placement`` that
   is set and differs from the configured space is reported as skipped. This also matches
   the manifest, which declares ``placement`` ``ro`` because it is not one of the eight
   PATCH paths.
2. **``apiConsumableDatasetIds`` is omitted unless a caller explicitly asks for it.**
   The neutral model has no counterpart (``manifest.py`` says so), and the field is not
   inert: it exposes member datasets over Qlik's OData consumption APIs. Sending "all of
   them" because the RS-02 example happens to would be inventing an intent the operator
   never expressed, in a customer's tenant. :meth:`QlikWriter.create` therefore takes an
   explicit ``api_consumable_refs`` argument, defaulting to ``None`` (key omitted); when
   it *is* supplied, it goes through ``DatasetResolution.subset_for`` (``resolve.py``)
   and is then re-filtered against the actually-sent ``datasetIds``, so the server-side
   subset rule holds by construction even after drops and capping.
3. **``glossaryIds`` is never sent at all** — not even as ``[]``. D5 puts glossary out of
   the MVP and the manifest declares ``glossary_term_refs`` ``na``; Databricks, the only
   v1 source, has no glossary to populate it from. Even if a neutral entity did carry
   ``glossary_term_refs``, those are engine neutral ``UUID``\\ s, and Qlik's
   ``glossaryIds`` wants tenant-native glossary ids — there is no resolver for that in
   v1, so the only honest options are "omit and report" or "invent", and D2's principle
   settles it. :data:`MAX_GLOSSARY_IDS` is still declared so the documented cap is
   recorded next to the one this build enforces.
4. **Key/value tags are flattened to ``"key=value"``.** Qlik's ``tags`` is a bare
   ``string[]``; the neutral :class:`~qlabs_catalog_sync_sdk.models.Tag` is key/value
   (Databricks UC tags are, per D6). A key-only tag round-trips exactly against
   ``read.py``'s ``Tag(key=item)`` mapping; a valued tag has no lossless Qlik shape, and
   sending the bare key instead would silently merge two different tags that share a key.
   TENANT_UNVERIFIED: whether Qlik constrains tag characters (``=`` in particular) or
   caps the array length is undocumented in RS-02.
5. **``description`` is sent verbatim even when the neutral field says markdown.**
   Qlik's ``description`` is a plain string and its ``readMe`` is the markdown field; the
   neutral ``documentation`` maps to ``readMe``, so a markdown ``description`` keeps its
   text and loses only the format declaration. Nothing is dropped, so nothing is reported.
6. **The create response's ``ETag`` header becomes ``source_revision`` when present.**
   ``read.py`` (point 2 of its docstring) already treats the resource-level ETag as the
   revision token, and T3.5's ``if-match`` needs one. TENANT_UNVERIFIED: RS-02 documents
   no ``ETag`` on the data-products POST response at all, so this captures whatever comes
   back and reports ``None`` otherwise — it never fabricates one from ``updatedAt``.
7. **Creating something whose manifest declares no writable field is refused before any
   HTTP call.** :meth:`QlikWriter._ensure_creatable` asks the manifest, rather than
   hard-coding a type check: a Qlik dataset is refused because every dataset field is
   declared ``ro`` (which is exactly what D2 means), and a glossary term is refused
   because the entity is declared unsupported (D5). That keeps the manifest the single
   source of truth the conformance kit's capability-honesty rule checks against.
8. **An entity that already carries a Qlik identity is logged, not refused.** Deciding
   create-vs-update is the engine's job (it owns the IdentityMap); a connector that
   second-guessed it would break a legitimate re-create after a target-side deletion. The
   warning exists so a duplicate-product bug is visible in the run log.

Every request in this module goes through :meth:`QlikWriter._send`, which reuses T3.1's
``auth.classify_response_error``/``auth.classify_transport_error`` — a 403 (creation
requires create permission in the target space, RS-02 section 5) becomes
:class:`~qlabs_catalog_sync_sdk.exceptions.AuthError`; a 429 (Tier 2, 100 req/min) or 5xx
is retried by ``HttpEndpoint`` and only becomes
:class:`~qlabs_catalog_sync_sdk.exceptions.TransientError` once those retries are
exhausted. No second classification is invented here.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, TypeAlias

import httpx
import structlog

from qlabs_catalog_sync_sdk.contract import (
    EntityType,
    IdentityRef,
    NeutralEntity,
    WriteResult,
)
from qlabs_catalog_sync_sdk.exceptions import CapabilityError, ConnectorError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.manifest import CapabilityManifest
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    DataProductStatus,
    Party,
    Tag,
    TextField,
)

from .auth import classify_response_error, classify_transport_error
from .manifest import qlik_capability_manifest
from .read import DATA_PRODUCTS_PATH
from .resolve import (
    DatasetIdentityLookup,
    DatasetMember,
    DatasetResolution,
    OwnerResolution,
    QlikReferenceResolver,
)

__all__ = [
    "MAX_DATASET_IDS",
    "MAX_GLOSSARY_IDS",
    "DatasetNameLookup",
    "QlikWriter",
    "build_writer",
    "no_dataset_names",
]

_logger = structlog.get_logger("qlabs_connector_qlik.write")

#: Server-side cap on ``datasetIds`` / ``apiConsumableDatasetIds`` (RS-02
#: ``qlik-two-way-sync-readiness.md`` section 2: "max 100 items"). Enforced client-side so
#: an over-sized product produces a reported partial write rather than a rejected request.
MAX_DATASET_IDS: Final = 100

#: Server-side cap on ``glossaryIds``. Declared for completeness only — this connector
#: never sends the field at all (module docstring, point 3).
MAX_GLOSSARY_IDS: Final = 100

#: How many individual examples one ``detail`` reason enumerates before collapsing the
#: rest into a count.
_MAX_REPORTED_EXAMPLES: Final = 5

#: Neutral field names, in the order they are reported, so ``written_fields`` and
#: ``skipped_fields`` are stable across runs and easy to assert on.
_FIELD_REPORT_ORDER: Final[tuple[str, ...]] = (
    "name",
    "description",
    "documentation",
    "status",
    "owners",
    "tags",
    "dataset_refs",
    "glossary_term_refs",
    "placement",
)

#: The display-name side of a member dataset, for ``resolve.py``'s tier-2 (name-within-
#: space) match. ``DataProduct.dataset_refs`` is a bare ``list[UUID]`` with no names
#: attached, and this connector must never import the engine to look one up, so the name
#: arrives through this seam exactly the way ``resolve.DatasetIdentityLookup`` carries
#: tier 1: a plain async callable, a bare ``UUID`` in and a bare ``str | None`` out.
#: Returning ``None`` disables tier 2 for that member — tier 1 (the IdentityMap) still
#: gets its chance, and a member that misses both is reported unresolved.
DatasetNameLookup: TypeAlias = Callable[[uuid.UUID], Awaitable["str | None"]]


async def no_dataset_names(neutral_id: uuid.UUID) -> str | None:
    """The default :data:`DatasetNameLookup`: no display names are known.

    Tier-1 (IdentityMap) resolution still works; tier-2 name matching is simply
    unavailable, which is the honest position for a caller that has not wired the seam.
    """
    del neutral_id
    return None


# --------------------------------------------------------------------------------------
# The request this module builds, before it is sent
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _CreateRequest:
    """The POST body plus the honest bookkeeping that becomes the ``WriteResult``."""

    body: dict[str, Any] = field(default_factory=dict)
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def wrote(self, *fields: str) -> None:
        for name in fields:
            if name not in self.written:
                self.written.append(name)

    def skip(self, name: str, note: str | None = None) -> None:
        if name not in self.skipped:
            self.skipped.append(name)
        if note is not None:
            self.notes.append(note)

    @property
    def written_fields(self) -> list[str]:
        return _in_report_order(self.written)

    @property
    def skipped_fields(self) -> list[str]:
        return _in_report_order(self.skipped)

    @property
    def detail(self) -> str | None:
        return "; ".join(self.notes) if self.notes else None


# --------------------------------------------------------------------------------------
# QlikWriter — the write path's long-lived collaborators, in one place
# --------------------------------------------------------------------------------------


class QlikWriter:
    """Writes data products into one configured Qlik space.

    One instance per configured endpoint, built in ``Connector.setup()`` and living until
    ``Connector.close()`` — the same lifetime as the
    :class:`~qlabs_connector_qlik.resolve.QlikReferenceResolver`
    it holds (whose caches are what make that lifetime worth having). It holds no
    per-write state, so concurrent ``create`` calls on one instance are safe.

    ``manifest`` defaults to the connector's real
    :func:`~qlabs_connector_qlik.manifest.qlik_capability_manifest`; it is injectable so a
    test can prove the capability gate against a deliberately narrower manifest without
    reaching into the connector.
    """

    def __init__(
        self,
        http: HttpEndpoint,
        *,
        endpoint: str,
        tenant_id: str,
        space_id: str,
        resolver: QlikReferenceResolver,
        manifest: CapabilityManifest | None = None,
        dataset_name_lookup: DatasetNameLookup | None = None,
    ) -> None:
        self._http = http
        self._endpoint = endpoint
        self._tenant_id = tenant_id
        self._space_id = space_id
        self._resolver = resolver
        self._manifest = manifest if manifest is not None else qlik_capability_manifest()
        self._dataset_name_lookup = (
            dataset_name_lookup if dataset_name_lookup is not None else no_dataset_names
        )

    @property
    def resolver(self) -> QlikReferenceResolver:
        """The reference resolver this writer sends every ``datasetIds``/``keyContacts``
        lookup through (decisions D2/D3). Exposed so T3.5's ``update()`` reuses the same
        instance — and therefore the same caches — rather than building a second one."""
        return self._resolver

    @property
    def manifest(self) -> CapabilityManifest:
        """The manifest every write is gated on. T3.5 reads ``allowed_update_paths`` and
        ``max_update_operations`` off it rather than re-deriving them."""
        return self._manifest

    # -- create (T3.4) ----------------------------------------------------------------

    async def create(
        self,
        entity: NeutralEntity,
        *,
        dataset_names: Mapping[uuid.UUID, str] | None = None,
        api_consumable_refs: Sequence[uuid.UUID] | None = None,
    ) -> WriteResult:
        """``POST /api/data-governance/data-products`` from a neutral data product.

        ``dataset_names`` is a per-call shortcut for :data:`DatasetNameLookup` — a caller
        that already holds the member :class:`~qlabs_catalog_sync_sdk.models.Dataset`
        entities can hand their names straight over instead of wiring the lookup. It wins
        over the injected lookup for the ids it covers.

        ``api_consumable_refs`` names the subset of ``entity.dataset_refs`` to expose over
        Qlik's OData consumption APIs. Omitted by default and never inferred (module
        docstring, point 2); whatever is supplied is filtered down to ids that actually
        made it into ``datasetIds``, so the server's subset rule cannot be violated.

        Raises :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError` — before any
        HTTP call — for an entity type this connector's manifest does not declare
        creatable, and :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError` for an
        entity with no usable ``name`` (also before any HTTP call: reference resolution
        itself issues ``GET``\\ s, so the guard runs first).
        """
        entity_type: EntityType | None = getattr(type(entity), "ENTITY_TYPE", None)
        self._ensure_creatable(entity_type)
        if not isinstance(entity, DataProduct):
            raise ConnectorError(
                f"expected a DataProduct to create, got {type(entity).__name__}",
                endpoint=self._endpoint,
                entity_type=EntityType.DATA_PRODUCT.value,
            )
        product = entity
        name = _required_name(product, endpoint=self._endpoint)

        existing = product.identity_for(self._endpoint)
        if existing is not None:
            # Module docstring, point 8 — visible, not fatal.
            await _logger.awarning(
                "qlik.write.create.entity_already_bound",
                endpoint=self._endpoint,
                native_key=existing.native_key,
            )

        request = await self._build_create_body(
            product,
            name=name,
            dataset_names=dataset_names,
            api_consumable_refs=api_consumable_refs,
        )

        response = await self._send("POST", DATA_PRODUCTS_PATH, json=request.body)
        raw = _json_object(response, url=DATA_PRODUCTS_PATH, endpoint=self._endpoint)
        ref = self._identity_from_response(raw)

        await _logger.ainfo(
            "qlik.write.create.ok",
            endpoint=self._endpoint,
            space_id=self._space_id,
            native_key=ref.native_key,
            dataset_ids=len(request.body.get("datasetIds", [])),
            key_contacts=len(request.body.get("keyContacts", [])),
            skipped_fields=request.skipped_fields,
            activated=raw.get("activated"),
        )
        return WriteResult.created(
            ref,
            source_revision=response.headers.get("etag"),
            written_fields=request.written_fields,
            skipped_fields=request.skipped_fields,
            detail=request.detail,
        )

    # -- update (T3.5) — deliberately not defined yet; see the module docstring. --------

    # -- payload construction ----------------------------------------------------------

    async def _build_create_body(
        self,
        product: DataProduct,
        *,
        name: str,
        dataset_names: Mapping[uuid.UUID, str] | None,
        api_consumable_refs: Sequence[uuid.UUID] | None,
    ) -> _CreateRequest:
        """Build the documented POST body, reporting everything that did not fit in it.

        Optional keys are omitted rather than sent empty: at create the object does not
        exist yet, so an omitted array and an empty array are indistinguishable at the
        target, and omitting keeps the request to exactly what the neutral entity
        actually said.
        """
        request = _CreateRequest(body={"name": name, "spaceId": self._space_id})
        request.wrote("name")

        description = _text_value(product.description)
        if description is not None:
            request.body["description"] = description
            request.wrote("description")

        readme = _text_value(product.documentation)
        if readme is not None:
            request.body["readMe"] = readme
            request.wrote("documentation")

        tags = _tag_values(product.tags)
        if tags:
            request.body["tags"] = tags
            request.wrote("tags")

        await self._apply_datasets(
            product,
            request,
            dataset_names=dataset_names,
            api_consumable_refs=api_consumable_refs,
        )
        await self._apply_owners(product, request)
        self._apply_placement(product, request)
        _apply_glossary(product, request)
        _apply_status(product, request)
        return request

    async def _apply_datasets(
        self,
        product: DataProduct,
        request: _CreateRequest,
        *,
        dataset_names: Mapping[uuid.UUID, str] | None,
        api_consumable_refs: Sequence[uuid.UUID] | None,
    ) -> None:
        """``datasetIds`` (D2) plus the ``apiConsumableDatasetIds`` subset rule."""
        resolution = await self._resolve_datasets(product, dataset_names=dataset_names)

        # Two neutral members can legitimately resolve to the same Qlik dataset (two
        # source tables matched to one dataset by name); RS-02 documents datasetIds as a
        # unique-string array, so collapse rather than send a duplicate.
        dataset_ids = _dedupe(resolution.dataset_ids)
        sent_ids = dataset_ids[:MAX_DATASET_IDS]
        over_cap = len(dataset_ids) - len(sent_ids)

        if sent_ids:
            request.body["datasetIds"] = sent_ids
            request.wrote("dataset_refs")
        if resolution.unresolved:
            request.skip(
                "dataset_refs",
                _unresolved_datasets_note(resolution, total=len(product.dataset_refs)),
            )
            await _logger.awarning(
                "qlik.write.create.datasets_unresolved",
                endpoint=self._endpoint,
                space_id=self._space_id,
                unresolved=len(resolution.unresolved),
                requested=len(product.dataset_refs),
            )
        if over_cap:
            request.skip(
                "dataset_refs",
                f"{over_cap} resolved dataset member(s) beyond Qlik's "
                f"{MAX_DATASET_IDS}-item datasetIds cap were omitted",
            )

        if api_consumable_refs is None:
            return
        sent_set = set(sent_ids)
        requested = _dedupe(resolution.subset_for(api_consumable_refs))
        consumable = [dataset_id for dataset_id in requested if dataset_id in sent_set]
        if consumable:
            request.body["apiConsumableDatasetIds"] = consumable
        dropped = len(list(api_consumable_refs)) - len(consumable)
        if dropped > 0:
            request.skip(
                "dataset_refs",
                f"{dropped} requested apiConsumableDatasetIds entr(ies) were dropped to keep "
                "the list a subset of the datasetIds actually sent",
            )

    async def _resolve_datasets(
        self,
        product: DataProduct,
        *,
        dataset_names: Mapping[uuid.UUID, str] | None,
    ) -> DatasetResolution:
        """Hand every member to the resolver (D2) — the only path to a ``datasetIds`` value.

        A member whose display name is unknown is still offered, with an empty name, so
        tier 1 (the IdentityMap) gets its chance; an empty name can never match a real
        Qlik item, so such a member simply falls through to "unresolved" instead of being
        silently skipped here.
        """
        members: list[DatasetMember] = []
        for neutral_id in product.dataset_refs:
            name = None if dataset_names is None else dataset_names.get(neutral_id)
            if name is None:
                name = await self._dataset_name_lookup(neutral_id)
            members.append(DatasetMember(neutral_id=neutral_id, name=name or ""))
        if not members:
            return DatasetResolution()
        return await self._resolver.resolve_datasets(members)

    async def _apply_owners(self, product: DataProduct, request: _CreateRequest) -> None:
        """``keyContacts`` (D3): resolved Qlik ``userId``s only, one entry per user."""
        if not product.owners:
            return
        resolution: OwnerResolution = await self._resolver.resolve_owners(product.owners)
        if resolution.key_contacts:
            request.body["keyContacts"] = [
                contact.as_json() for contact in resolution.key_contacts
            ]
            request.wrote("owners")
        if resolution.unmatched:
            request.skip("owners", _unmatched_owners_note(resolution, total=len(product.owners)))
            await _logger.awarning(
                "qlik.write.create.owners_unmatched",
                endpoint=self._endpoint,
                unmatched=len(resolution.unmatched),
                requested=len(product.owners),
            )

    def _apply_placement(self, product: DataProduct, request: _CreateRequest) -> None:
        """``spaceId`` is the configured target space, always (module docstring, point 1)."""
        if product.placement is None:
            return
        if product.placement == self._space_id:
            request.wrote("placement")
            return
        request.skip(
            "placement",
            f"placement {product.placement!r} was ignored: data products are created in the "
            f"configured Qlik target space {self._space_id!r}",
        )

    # -- HTTP + response mapping --------------------------------------------------------

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One request, with T3.1's error classification and nothing invented on top.

        The single seam every write in this module goes through. See the module docstring
        for the one gap T3.5 must close here (HTTP 412 ->
        :class:`~qlabs_catalog_sync_sdk.exceptions.ConflictError`).
        """
        try:
            return await self._http.request(method, url, **kwargs)
        except httpx.HTTPStatusError as exc:
            raise classify_response_error(
                exc, endpoint=self._endpoint, entity_type=EntityType.DATA_PRODUCT.value
            ) from exc
        except httpx.TransportError as exc:
            raise classify_transport_error(
                exc, endpoint=self._endpoint, entity_type=EntityType.DATA_PRODUCT.value
            ) from exc

    def _identity_from_response(self, raw: Mapping[str, Any]) -> IdentityRef:
        """The created product's identity, exactly as ``read.py`` shapes it.

        ``native_key`` is ``id`` and ``qri``/``mainId`` ride along as secondary keys —
        identical to ``read._map_data_product``, so a ref minted by a create and a ref
        minted by a read of the same product compare equal.
        """
        native_id = raw.get("id")
        if not isinstance(native_id, str) or not native_id:
            raise ConnectorError(
                "Qlik data-product create response carries no usable 'id'",
                endpoint=self._endpoint,
                entity_type=EntityType.DATA_PRODUCT.value,
            )
        return IdentityRef(
            endpoint=self._endpoint,
            entity_type=EntityType.DATA_PRODUCT,
            native_key=native_id,
            tenant_id=self._tenant_id,
            secondary_keys=_secondary_keys(qri=raw.get("qri"), mainId=raw.get("mainId")),
        )

    # -- capability gate ------------------------------------------------------------------

    def _ensure_creatable(self, entity_type: EntityType | None) -> None:
        """Refuse, before any HTTP call, anything the manifest does not declare creatable.

        Driven entirely by the manifest (module docstring, point 7): an unsupported entity
        is refused because it is unsupported (D5), and a supported-but-entirely-read-only
        entity is refused because it has no writable field at all (D2 — Qlik datasets are
        resolved, never created).
        """
        if entity_type is None:
            raise CapabilityError(
                "cannot create an object that declares no ENTITY_TYPE",
                endpoint=self._endpoint,
                operation="create",
            )
        capability = self._manifest.entity_capability(entity_type)
        if capability is None or not capability.supported:
            raise CapabilityError(
                f"connector {self._endpoint!r} does not support {entity_type.value!r} "
                "(glossary and category are out of the MVP — decision D5)",
                endpoint=self._endpoint,
                entity_type=entity_type.value,
                operation="create",
            )
        if not any(field_capability.is_writable for field_capability in capability.fields.values()):
            raise CapabilityError(
                f"connector {self._endpoint!r} declares every {entity_type.value!r} field "
                "read-only, so it can never create one (decision D2: Qlik datasets are "
                "resolved against the target space, never created)",
                endpoint=self._endpoint,
                entity_type=entity_type.value,
                capability_mode="ro",
                operation="create",
            )
        if entity_type is not EntityType.DATA_PRODUCT:
            raise CapabilityError(
                f"connector {self._endpoint!r} implements create only for "
                f"{EntityType.DATA_PRODUCT.value!r}, not {entity_type.value!r}",
                endpoint=self._endpoint,
                entity_type=entity_type.value,
                operation="create",
            )


def build_writer(
    http: HttpEndpoint,
    *,
    endpoint: str,
    tenant_id: str,
    space_id: str,
    dataset_identity_lookup: DatasetIdentityLookup,
    dataset_name_lookup: DatasetNameLookup | None = None,
    manifest: CapabilityManifest | None = None,
) -> QlikWriter:
    """Build a :class:`QlikWriter` and the
    :class:`~qlabs_connector_qlik.resolve.QlikReferenceResolver` it owns, in one call.

    This is what ``Connector.setup()`` calls: it keeps the resolver's construction (and
    therefore its cache lifetime, which ``resolve.py`` documents as "one connector
    instance") in exactly one place instead of duplicating it at every call site.
    """
    resolver = QlikReferenceResolver(
        http,
        endpoint=endpoint,
        space_id=space_id,
        dataset_identity_lookup=dataset_identity_lookup,
    )
    return QlikWriter(
        http,
        endpoint=endpoint,
        tenant_id=tenant_id,
        space_id=space_id,
        resolver=resolver,
        manifest=manifest,
        dataset_name_lookup=dataset_name_lookup,
    )


# --------------------------------------------------------------------------------------
# Neutral value -> Qlik wire value (T3.5 reuses these for its JSON Patch `value`s)
# --------------------------------------------------------------------------------------


def _required_name(product: DataProduct, *, endpoint: str) -> str:
    """Qlik's only required create field. Blank is refused here, not at the tenant.

    ``DataProduct.name`` is ``min_length=1`` in the neutral model, so an absent name can
    only reach here through ``model_construct`` or a whitespace-only string — both of
    which would otherwise create a nameless product in a customer's catalog.
    """
    name = getattr(product, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise ConnectorError(
            "a Qlik data product cannot be created without a name",
            endpoint=endpoint,
            entity_type=EntityType.DATA_PRODUCT.value,
        )
    return name


def _text_value(text: TextField | None) -> str | None:
    """A neutral text field's body, or ``None`` when the field was not reported at all."""
    return None if text is None else text.text


def _tag_values(tags: Sequence[Tag]) -> list[str]:
    """Neutral key/value tags flattened to Qlik's ``string[]`` (module docstring, point 4)."""
    return _dedupe(
        tag.key if tag.value is None else f"{tag.key}={tag.value}" for tag in tags
    )


# --------------------------------------------------------------------------------------
# Reporting helpers — the D2/D3/D5/D7 skip notes
# --------------------------------------------------------------------------------------


def _apply_glossary(product: DataProduct, request: _CreateRequest) -> None:
    """D5: ``glossaryIds`` is never sent, and saying so is the whole behavior."""
    if not product.glossary_term_refs:
        return
    request.skip(
        "glossary_term_refs",
        f"{len(product.glossary_term_refs)} glossary reference(s) were not written: "
        "glossary is out of the MVP (decision D5) and this connector has no resolver for "
        "Qlik glossary ids",
    )


def _apply_status(product: DataProduct, request: _CreateRequest) -> None:
    """D7: a created product starts deactivated, whatever the neutral status asked for."""
    if product.status is None or product.status is DataProductStatus.DRAFT:
        return
    request.skip(
        "status",
        f"status {product.status.value!r} was not applied: the product was created "
        "deactivated because activation is opt-in and off by default (decision D7)",
    )


def _unresolved_datasets_note(resolution: DatasetResolution, *, total: int) -> str:
    examples = [
        f"{item.name!r} ({item.reason.value})" for item in resolution.unresolved
    ]
    return (
        f"{len(resolution.unresolved)} of {total} dataset member(s) did not resolve to an "
        f"existing Qlik dataset in the target space and were omitted (decision D2): "
        f"{_summarize(examples)}"
    )


def _unmatched_owners_note(resolution: OwnerResolution, *, total: int) -> str:
    examples = [
        f"{_owner_label(item.party)} ({item.reason.value})" for item in resolution.unmatched
    ]
    return (
        f"{len(resolution.unmatched)} of {total} owner(s) did not match a Qlik user and were "
        f"dropped from keyContacts (decision D3): {_summarize(examples)}"
    )


def _owner_label(party: Party) -> str:
    """An identifying-but-not-personal label for the run report.

    Never the raw email address: ``resolve.py`` already logs only its domain because an
    email is personal data, and ``WriteResult.detail`` ends up in the same run report.
    """
    if party.display_name:
        return party.display_name
    if party.party_id:
        return party.party_id
    _, _, domain = (party.email or "").rpartition("@")
    return f"<owner@{domain}>" if domain else "<unidentified owner>"


def _summarize(examples: Sequence[str]) -> str:
    """At most :data:`_MAX_REPORTED_EXAMPLES` examples, then a count of the rest."""
    shown = list(examples[:_MAX_REPORTED_EXAMPLES])
    remaining = len(examples) - len(shown)
    if remaining > 0:
        shown.append(f"and {remaining} more")
    return ", ".join(shown)


# --------------------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------------------


def _in_report_order(names: Sequence[str]) -> list[str]:
    ordered = [name for name in _FIELD_REPORT_ORDER if name in names]
    ordered.extend(name for name in names if name not in _FIELD_REPORT_ORDER)
    return ordered


def _dedupe(values: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication of an iterable of strings."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _secondary_keys(**candidates: object) -> dict[str, str]:
    """Every non-empty string candidate, as ``IdentityRef.secondary_keys``.

    Deliberately a local copy of ``read.py``'s private helper of the same name rather than
    an import of it: ``read.py`` belongs to T3.3 and does not export it, and a two-line
    dict comprehension is not worth reaching across that ownership line for.
    """
    return {key: value for key, value in candidates.items() if isinstance(value, str) and value}


def _json_object(response: httpx.Response, *, url: str, endpoint: str) -> dict[str, Any]:
    body = response.json()
    if not isinstance(body, dict):
        raise ConnectorError(
            f"Qlik returned a non-object JSON body from {url}",
            endpoint=endpoint,
            entity_type=EntityType.DATA_PRODUCT.value,
        )
    return body
