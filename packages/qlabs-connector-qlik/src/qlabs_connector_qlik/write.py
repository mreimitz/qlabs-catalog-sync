"""Qlik write path — the only place in v1 that mutates a customer's catalog.

WP3 / T3.4: :meth:`QlikWriter.create` — turn a neutral
:class:`~qlabs_catalog_sync_sdk.models.DataProduct` into the documented
``POST /api/data-governance/data-products`` body (note: **no** ``/v1`` segment — that
family is ``/api/v1/...``; RS-02 ``qlik-catalog-api-reference.md`` section 3.1), send it,
and hand back a :class:`~qlabs_catalog_sync_sdk.contract.WriteResult` that reports every
reference the connector could not place.

WP3 / T3.5 (this task): :meth:`QlikWriter.update` — turn a
:class:`~qlabs_catalog_sync_sdk.models.FieldDiff` into
``PATCH /api/data-governance/data-products/{id}``, a JSON Patch array of
``op: "replace"`` operations against the closed eight-path enum, guarded with
``if-match`` and answered with ``204 No Content``. It reuses everything T3.4 left in
place: :func:`_text_value` / :func:`_tag_values` produce the ``value`` shapes ``/name``,
``/description``, ``/readMe`` and ``/tags`` want; :meth:`QlikWriter._resolve_datasets`
and :meth:`QlikReferenceResolver.resolve_owners` produce the full-replace ``/datasetIds``
and ``/keyContacts`` arrays; :data:`MAX_DATASET_IDS`, :func:`_dedupe`, the ``notes``
builders and the ``wrote``/``skip`` bookkeeping are unchanged.

Still to land in this file: **T3.7 — ``delete()`` and the lifecycle actions**
(activate/deactivate/move). Those live in their own ``lifecycle.py``, not here.

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

Update-path decisions (T3.5), for the same "no live tenant" reason:

9.  **The neutral -> native mapping is a closed table, and every field in a diff must be
    in it.** :data:`PATCH_PATH_FOR_FIELD` is the whole translation. A change whose field
    the manifest declares ``ro``/``na``, or whose native path is outside Qlik's
    eight-value enum, raises
    :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError` **before any request** —
    never a silent skip, and never a partial write of "the rest of the diff" that leaves
    the engine believing the whole diff landed. Two fields are deliberately out of the
    enum and therefore always refused here: ``placement`` (``/spaceId``, which is the
    ``/actions/move`` endpoint's job — T3.7) and ``status`` (Qlik activation, which is
    the ``/actions/activate`` endpoint's job — T3.7 plus the T7.4 opt-in, decision D7).
    Refusing them loudly is the point: the engine must route them to the right call, and
    the alternative — reporting them as "skipped" and returning success — would let a
    never-applied activation look synced forever.
10. **Arrays are full replace, and an array that resolved to nothing is not sent at
    all.** ``replace`` on ``/datasetIds``/``/keyContacts``/``/tags`` overwrites the whole
    array, which is exactly what ``FieldUpdateMode.REPLACE`` asks for. But D2/D3 mean a
    member or an owner can fail to resolve, and "omit the unresolved ones" reads very
    differently at update than at create: at create an omitted member simply never
    appears, while at update an omitted member is *removed from the customer's product*.
    So: a partial resolution is sent (the members that dropped out do not exist in Qlik
    at all, so they could not have been in the target's list either) and reported as
    both written and skipped, exactly as at create; but a change that asked for at least
    one member and resolved **none** of them omits the operation entirely and reports it
    — sending ``[]`` there would assert "this product has no datasets", which the source
    never said, and would be a delete in all but name (decision D4). A diff whose desired
    value is genuinely the empty array is a different thing and *is* sent.
11. **Batching: this connector never splits a diff, it refuses one that cannot fit.**
    Qlik caps a data-product PATCH at 8 operations. The two safe-looking options are both
    unsafe. Truncating loses data silently. Splitting cannot work: a
    :class:`~qlabs_catalog_sync_sdk.models.FieldDiff` carries exactly one
    ``expected_revision``, and the first request invalidates it — so the second batch
    either ships a revision that is stale by construction (a guaranteed 412) or drops
    ``if-match`` and becomes a blind, unguarded overwrite; and because Qlik offers no
    transaction across two PATCHes, a failure between them leaves the product in a state
    neither the source nor the target ever described. So a diff that would need more than
    :attr:`EntityCapability.max_update_operations` operations raises ``CapabilityError``
    **before any request** — the plan was not applicable, which is exactly what that
    exception means, and the engine can see it coming from ``DiffPlan.request_count``.
    In this build the refusal is unreachable: the neutral model can reach only seven of
    the eight paths (``/glossaryIds`` is never written, D5), so the maximum a diff can
    ask for is 7. The guard exists so that widening the mapping later fails loudly
    instead of quietly splitting.
12. **A 412 buys exactly one re-read / re-diff / re-apply, and "re-diff" means what a
    connector can honestly re-derive.** The engine owns the last-known target envelopes;
    this connector does not, so it cannot recompute the engine's minimal diff. What it
    *can* do is exact and sufficient: every one of the eight paths takes a whole value,
    so the diff already states the complete desired value for each field it touches.
    On a 412 the connector re-``GET``\\ s the product, drops every operation whose
    desired value the product **already** carries (that is the honest re-diff: an
    operation that would change nothing), and re-applies what is left against the
    **fresh** ETag from that re-read. Comparison is exact equality and fails toward
    re-sending — a value it cannot prove already matches is sent again, because sending
    an identical replace is harmless while dropping a needed one is a lost write. What it
    deliberately does **not** do: widen the field set (a field that moved at the target
    but is not in the diff is never touched), or decide conflict policy — v1's
    manual-edit policy (source-wins, configurable) lives in the engine, so a field that
    still differs is simply written, as the engine asked. If the re-read yields no ETag
    the retry is abandoned and the :class:`~qlabs_catalog_sync_sdk.exceptions.ConflictError`
    propagates: an endpoint that just enforced ``if-match`` and then hands back nothing to
    match on is a contradiction, and an unguarded resend into a customer's catalog is not
    the way to resolve it. A second 412 propagates too — one cycle, never a retry loop —
    so the engine, which holds the state this connector does not, decides.
13. **``if-match`` is sent when the diff carries a revision, and its absence is not
    fatal.** RS-02 documents ``if-match`` for glossary/category writes but says nothing
    about whether the data-products PATCH honors ETags at all (TENANT_UNVERIFIED, already
    registered). Sending it costs nothing if ignored. A diff with no
    ``expected_revision`` — the diff engine leaves it ``None`` when the target read
    carried no ETag, or when the envelopes disagreed — is still applied, unguarded, with
    a ``qlik.write.update.unguarded`` warning; refusing instead would make ``update()``
    unusable on a tenant that returns no ETags at all, which is a real possibility here.
14. **TENANT_UNVERIFIED: the PATCH request's ``Content-Type``.** RS-02 says the endpoint
    "uses JSON Patch (array of operations)" but never names a media type for it, and the
    documented create call on the same family is ``application/json``. This module sends
    ``application/json`` (what ``httpx``'s ``json=`` produces) rather than guessing at
    ``application/json-patch+json``; if a tenant probe shows the endpoint requires the
    latter, this is a one-line change and the only place it would need making.
15. **``/apiConsumableDatasetIds`` is opt-in at update too, and only alongside
    ``/datasetIds``.** Same reasoning as create (point 2): no neutral field sources it, so
    it is never inferred. It is additionally constrained here, because the server rule is
    that it must be a subset of ``datasetIds`` *after* the patch: it is only accepted in
    the same call that replaces ``/datasetIds``, so the subset can be enforced against the
    ids actually being sent rather than against target state this connector has not read.
    Asking for it on its own raises ``CapabilityError``.

Every request in this module goes through :meth:`QlikWriter._send`, which reuses T3.1's
``auth.classify_response_error``/``auth.classify_transport_error`` — a 403 (creation
requires create permission in the target space, RS-02 section 5) becomes
:class:`~qlabs_catalog_sync_sdk.exceptions.AuthError`; a 429 (Tier 2, 100 req/min) or 5xx
is retried by ``HttpEndpoint`` and only becomes
:class:`~qlabs_catalog_sync_sdk.exceptions.TransientError` once those retries are
exhausted. No second classification is invented here — with exactly one addition T3.5
owns: **HTTP 412**. ``auth.classify_response_error`` has no 412 branch (it lands on the
generic :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError`) because nothing in
the create path can produce one, and ``auth.py`` belongs to T3.1. :meth:`QlikWriter._send`
therefore intercepts 412 *before* delegating and raises
:class:`~qlabs_catalog_sync_sdk.exceptions.ConflictError` itself, carrying the
``expected_revision`` it sent and the ``actual_revision`` the tenant returned. That is a
local override, not a second classification scheme: every other status still goes to
``auth.py`` untouched. If ``auth.py`` ever grows a 412 branch, this interception becomes
redundant rather than conflicting.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, TypeAlias, TypeVar

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from qlabs_catalog_sync_sdk.contract import (
    EntityType,
    IdentityRef,
    NeutralEntity,
    WriteResult,
)
from qlabs_catalog_sync_sdk.exceptions import CapabilityError, ConflictError, ConnectorError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.manifest import CapabilityManifest, EntityCapability
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    DataProductStatus,
    FieldChange,
    FieldDiff,
    Party,
    Tag,
    TextField,
)

from .auth import classify_response_error, classify_transport_error
from .manifest import QLIK_DATA_PRODUCT_PATCH_PATHS, qlik_capability_manifest
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
    "MAX_UPDATE_OPERATIONS",
    "PATCH_PATH_FOR_FIELD",
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

#: Server-side cap on JSON Patch operations in one data-product PATCH (RS-02
#: ``qlik-two-way-sync-readiness.md`` section 2: "Max 8 operations per request (matches
#: the 8 writable fields)"). Used only when the manifest in hand declares no
#: ``max_update_operations`` of its own — the manifest is the source of truth, this is
#: the documented wire fact behind it.
MAX_UPDATE_OPERATIONS: Final = 8

#: Neutral field name -> the JSON Pointer a ``replace`` operation writes it through.
#: The whole of this connector's update translation, and deliberately a closed table:
#: :meth:`QlikWriter.update` refuses any field that is not a key here (module docstring,
#: point 9). Two entries map to paths Qlik's PATCH enum does **not** contain, which is
#: the point — ``placement``/``spaceId`` moves through ``/actions/move`` and is declared
#: ``ro`` in the manifest, so naming its native path lets the refusal say *why* rather
#: than "unknown field". ``status`` has no native path at all (Qlik activation is an
#: action, not a field) and so is absent from this table entirely.
PATCH_PATH_FOR_FIELD: Final[dict[str, str]] = {
    "name": "/name",
    "description": "/description",
    "documentation": "/readMe",
    "tags": "/tags",
    "dataset_refs": "/datasetIds",
    "owners": "/keyContacts",
    "glossary_term_refs": "/glossaryIds",
    "placement": "/spaceId",  # NOT in Qlik's PATCH enum — always refused (D-note 9)
}

#: The one neutral field this connector can write, but not through the PATCH endpoint.
#: Kept apart from :data:`PATCH_PATH_FOR_FIELD` so the refusal message can name the
#: endpoint that actually owns it instead of pretending the field is unknown.
_FIELD_WRITTEN_BY_ACTION: Final[dict[str, str]] = {
    "status": (
        "Qlik activation is a lifecycle action (POST .../actions/activate|deactivate), "
        "not one of the eight PATCH paths — and activation is opt-in and off by default "
        "(decision D7)"
    ),
    "placement": (
        "a Qlik data product changes space through POST .../actions/move, not through "
        "the PATCH path enum"
    ),
}

#: HTTP 412 Precondition Failed — the ``if-match`` revision no longer matched. The one
#: status ``auth.classify_response_error`` (T3.1) does not classify, because nothing on
#: the create path can produce it; :meth:`QlikWriter._send` maps it locally.
_PRECONDITION_FAILED: Final = 412

#: ``apiConsumableDatasetIds`` has no neutral field, so it never appears in a diff; it is
#: only ever requested explicitly by a caller (module docstring, points 2 and 15).
_API_CONSUMABLE_PATH: Final = "/apiConsumableDatasetIds"

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
class _WriteReport:
    """The honest bookkeeping that becomes the ``WriteResult``, shared by both writes.

    Neither create nor update may report a field as written without it having reached
    the wire, and neither may drop a field without saying so — see the module
    docstring's "how a dropped reference reaches the caller".
    """

    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def wrote(self, *fields: str) -> None:
        for name in fields:
            if name not in self.written:
                self.written.append(name)

    def unwrite(self, name: str) -> None:
        """Take a field back out of ``written``.

        Used only by the 412 recovery path: an operation the target turns out to already
        carry is dropped from the retry, and this call keeps ``written_fields`` meaning
        strictly "fields *this call* wrote" rather than "fields that now happen to
        match" (module docstring, point 12).
        """
        if name in self.written:
            self.written.remove(name)

    def skip(self, name: str, note: str | None = None) -> None:
        if name not in self.skipped:
            self.skipped.append(name)
        if note is not None:
            self.notes.append(note)

    def note(self, note: str) -> None:
        """Add a ``detail`` sentence that is not tied to a dropped field."""
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


@dataclass(slots=True)
class _CreateRequest(_WriteReport):
    """The POST body plus the honest bookkeeping that becomes the ``WriteResult``."""

    body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _PatchOperation:
    """One JSON Patch ``replace``, plus the neutral field it came from.

    ``field`` never reaches the wire — it is what lets the 412 recovery path and the
    ``WriteResult`` talk about *fields* while the request talks about *paths*.
    """

    field: str
    path: str
    value: Any

    def as_json(self) -> dict[str, Any]:
        """The exact operation object Qlik's JSON Patch body wants — ``replace`` only."""
        return {"op": "replace", "path": self.path, "value": self.value}


@dataclass(slots=True)
class _UpdateRequest(_WriteReport):
    """The JSON Patch operations plus the same bookkeeping the create path uses."""

    operations: list[_PatchOperation] = field(default_factory=list)

    def add(self, field_name: str, path: str, value: Any) -> None:
        self.operations.append(_PatchOperation(field=field_name, path=path, value=value))
        self.wrote(field_name)

    @property
    def body(self) -> list[dict[str, Any]]:
        """The request body: a JSON Patch array, in the order the diff listed its fields."""
        return [operation.as_json() for operation in self.operations]

    @property
    def paths(self) -> list[str]:
        return [operation.path for operation in self.operations]


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

    # -- update (T3.5) ------------------------------------------------------------------

    async def update(
        self,
        ref: IdentityRef,
        diff: FieldDiff,
        *,
        dataset_names: Mapping[uuid.UUID, str] | None = None,
        api_consumable_refs: Sequence[uuid.UUID] | None = None,
    ) -> WriteResult:
        """``PATCH /api/data-governance/data-products/{id}`` from a neutral field diff.

        Emits a JSON Patch array of ``op: "replace"`` operations — never ``add``, never
        ``remove`` — against the closed eight-path enum, sends ``diff.expected_revision``
        as ``if-match``, and expects ``204 No Content``. Array paths are full replace, so
        one changed tag sends the whole ``tags`` array; that is what the diff's
        :attr:`~qlabs_catalog_sync_sdk.models.FieldUpdateMode.REPLACE` intent already
        says, and this method honors it rather than re-deriving it.

        ``dataset_names`` and ``api_consumable_refs`` mean exactly what they mean on
        :meth:`create`, with one extra rule for the latter: it is only accepted in a call
        that also replaces ``dataset_refs``, so Qlik's "subset of ``datasetIds``" rule is
        enforceable against the ids actually being sent (module docstring, point 15).

        Returns :attr:`~qlabs_catalog_sync_sdk.contract.WriteOutcome.NO_OP` **without
        issuing any request** for an empty diff — the engine's idempotency claim rests on
        that — and also when every operation had to be dropped to avoid emptying an array
        on a resolution failure (module docstring, point 10).

        Raises, all before any request:
        :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError` for a field the
        manifest declares ``ro``/``na``, for a field whose native path is outside the
        eight-value enum (``status`` and ``placement`` — both are other endpoints' work),
        and for a diff that would need more than the declared operation cap.
        :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError` for a change whose
        value cannot be read as the neutral type it claims to be, or that would blank the
        product's required ``name``. A rejected ``if-match`` raises
        :class:`~qlabs_catalog_sync_sdk.exceptions.ConflictError` — but only after one
        re-read / re-diff / re-apply cycle has failed too (module docstring, point 12).
        """
        self._ensure_updatable(ref, diff, api_consumable_refs=api_consumable_refs)
        native_id = ref.secondary_keys.get("id", ref.native_key)
        url = f"{DATA_PRODUCTS_PATH}/{native_id}"

        if not diff.changes:
            # Belt and braces: an empty diff also produces zero operations below and
            # would reach the same NO_OP, but the guarantee the engine's idempotency
            # claim rests on ("no diff, no request") should not depend on a downstream
            # branch staying that way.
            await _logger.ainfo(
                "qlik.write.update.no_op",
                endpoint=self._endpoint,
                native_key=ref.native_key,
                reason="empty_diff",
            )
            return WriteResult.no_op(ref, source_revision=diff.expected_revision)

        request = await self._build_update_request(
            diff, dataset_names=dataset_names, api_consumable_refs=api_consumable_refs
        )
        if not request.operations:
            # Every operation was dropped rather than empty an array on a resolution
            # failure (module docstring, point 10). Nothing was written and nothing was
            # sent — but the reasons still reach the run report.
            await _logger.awarning(
                "qlik.write.update.no_op",
                endpoint=self._endpoint,
                native_key=ref.native_key,
                reason="nothing_applicable",
                skipped_fields=request.skipped_fields,
            )
            return WriteResult.no_op(
                ref,
                source_revision=diff.expected_revision,
                skipped_fields=request.skipped_fields,
                detail=request.detail,
            )

        expected_revision = diff.expected_revision
        if expected_revision is None:
            # Module docstring, point 13 — visible, not fatal.
            await _logger.awarning(
                "qlik.write.update.unguarded",
                endpoint=self._endpoint,
                native_key=ref.native_key,
                paths=request.paths,
            )

        try:
            response = await self._send_patch(
                url, request.body, expected_revision=expected_revision
            )
        except ConflictError as conflict:
            return await self._recover_from_conflict(ref, url, request, conflict)

        await _logger.ainfo(
            "qlik.write.update.ok",
            endpoint=self._endpoint,
            native_key=ref.native_key,
            paths=request.paths,
            operations=len(request.operations),
            guarded=expected_revision is not None,
            skipped_fields=request.skipped_fields,
            status_code=response.status_code,
        )
        return WriteResult.updated(
            ref,
            source_revision=response.headers.get("etag"),
            written_fields=request.written_fields,
            skipped_fields=request.skipped_fields,
            detail=request.detail,
        )

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
        resolution = await self._resolve_datasets(
            product.dataset_refs, dataset_names=dataset_names
        )

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
        dataset_refs: Sequence[uuid.UUID],
        *,
        dataset_names: Mapping[uuid.UUID, str] | None,
    ) -> DatasetResolution:
        """Hand every member to the resolver (D2) — the only path to a ``datasetIds`` value.

        A member whose display name is unknown is still offered, with an empty name, so
        tier 1 (the IdentityMap) gets its chance; an empty name can never match a real
        Qlik item, so such a member simply falls through to "unresolved" instead of being
        silently skipped here.

        Takes the bare member ids rather than the whole product so ``update()`` — whose
        desired members arrive as a JSON array on a
        :class:`~qlabs_catalog_sync_sdk.models.FieldChange`, not as an entity — reuses
        this exact path (and therefore the resolver's caches) instead of a second one.
        """
        members: list[DatasetMember] = []
        for neutral_id in dataset_refs:
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

    # -- JSON Patch construction (T3.5) --------------------------------------------------

    async def _build_update_request(
        self,
        diff: FieldDiff,
        *,
        dataset_names: Mapping[uuid.UUID, str] | None,
        api_consumable_refs: Sequence[uuid.UUID] | None,
    ) -> _UpdateRequest:
        """Turn every change into one ``replace`` operation, in the diff's own order.

        Every field reaching here has already passed :meth:`_ensure_updatable`, so it is
        declared writable and maps into Qlik's path enum. What can still go wrong is the
        *value*: a change whose JSON cannot be read as the neutral type it claims raises
        :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError`, and an array whose
        members or owners resolve to nothing drops its operation rather than emptying the
        target's array (module docstring, point 10).
        """
        request = _UpdateRequest()
        for change in diff.changes:
            path = PATCH_PATH_FOR_FIELD[change.field]
            if change.field == "name":
                request.add(change.field, path, _patch_name(change, endpoint=self._endpoint))
            elif change.field in ("description", "documentation"):
                request.add(change.field, path, _patch_text(change, endpoint=self._endpoint))
            elif change.field == "tags":
                request.add(change.field, path, _patch_tags(change, endpoint=self._endpoint))
            elif change.field == "dataset_refs":
                await self._apply_dataset_update(
                    change,
                    request,
                    dataset_names=dataset_names,
                    api_consumable_refs=api_consumable_refs,
                )
            elif change.field == "owners":
                await self._apply_owner_update(change, request)
            else:
                # Only reachable through an injected manifest that declares a field
                # writable which this connector has no way to write — `glossary_term_refs`
                # (D5: no resolver for Qlik glossary ids exists in v1) being the real
                # case. Refuse rather than invent a value.
                raise CapabilityError(
                    f"connector {self._endpoint!r} declares no way to build a Qlik "
                    f"{path!r} value from the neutral field {change.field!r}"
                    + (
                        " (glossary is out of the MVP — decision D5)"
                        if change.field == "glossary_term_refs"
                        else ""
                    ),
                    endpoint=self._endpoint,
                    entity_type=EntityType.DATA_PRODUCT.value,
                    field=change.field,
                    operation="update",
                )
        return request

    async def _apply_dataset_update(
        self,
        change: FieldChange,
        request: _UpdateRequest,
        *,
        dataset_names: Mapping[uuid.UUID, str] | None,
        api_consumable_refs: Sequence[uuid.UUID] | None,
    ) -> None:
        """``/datasetIds`` as a full replace (D2), plus the opt-in consumable subset."""
        requested = _patch_dataset_refs(change, endpoint=self._endpoint)
        resolution = await self._resolve_datasets(requested, dataset_names=dataset_names)
        dataset_ids = _dedupe(resolution.dataset_ids)
        sent_ids = dataset_ids[:MAX_DATASET_IDS]
        over_cap = len(dataset_ids) - len(sent_ids)

        if requested and not sent_ids:
            # Sending [] here would assert "this product has no datasets", which the
            # source never said (module docstring, point 10).
            request.skip(
                "dataset_refs", _no_members_resolved_note(resolution, total=len(requested))
            )
            await _logger.awarning(
                "qlik.write.update.datasets_all_unresolved",
                endpoint=self._endpoint,
                space_id=self._space_id,
                requested=len(requested),
            )
            # No `/datasetIds` operation also means no `/apiConsumableDatasetIds` one: a
            # subset can only be enforced against ids this request is actually sending.
            return

        request.add("dataset_refs", PATCH_PATH_FOR_FIELD["dataset_refs"], sent_ids)
        if resolution.unresolved:
            request.skip(
                "dataset_refs", _unresolved_datasets_note(resolution, total=len(requested))
            )
            await _logger.awarning(
                "qlik.write.update.datasets_unresolved",
                endpoint=self._endpoint,
                space_id=self._space_id,
                unresolved=len(resolution.unresolved),
                requested=len(requested),
            )
        if over_cap:
            request.skip(
                "dataset_refs",
                f"{over_cap} resolved dataset member(s) beyond Qlik's "
                f"{MAX_DATASET_IDS}-item datasetIds cap were omitted",
            )

        if api_consumable_refs is None:
            return
        sent = set(sent_ids)
        wanted = _dedupe(resolution.subset_for(api_consumable_refs))
        consumable = [dataset_id for dataset_id in wanted if dataset_id in sent]
        request.add("dataset_refs", _API_CONSUMABLE_PATH, consumable)
        dropped = len(list(api_consumable_refs)) - len(consumable)
        if dropped > 0:
            request.skip(
                "dataset_refs",
                f"{dropped} requested apiConsumableDatasetIds entr(ies) were dropped to keep "
                "the list a subset of the datasetIds actually sent",
            )

    async def _apply_owner_update(self, change: FieldChange, request: _UpdateRequest) -> None:
        """``/keyContacts`` as a full replace (D3): resolved Qlik ``userId``\\ s only."""
        parties = _patch_parties(change, endpoint=self._endpoint)
        resolution: OwnerResolution = await self._resolver.resolve_owners(parties)

        if parties and not resolution.key_contacts:
            # Same rule as datasets: a lookup that matched nobody must not be written as
            # "this product has no contacts" (module docstring, point 10).
            request.skip("owners", _no_owners_matched_note(resolution, total=len(parties)))
            await _logger.awarning(
                "qlik.write.update.owners_all_unmatched",
                endpoint=self._endpoint,
                requested=len(parties),
            )
            return

        request.add(
            "owners",
            PATCH_PATH_FOR_FIELD["owners"],
            [contact.as_json() for contact in resolution.key_contacts],
        )
        if resolution.unmatched:
            request.skip("owners", _unmatched_owners_note(resolution, total=len(parties)))
            await _logger.awarning(
                "qlik.write.update.owners_unmatched",
                endpoint=self._endpoint,
                unmatched=len(resolution.unmatched),
                requested=len(parties),
            )

    # -- HTTP + response mapping --------------------------------------------------------

    async def _send(
        self,
        method: str,
        url: str,
        *,
        expected_revision: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """One request, with T3.1's error classification and one local addition on top.

        The single seam every write in this module goes through. ``auth.py``'s
        :func:`~qlabs_connector_qlik.auth.classify_response_error` has no **412** branch
        (nothing on the create path can produce one) and belongs to T3.1, so this method
        intercepts 412 before delegating and raises
        :class:`~qlabs_catalog_sync_sdk.exceptions.ConflictError` itself — carrying the
        ``if-match`` value it sent and whatever revision the tenant returned. Every other
        status still goes to ``auth.py`` untouched (module docstring, last paragraph).
        """
        try:
            return await self._http.request(method, url, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == _PRECONDITION_FAILED:
                raise _conflict_error(
                    exc, endpoint=self._endpoint, expected_revision=expected_revision
                ) from exc
            raise classify_response_error(
                exc, endpoint=self._endpoint, entity_type=EntityType.DATA_PRODUCT.value
            ) from exc
        except httpx.TransportError as exc:
            raise classify_transport_error(
                exc, endpoint=self._endpoint, entity_type=EntityType.DATA_PRODUCT.value
            ) from exc

    async def _send_patch(
        self,
        url: str,
        body: list[dict[str, Any]],
        *,
        expected_revision: str | None,
    ) -> httpx.Response:
        """The one PATCH shape this module ever sends: a JSON Patch array plus ``if-match``.

        ``if-match`` is omitted entirely when there is no revision to send, rather than
        sent empty — an empty precondition is not a weaker precondition, it is a
        malformed one (module docstring, point 13).
        """
        headers = {} if expected_revision is None else {"if-match": expected_revision}
        return await self._send(
            "PATCH", url, json=body, headers=headers, expected_revision=expected_revision
        )

    async def _recover_from_conflict(
        self,
        ref: IdentityRef,
        url: str,
        request: _UpdateRequest,
        conflict: ConflictError,
    ) -> WriteResult:
        """One re-read / re-diff / re-apply cycle after a 412 — never a retry loop.

        "Re-diff" is bounded by what a connector honestly holds (module docstring, point
        12): the diff already states the complete desired value for every path it
        touches, so the connector re-reads the product and drops exactly the operations
        the product **provably already carries**. It never widens the field set and never
        decides conflict policy. A second 412 propagates, and so does the original one if
        the re-read hands back no revision to guard the retry with.
        """
        await _logger.awarning(
            "qlik.write.update.conflict",
            endpoint=self._endpoint,
            native_key=ref.native_key,
            expected_revision=conflict.expected_revision,
            actual_revision=conflict.actual_revision,
            paths=request.paths,
        )

        response = await self._send("GET", url)
        current = _json_object(response, url=url, endpoint=self._endpoint)
        fresh_revision = response.headers.get("etag")
        if fresh_revision is None:
            await _logger.awarning(
                "qlik.write.update.conflict_unguardable",
                endpoint=self._endpoint,
                native_key=ref.native_key,
                reason="re-read returned no ETag to guard the retry with",
            )
            raise conflict

        remaining: list[_PatchOperation] = []
        already: list[_PatchOperation] = []
        for operation in request.operations:
            (already if _already_applied(operation, current) else remaining).append(operation)

        if already:
            still_written = {operation.field for operation in remaining}
            for operation in already:
                if operation.field not in still_written:
                    request.unwrite(operation.field)
            request.note(
                "a concurrent change had already applied "
                f"{', '.join(operation.path for operation in already)}, so "
                f"{'those paths were' if len(already) > 1 else 'that path was'} left out of "
                "the retry"
            )

        if not remaining:
            await _logger.ainfo(
                "qlik.write.update.no_op",
                endpoint=self._endpoint,
                native_key=ref.native_key,
                reason="conflict_already_applied",
            )
            return WriteResult.no_op(
                ref,
                source_revision=fresh_revision,
                skipped_fields=request.skipped_fields,
                detail=request.detail,
            )

        retry = await self._send_patch(
            url,
            [operation.as_json() for operation in remaining],
            expected_revision=fresh_revision,
        )
        await _logger.ainfo(
            "qlik.write.update.reapplied",
            endpoint=self._endpoint,
            native_key=ref.native_key,
            paths=[operation.path for operation in remaining],
            dropped_paths=[operation.path for operation in already],
            expected_revision=fresh_revision,
            status_code=retry.status_code,
        )
        return WriteResult.updated(
            ref,
            source_revision=retry.headers.get("etag"),
            written_fields=request.written_fields,
            skipped_fields=request.skipped_fields,
            detail=request.detail,
        )

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
        self._ensure_entity_writable(entity_type, operation="create")

    def _ensure_entity_writable(
        self, entity_type: EntityType | None, *, operation: str
    ) -> EntityCapability:
        """The entity-level half of the capability gate, shared by create and update.

        Returns the declared capability so the caller can go on to read
        ``allowed_update_paths``/``max_update_operations`` off the same object instead of
        looking it up twice.
        """
        if entity_type is None:
            raise CapabilityError(
                f"cannot {operation} an object that declares no ENTITY_TYPE",
                endpoint=self._endpoint,
                operation=operation,
            )
        capability = self._manifest.entity_capability(entity_type)
        if capability is None or not capability.supported:
            raise CapabilityError(
                f"connector {self._endpoint!r} does not support {entity_type.value!r} "
                "(glossary and category are out of the MVP — decision D5)",
                endpoint=self._endpoint,
                entity_type=entity_type.value,
                operation=operation,
            )
        if not any(field_capability.is_writable for field_capability in capability.fields.values()):
            raise CapabilityError(
                f"connector {self._endpoint!r} declares every {entity_type.value!r} field "
                f"read-only, so it can never {operation} one (decision D2: Qlik datasets are "
                "resolved against the target space, never created)",
                endpoint=self._endpoint,
                entity_type=entity_type.value,
                capability_mode="ro",
                operation=operation,
            )
        if entity_type is not EntityType.DATA_PRODUCT:
            raise CapabilityError(
                f"connector {self._endpoint!r} implements {operation} only for "
                f"{EntityType.DATA_PRODUCT.value!r}, not {entity_type.value!r}",
                endpoint=self._endpoint,
                entity_type=entity_type.value,
                operation=operation,
            )
        return capability

    def _ensure_updatable(
        self,
        ref: IdentityRef,
        diff: FieldDiff,
        *,
        api_consumable_refs: Sequence[uuid.UUID] | None,
    ) -> None:
        """Refuse, before any request at all, a diff this endpoint cannot apply.

        Three refusals, in this order (module docstring, points 9, 11 and 14): a field the
        manifest declares ``ro``/``na``; a field whose native path is outside Qlik's
        closed enum; and a request that would carry more operations than the endpoint
        accepts. All three are :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError`
        — the engine's plan was not applicable — and all three fire before any HTTP call,
        including before the resolver's read-only lookups.
        """
        if diff.entity_type is not ref.entity_type:
            raise CapabilityError(
                f"cannot apply a {diff.entity_type.value!r} diff to a "
                f"{ref.entity_type.value!r} reference",
                endpoint=self._endpoint,
                entity_type=ref.entity_type.value,
                operation="update",
            )
        capability = self._ensure_entity_writable(ref.entity_type, operation="update")
        allowed = frozenset(
            capability.allowed_update_paths
            if capability.allowed_update_paths is not None
            else QLIK_DATA_PRODUCT_PATCH_PATHS
        )

        operations = [self._patch_path_for(change.field, capability, allowed) for change in
                      diff.changes]
        if api_consumable_refs is not None:
            if "dataset_refs" not in diff.field_names:
                raise CapabilityError(
                    f"connector {self._endpoint!r} only replaces {_API_CONSUMABLE_PATH!r} in a "
                    "request that also replaces '/datasetIds': Qlik requires it to stay a "
                    "subset of datasetIds, and this connector never reads the product's "
                    "current members to guess that subset (decision D2)",
                    endpoint=self._endpoint,
                    entity_type=ref.entity_type.value,
                    field="dataset_refs",
                    operation="update",
                )
            if _API_CONSUMABLE_PATH not in allowed:
                raise CapabilityError(
                    f"connector {self._endpoint!r} cannot write {_API_CONSUMABLE_PATH!r}: it is "
                    "not in the manifest's declared update-path enum",
                    endpoint=self._endpoint,
                    entity_type=ref.entity_type.value,
                    operation="update",
                )
            operations.append(_API_CONSUMABLE_PATH)

        cap = (
            capability.max_update_operations
            if capability.max_update_operations is not None
            else MAX_UPDATE_OPERATIONS
        )
        if len(operations) > cap:
            raise CapabilityError(
                f"this diff needs {len(operations)} JSON Patch operations "
                f"({', '.join(operations)}) but Qlik accepts at most {cap} per data-product "
                "PATCH; connector "
                f"{self._endpoint!r} refuses rather than splitting the request, because a "
                "FieldDiff carries one expected_revision that the first request would "
                "invalidate — the second batch would be either unguarded or guaranteed to "
                "conflict, and Qlik offers no transaction across two PATCHes",
                endpoint=self._endpoint,
                entity_type=ref.entity_type.value,
                operation="update",
            )

    def _patch_path_for(
        self, field_name: str, capability: EntityCapability, allowed: frozenset[str]
    ) -> str:
        """The JSON Pointer ``field_name`` writes through, or a refusal explaining why not."""
        field_capability = capability.fields.get(field_name)
        if field_capability is None or not field_capability.is_writable:
            mode = "na" if field_capability is None else field_capability.mode.value
            declared = "does not declare" if field_capability is None else f"declares {mode!r}"
            raise CapabilityError(
                f"connector {self._endpoint!r} cannot update "
                f"{EntityType.DATA_PRODUCT.value!r} field {field_name!r}: the capability "
                f"manifest {declared} it",
                endpoint=self._endpoint,
                entity_type=EntityType.DATA_PRODUCT.value,
                field=field_name,
                capability_mode=mode,
                operation="update",
            )
        path = PATCH_PATH_FOR_FIELD.get(field_name)
        if path is not None and path in allowed:
            return path
        because = _FIELD_WRITTEN_BY_ACTION.get(field_name)
        native = "no native Qlik data-product path" if path is None else repr(path)
        raise CapabilityError(
            f"connector {self._endpoint!r} cannot update {field_name!r} through the "
            f"data-product JSON Patch endpoint: it maps to {native}, which is outside the "
            f"closed path enum ({', '.join(sorted(allowed))})"
            + (f" — {because}" if because else ""),
            endpoint=self._endpoint,
            entity_type=EntityType.DATA_PRODUCT.value,
            field=field_name,
            operation="update",
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
# FieldChange JSON -> Qlik JSON Patch `value` (T3.5)
#
# A `FieldChange` carries the *JSON projection* of a neutral value (`envelope.to_json_value`),
# not the typed model, so each converter below re-reads that projection through the neutral
# type it claims to be and then hands it to the same wire converters `create()` uses. That
# way one mapping decision (a valued tag becomes `key=value`, a TextField becomes its body)
# is implemented once and cannot drift between the two write paths. A projection that is not
# readable as its neutral type is a planning/serialization bug in the caller, so it raises
# `ConnectorError` rather than being coerced into something sendable.
# --------------------------------------------------------------------------------------


def _patch_name(change: FieldChange, *, endpoint: str) -> str:
    """``/name``: the product's only required field, so a blank one is refused, not sent.

    RS-02 allows ``null`` as a ``value`` for the string paths, but ``name`` is documented
    ``REQUIRED`` at create, and a sync that blanks the name of a product in a customer's
    catalog is the same failure :func:`_required_name` guards the create path against.
    """
    value = change.value
    if isinstance(value, str) and value.strip():
        return value
    raise ConnectorError(
        f"refusing to replace a Qlik data product's /name with {value!r}: name is the "
        "product's only required field and cannot be blanked",
        endpoint=endpoint,
        entity_type=EntityType.DATA_PRODUCT.value,
    )


def _patch_text(change: FieldChange, *, endpoint: str) -> str | None:
    """``/description`` and ``/readMe``: the text body, or ``null`` to clear the field.

    ``None`` is passed through rather than turned into ``""``: the diff engine's rule is
    that ``null`` is the source asserting the field is empty (absent fields never reach a
    diff at all), and RS-02 documents ``null`` as a legal value for these paths.
    """
    value = change.value
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return _text_value(_neutral(TextField, value, change=change, endpoint=endpoint))
    raise _unreadable_change(change, "a text field", endpoint=endpoint)


def _patch_tags(change: FieldChange, *, endpoint: str) -> list[str]:
    """``/tags``: the complete desired ``string[]``, flattened exactly as at create."""
    value = change.value
    if value is None:
        return []
    if not isinstance(value, list):
        raise _unreadable_change(change, "a list of tags", endpoint=endpoint)
    return _tag_values(
        [_neutral(Tag, item, change=change, endpoint=endpoint) for item in value]
    )


def _patch_parties(change: FieldChange, *, endpoint: str) -> list[Party]:
    """``/keyContacts``: the complete desired owner list, before D3 resolution."""
    value = change.value
    if value is None:
        return []
    if not isinstance(value, list):
        raise _unreadable_change(change, "a list of owners", endpoint=endpoint)
    return [_neutral(Party, item, change=change, endpoint=endpoint) for item in value]


def _patch_dataset_refs(change: FieldChange, *, endpoint: str) -> list[uuid.UUID]:
    """``/datasetIds``: the complete desired member list, before D2 resolution.

    The values are engine neutral ids (``DataProduct.dataset_refs`` is ``list[UUID]``),
    never Qlik ids — resolving them is :meth:`QlikWriter._resolve_datasets`' job.
    """
    value = change.value
    if value is None:
        return []
    if not isinstance(value, list):
        raise _unreadable_change(change, "a list of dataset ids", endpoint=endpoint)
    members: list[uuid.UUID] = []
    for item in value:
        if isinstance(item, uuid.UUID):
            members.append(item)
            continue
        if not isinstance(item, str):
            raise _unreadable_change(change, "a list of dataset ids", endpoint=endpoint)
        try:
            members.append(uuid.UUID(item))
        except ValueError as exc:
            raise ConnectorError(
                f"the {change.field!r} change carries {item!r}, which is not a neutral "
                "entity id; Qlik dataset ids are resolved from neutral ids, never sent raw",
                endpoint=endpoint,
                entity_type=EntityType.DATA_PRODUCT.value,
                cause=exc,
            ) from exc
    return members


_NeutralT = TypeVar("_NeutralT", bound=BaseModel)


def _neutral(
    model: type[_NeutralT], value: object, *, change: FieldChange, endpoint: str
) -> _NeutralT:
    """Read one JSON-projected neutral value back into its model, or refuse honestly."""
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise _unreadable_change(
            change, f"a {model.__name__}", endpoint=endpoint, cause=exc
        ) from exc


def _unreadable_change(
    change: FieldChange, expected: str, *, endpoint: str, cause: BaseException | None = None
) -> ConnectorError:
    return ConnectorError(
        f"the {change.field!r} change does not carry {expected}: a diff value must be the "
        "JSON projection of the neutral field it names",
        endpoint=endpoint,
        entity_type=EntityType.DATA_PRODUCT.value,
        cause=cause,
    )


def _conflict_error(
    exc: httpx.HTTPStatusError, *, endpoint: str, expected_revision: str | None
) -> ConflictError:
    """HTTP 412 -> :class:`ConflictError`, the one status ``auth.py`` does not classify."""
    return ConflictError(
        "Qlik rejected the data-product PATCH: the if-match revision no longer matches the "
        "product's current revision (HTTP 412)",
        endpoint=endpoint,
        entity_type=EntityType.DATA_PRODUCT.value,
        cause=exc,
        expected_revision=expected_revision,
        actual_revision=exc.response.headers.get("etag"),
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


def _no_members_resolved_note(resolution: DatasetResolution, *, total: int) -> str:
    """Why ``/datasetIds`` was left out of the PATCH entirely (module docstring, point 10)."""
    examples = [f"{item.name!r} ({item.reason.value})" for item in resolution.unresolved]
    return (
        f"none of the {total} desired dataset member(s) resolved to an existing Qlik dataset "
        f"in the target space, so datasetIds was left untouched rather than replaced with an "
        f"empty list (decisions D2 and D4): {_summarize(examples)}"
    )


def _no_owners_matched_note(resolution: OwnerResolution, *, total: int) -> str:
    """Why ``/keyContacts`` was left out of the PATCH entirely (module docstring, point 10)."""
    examples = [
        f"{_owner_label(item.party)} ({item.reason.value})" for item in resolution.unmatched
    ]
    return (
        f"none of the {total} desired owner(s) matched a Qlik user, so keyContacts was left "
        f"untouched rather than replaced with an empty list (decisions D3 and D4): "
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


def _already_applied(operation: _PatchOperation, current: Mapping[str, Any]) -> bool:
    """True only when the product **provably** already carries this operation's value.

    Deliberately exact equality, and deliberately biased toward re-sending: an absent key,
    a re-ordered array, a ``keyContacts`` entry the tenant decorated with extra keys — any
    value this cannot prove equal counts as "not applied". Re-sending an identical replace
    is harmless; dropping an operation that was actually still needed would silently lose
    a write (module docstring, point 12).
    """
    key = operation.path.removeprefix("/")
    if key not in current:
        return False
    return bool(current[key] == operation.value)


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
