"""Qlik reference resolution: datasets and users (decisions D2 and D3).

WP3 / T3.9 (Sonnet). Resolves the two Qlik-native references a data-product write
needs — ``datasetIds`` and ``keyContacts`` — without ever creating anything: this
module issues ``GET`` requests only. See ``decision-databricks-to-qlik-mvp.md``:

* **D2 — datasets are resolved, never created.** A product's member datasets are
  matched against datasets that already exist in the target space, tier 1 through the
  engine's IdentityMap, tier 2 by an exact name match scoped to that space. A member
  that resolves through neither tier is omitted from the payload and reported as
  unresolved — never invented.
* **D3 — owner emails resolve to Qlik ``userId``\\ s through the users API,** cached,
  best-effort, never an identity system. A miss is dropped and reported, exactly like
  an unresolved dataset.

The hard dependency rule (this connector depends only on the SDK plus ``httpx``, never
on the engine) is why "IdentityMap first" cannot mean importing the engine's
``qlabs_catalog_sync.identity.IdentityResolver`` — that class lives in the engine
package. Instead, :data:`DatasetIdentityLookup` names the exact seam: a plain async
callable, ``Callable[[UUID], Awaitable[str | None]]``, that the engine satisfies with a
thin closure over ``IdentityResolver.counterpart``:

.. code-block:: python

    async def dataset_identity_lookup(neutral_id: uuid.UUID) -> str | None:
        binding = await identity_resolver.counterpart(
            neutral_id, "qlik", EntityType.DATASET
        )
        if binding is None:
            return None
        identity = binding.identity
        return identity.secondary_keys.get("resourceId", identity.native_key)

Nothing in this module ever imports ``qlabs_catalog_sync`` — the callable is the only
thing crossing the boundary, and it carries no engine type across it (a bare ``UUID``
in, a bare ``str | None`` out).

**The seam runs both ways, and the other direction lives in ``read.py``.** The manifest
declares ``dataset_refs`` ``rw``, so a read has to be able to turn Qlik's native
``datasetIds`` back into the neutral ids this module turned them from. That is
``read.DatasetRefLookup`` — the exact mirror of :data:`DatasetIdentityLookup`, a bare
``str`` in and a bare ``uuid.UUID | None`` out, defaulting to "no answer" the same way
this one does. It is declared in ``read.py`` rather than here purely because *this*
module imports *that* one (``ITEMS_PATH``/``DEFAULT_PAGE_SIZE``), so declaring it here
would make the import a cycle. The engine satisfies it from the same IdentityMap, but
**not** with a mirror of the closure above: ``IdentityResolver.resolve_neutral_id``
looks a binding up by ``native_key`` (a dataset's is its ``secureQri``), whereas
``datasetIds`` carries the ``resourceId`` the forward closure hands out — so the reverse
direction needs an index over ``list_bindings(...)``'s ``identity.secondary_keys
["resourceId"]``, not a point read. ``read.py``'s point 4 states the reporting rule that
falls out of it; the connector-side wiring notes say what the orchestrator must build.

Design decisions worth being explicit about, since none of them can be checked against
a live tenant (decision D8 / agent-guide "no live tenants" rule):

1. **What a "member" is.** ``DataProduct.dataset_refs`` (SDK ``models.py``) is a bare
   ``list[UUID]`` — engine neutral ids with no display name attached. A name-match
   fallback needs a name to match on, so this module's public API takes
   :class:`DatasetMember` (``neutral_id`` + ``name``), not a raw ``UUID`` list; the
   caller (T3.4's ``write.py``) is expected to zip the product's ``dataset_refs``
   against the corresponding ``Dataset.name`` values it already holds (it reads those
   datasets to diff them before ever writing the product). Resolving that name is the
   caller's job; matching it against Qlik is this module's.
2. **The `datasetIds` wire value is `resourceId`, not the Items-API item id.** RS-02
   section 4.1: "Dataset id — key for dataset CRUD and for ``datasetIds`` /
   ``apiConsumableDatasetIds`` on a data product", and section 1.1: "``resourceId`` —
   the id of the underlying resource (e.g. the app id or dataset id)". ``read.py``'s
   own dataset ``IdentityRef`` already carries ``resourceId`` as a secondary key for
   exactly this reason. TENANT_UNVERIFIED: RS-02 never shows a ``datasetIds`` entry
   paired against its source item's ``resourceId`` in the same example, so this
   equivalence is inferred, not witnessed — worth a tenant probe before T3.4 ships.
3. **Name-match is exact, case-sensitive, and never guesses under ambiguity.** The
   Items API list endpoint is filterable by ``name`` (RS-02 section 3.5), but its
   match semantics (exact vs. substring/fuzzy) are undocumented — TENANT_UNVERIFIED.
   This module treats the server-side filter as a narrowing optimization only: results
   are always re-checked client-side for an exact string match before being counted.
   Zero exact matches -> :attr:`DatasetResolutionReason.NOT_FOUND`. Two or more ->
   :attr:`DatasetResolutionReason.AMBIGUOUS`, deliberately treated the same as
   not-found (omitted, reported, never guessed): D2's whole point is that writing the
   wrong dataset into a customer's product is worse than writing none.
4. **The users-API filter syntax is inferred, not confirmed for this endpoint.**
   RS-02 (this task's own citation) documents ``GET /api/v1/users`` only as a bare
   path. The RFC 7644 (SCIM)-style ``filter=<field> eq '<value>'`` syntax is
   documented for the *same* endpoint family in
   ``RS-09-access-control-sync/notes/qlik-access-control.md`` ("``filter=subject eq
   'user1234'``" / "``filter=assignedRoles.name eq '...'``"), which this module
   extrapolates to ``filter=email eq '<email>'``. TENANT_UNVERIFIED: the exact field
   name (``email`` vs. some other subject-like key) and whether ``eq`` is
   case-sensitive are both unproven — flag for a tenant probe before T3.4/T8.1 rely on
   it. Because the exact match semantics are unproven, results are also re-checked
   client-side (case-insensitive exact email equality) before being trusted, exactly
   as for dataset names.
5. **A user matching more than one distinct id for the same email is ambiguous, not a
   pick.** Symmetrical with the dataset policy: :attr:`OwnerResolutionReason.AMBIGUOUS`
   is reported and dropped, never guessed.
6. **``keyContacts`` dedup keeps the highest-priority role per user, not first-seen.**
   RS-02's sync-readiness note: "a given ``userId`` may appear only once in
   ``keyContacts`` (one role per user)". When the same person is listed twice under
   different neutral :class:`~qlabs_catalog_sync_sdk.models.PartyRole`\\ s (e.g. once
   as ``OWNER`` on the schema, once as ``STEWARD`` via a tag-derived contact), this
   module keeps ``OWNER`` > ``STEWARD`` > ``CONTACT`` > ``OTHER`` — the most
   informative single role, not an arbitrary "whichever came first in a list order the
   caller does not control". Equal-priority collisions keep the first-seen entry.
7. **Caches are process-lifetime, per :class:`QlikReferenceResolver` instance, with no
   TTL, and are cleared only by an explicit** :meth:`QlikReferenceResolver.clear_cache`
   **call.** Two different lookups, two different staleness stories:
   * Dataset name -> id is never the authority — tier 1 (the IdentityMap) always wins
     when it has an answer, so a stale tier-2 cache entry can at worst cause one extra
     cycle of "still unresolved" or "still resolved to the same object" before the
     IdentityMap catches up; it can never cause a wrong write, because this module
     never writes.
   * Email -> ``userId`` changes only when a user's account is deleted/recreated in
     Qlik, which is rare and already an out-of-band admin action; D3 keeps owners
     best-effort, so a cache that occasionally misses a very recent rebind for one
     cycle is an acceptable trade against re-querying the Tier-1 (1000/min) users API
     for every contact on every product, every cycle.
   The intended lifetime is one connector instance (mirroring ``HttpEndpoint`` /
   ``OAuth2ClientCredentialsProvider`` in ``auth.py``): the orchestrator constructs one
   :class:`QlikReferenceResolver` in ``Connector.setup()`` and lets it live until
   ``Connector.close()``, so the cache naturally resets on every fresh connector
   process. ``clear_cache()`` exists so tests can prove the cache without needing a
   second instance, and so an operator-triggered reset is possible later without a
   code change, though nothing in v1 calls it automatically.
8. **The 100-item ``datasetIds`` cap and the ``apiConsumableDatasetIds`` subset rule
   are enforced by the caller, not here** (T3.4's DoD explicitly owns "enforce the
   documented caps ... before calling"). What this module *does* guarantee is that
   :meth:`DatasetResolution.subset_for` can only ever return ids that are actually in
   :attr:`DatasetResolution.dataset_ids` — so a caller building
   ``apiConsumableDatasetIds`` from a subset of the *original* desired members (which
   may include ones this module just dropped as unresolved) cannot accidentally violate
   the subset invariant by forgetting to re-check against the drop list itself.

Every ``http`` call in this module is wrapped with ``auth.classify_response_error`` /
``auth.classify_transport_error`` (T3.1), exactly like ``read.py`` — no second
classification is invented here.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, TypeAlias

import httpx
import structlog

from qlabs_catalog_sync_sdk.contract import EntityType
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import Party, PartyRole

from .auth import classify_response_error, classify_transport_error
from .read import DEFAULT_PAGE_SIZE, ITEMS_PATH

__all__ = [
    "USERS_PATH",
    "DatasetIdentityLookup",
    "DatasetMember",
    "DatasetResolution",
    "DatasetResolutionReason",
    "KeyContact",
    "OwnerResolution",
    "OwnerResolutionReason",
    "QlikReferenceResolver",
    "UnmatchedOwner",
    "UnresolvedDataset",
]

_logger = structlog.get_logger("qlabs_connector_qlik.resolve")

#: Tenant-wide principal catalog (RS-09 ``qlik-access-control.md`` section 3; RS-02
#: section 4.1 "spaceId, ownerId, userId — governance keys"). Tier 1 rate limit
#: (1,000 req/min) per RS-02 section 3.4.
USERS_PATH: Final = "/api/v1/users"

#: Highest-priority role wins a ``keyContacts`` dedup collision (module docstring,
#: point 6). Lower number = higher priority.
_ROLE_PRIORITY: Final[dict[PartyRole, int]] = {
    PartyRole.OWNER: 0,
    PartyRole.STEWARD: 1,
    PartyRole.CONTACT: 2,
    PartyRole.OTHER: 3,
}

#: The exact shape the engine must satisfy to give this module tier-1 dataset
#: resolution (module docstring, top). Returns the Qlik dataset id already bound to
#: this neutral id at the ``qlik`` endpoint, or ``None`` when the IdentityMap has no
#: Qlik-side binding yet.
DatasetIdentityLookup: TypeAlias = Callable[[uuid.UUID], Awaitable["str | None"]]


class DatasetResolutionReason(StrEnum):
    """Why a data-product member dataset did not resolve to a Qlik dataset id."""

    #: Neither the IdentityMap nor an exact name match within the space found it.
    NOT_FOUND = "not_found"
    #: More than one dataset in the space has the member's exact name — refused, not
    #: guessed (module docstring, point 3).
    AMBIGUOUS = "ambiguous"


class OwnerResolutionReason(StrEnum):
    """Why an owner did not become a ``keyContacts`` entry."""

    #: The neutral ``Party`` carries no email at all — nothing to look up.
    NO_EMAIL = "no_email"
    #: The users API returned no user with this email.
    NOT_FOUND = "not_found"
    #: The users API returned more than one distinct user id for this email.
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class DatasetMember:
    """One data-product member this resolver is asked to match to an existing dataset.

    ``name`` is only ever used for the tier-2 fallback (module docstring, point 1);
    tier 1 (the IdentityMap) is keyed on ``neutral_id`` alone.
    """

    neutral_id: uuid.UUID
    name: str


@dataclass(frozen=True, slots=True)
class UnresolvedDataset:
    """One member :meth:`QlikReferenceResolver.resolve_datasets` could not place."""

    neutral_id: uuid.UUID
    name: str
    reason: DatasetResolutionReason
    #: How many exact-name candidates were found in the space, when the reason is
    #: :attr:`~DatasetResolutionReason.AMBIGUOUS`; ``0`` otherwise.
    candidate_count: int = 0


@dataclass(frozen=True, slots=True)
class DatasetResolution:
    """The result of resolving a product's member datasets (decision D2)."""

    #: Every member that resolved, in first-resolved order, keyed by its neutral id.
    #: A ``dict`` (not a list) so duplicate members collapse naturally and
    #: :meth:`subset_for` can do an O(1) membership check per candidate.
    resolved: dict[uuid.UUID, str] = field(default_factory=dict)
    #: Every member that did not resolve — reported, never silently dropped.
    unresolved: list[UnresolvedDataset] = field(default_factory=list)

    @property
    def dataset_ids(self) -> list[str]:
        """The ``datasetIds`` payload value: every resolved id, in resolution order."""
        return list(self.resolved.values())

    def subset_for(self, neutral_ids: Iterable[uuid.UUID]) -> list[str]:
        """Qlik dataset ids for ``neutral_ids``, filtered to ones that actually resolved.

        Intended for building ``apiConsumableDatasetIds`` from whatever subset of the
        *original* desired members the caller considers API-consumable: any id in
        ``neutral_ids`` that this resolver dropped as unresolved is silently excluded
        here rather than raising, which is what keeps the result a true subset of
        :attr:`dataset_ids` even when resolution reports a miss (module docstring,
        point 8). Order follows ``neutral_ids``, not resolution order.
        """
        return [self.resolved[nid] for nid in neutral_ids if nid in self.resolved]


@dataclass(frozen=True, slots=True)
class UnmatchedOwner:
    """One owner :meth:`QlikReferenceResolver.resolve_owners` could not place."""

    party: Party
    reason: OwnerResolutionReason


@dataclass(frozen=True, slots=True)
class KeyContact:
    """One resolved ``keyContacts`` entry: a Qlik ``userId`` plus its Qlik role string."""

    user_id: str
    #: Already the Qlik wire value (``PartyRole.value`` — the two vocabularies share
    #: their lowercase spelling by construction; see ``read.py``'s reverse mapping).
    role: str

    def as_json(self) -> dict[str, str]:
        """The exact ``{"userId": ..., "role": ...}`` shape the create/PATCH body wants."""
        return {"userId": self.user_id, "role": self.role}


@dataclass(frozen=True, slots=True)
class OwnerResolution:
    """The result of resolving a product's owners to Qlik ``keyContacts`` (decision D3)."""

    #: One entry per distinct ``userId`` (module docstring, point 6).
    key_contacts: list[KeyContact] = field(default_factory=list)
    #: Every owner that did not become a key contact — reported, never silently dropped.
    unmatched: list[UnmatchedOwner] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _NameLookupResult:
    """Cached outcome of one dataset name lookup within the resolver's space."""

    dataset_id: str | None
    reason: DatasetResolutionReason | None = None
    candidate_count: int = 0


@dataclass(frozen=True, slots=True)
class _EmailLookupResult:
    """Cached outcome of one owner-email lookup against the users API."""

    user_id: str | None
    reason: OwnerResolutionReason | None = None


class QlikReferenceResolver:
    """Resolves dataset members (D2) and owner emails (D3) for one Qlik space.

    Owns two in-process caches (module docstring, point 7) and the injected tier-1
    identity lookup (module docstring, top). One instance is scoped to one configured
    Qlik endpoint's target space, matching :class:`~qlabs_connector_qlik.config.QlikConfig`
    (``space_id`` is singular per tenant endpoint).
    """

    def __init__(
        self,
        http: HttpEndpoint,
        *,
        endpoint: str,
        space_id: str,
        dataset_identity_lookup: DatasetIdentityLookup,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self._http = http
        self._endpoint = endpoint
        self._space_id = space_id
        self._dataset_identity_lookup = dataset_identity_lookup
        self._page_size = page_size
        self._dataset_name_cache: dict[str, _NameLookupResult] = {}
        self._user_email_cache: dict[str, _EmailLookupResult] = {}

    def clear_cache(self) -> None:
        """Drop every cached dataset-name and owner-email lookup.

        Not called automatically anywhere in v1 (module docstring, point 7) — exists
        for deterministic tests and a possible future operator-triggered reset.
        """
        self._dataset_name_cache.clear()
        self._user_email_cache.clear()

    # -- datasets (decision D2) -------------------------------------------------------

    async def resolve_datasets(self, members: Sequence[DatasetMember]) -> DatasetResolution:
        """Resolve every member: tier 1 the injected IdentityMap lookup, tier 2 an exact
        name match scoped to :attr:`space_id`. Never issues a write of any kind."""
        resolved: dict[uuid.UUID, str] = {}
        unresolved: list[UnresolvedDataset] = []
        for member in members:
            dataset_id = await self._dataset_identity_lookup(member.neutral_id)
            if dataset_id is not None:
                resolved[member.neutral_id] = dataset_id
                continue

            name_result = await self._resolve_dataset_name(member.name)
            if name_result.dataset_id is not None:
                resolved[member.neutral_id] = name_result.dataset_id
                continue

            reason = name_result.reason
            assert reason is not None  # a miss always carries a reason
            unresolved.append(
                UnresolvedDataset(
                    neutral_id=member.neutral_id,
                    name=member.name,
                    reason=reason,
                    candidate_count=name_result.candidate_count,
                )
            )
        return DatasetResolution(resolved=resolved, unresolved=unresolved)

    async def _resolve_dataset_name(self, name: str) -> _NameLookupResult:
        cached = self._dataset_name_cache.get(name)
        if cached is not None:
            return cached

        items = await self._list_items_by_name(name)
        matches = [item for item in items if _item_name(item) == name]
        if not matches:
            result = _NameLookupResult(dataset_id=None, reason=DatasetResolutionReason.NOT_FOUND)
            await _logger.ainfo("qlik.resolve.dataset_unresolved", name=name, reason="not_found")
        elif len(matches) > 1:
            result = _NameLookupResult(
                dataset_id=None,
                reason=DatasetResolutionReason.AMBIGUOUS,
                candidate_count=len(matches),
            )
            await _logger.awarning(
                "qlik.resolve.dataset_ambiguous",
                name=name,
                space_id=self._space_id,
                candidate_count=len(matches),
            )
        else:
            dataset_id = _dataset_id_of(matches[0])
            if dataset_id is None:
                result = _NameLookupResult(
                    dataset_id=None, reason=DatasetResolutionReason.NOT_FOUND
                )
                await _logger.ainfo(
                    "qlik.resolve.dataset_unresolved", name=name, reason="no_usable_id"
                )
            else:
                result = _NameLookupResult(dataset_id=dataset_id)

        self._dataset_name_cache[name] = result
        return result

    async def _list_items_by_name(self, name: str) -> list[dict[str, Any]]:
        params = {
            "resourceType": "dataset",
            "spaceId": self._space_id,
            "name": name,
            "limit": self._page_size,
        }
        try:
            return [
                item
                async for item in self._http.paginate_cursor("GET", ITEMS_PATH, params=params)
                if isinstance(item, dict)
            ]
        except httpx.HTTPStatusError as exc:
            raise classify_response_error(
                exc, endpoint=self._endpoint, entity_type=EntityType.DATASET.value
            ) from exc
        except httpx.TransportError as exc:
            raise classify_transport_error(
                exc, endpoint=self._endpoint, entity_type=EntityType.DATASET.value
            ) from exc

    # -- owners (decision D3) ----------------------------------------------------------

    async def resolve_owners(self, owners: Sequence[Party]) -> OwnerResolution:
        """Resolve every owner's email to a Qlik ``userId`` and dedup into ``keyContacts``.

        A ``Party`` with no email cannot be looked up at all and is reported
        :attr:`OwnerResolutionReason.NO_EMAIL`. Never issues a write of any kind.
        """
        chosen_role: dict[str, PartyRole] = {}
        unmatched: list[UnmatchedOwner] = []
        order: list[str] = []

        for party in owners:
            if not party.email:
                unmatched.append(UnmatchedOwner(party=party, reason=OwnerResolutionReason.NO_EMAIL))
                continue

            email_result = await self._resolve_email(party.email)
            if email_result.user_id is None:
                reason = email_result.reason
                assert reason is not None  # a miss always carries a reason
                unmatched.append(UnmatchedOwner(party=party, reason=reason))
                continue

            user_id = email_result.user_id
            existing = chosen_role.get(user_id)
            if existing is None:
                order.append(user_id)
                chosen_role[user_id] = party.role
            elif _ROLE_PRIORITY[party.role] < _ROLE_PRIORITY[existing]:
                chosen_role[user_id] = party.role
            # else: equal-or-lower priority than the role already held -> keep it
            # (module docstring, point 6).

        key_contacts = [
            KeyContact(user_id=user_id, role=chosen_role[user_id].value) for user_id in order
        ]
        return OwnerResolution(key_contacts=key_contacts, unmatched=unmatched)

    async def _resolve_email(self, email: str) -> _EmailLookupResult:
        cached = self._user_email_cache.get(email)
        if cached is not None:
            return cached

        users = await self._list_users_by_email(email)
        folded = email.casefold()
        matches = [
            user
            for user in users
            if isinstance(user.get("email"), str) and user["email"].casefold() == folded
        ]
        distinct_ids = {
            user["id"] for user in matches if isinstance(user.get("id"), str) and user["id"]
        }

        if not distinct_ids:
            result = _EmailLookupResult(user_id=None, reason=OwnerResolutionReason.NOT_FOUND)
            await _logger.awarning(
                "qlik.resolve.owner_unmatched", reason="not_found", email_domain=_domain_of(email)
            )
        elif len(distinct_ids) > 1:
            result = _EmailLookupResult(user_id=None, reason=OwnerResolutionReason.AMBIGUOUS)
            await _logger.awarning(
                "qlik.resolve.owner_unmatched",
                reason="ambiguous",
                email_domain=_domain_of(email),
                candidate_count=len(distinct_ids),
            )
        else:
            result = _EmailLookupResult(user_id=next(iter(distinct_ids)))

        self._user_email_cache[email] = result
        return result

    async def _list_users_by_email(self, email: str) -> list[dict[str, Any]]:
        # TENANT_UNVERIFIED filter syntax — module docstring, point 4.
        params = {"filter": f"email eq '{email}'", "limit": self._page_size}
        try:
            return [
                item
                async for item in self._http.paginate_cursor("GET", USERS_PATH, params=params)
                if isinstance(item, dict)
            ]
        except httpx.HTTPStatusError as exc:
            raise classify_response_error(
                exc, endpoint=self._endpoint, entity_type="user"
            ) from exc
        except httpx.TransportError as exc:
            raise classify_transport_error(
                exc, endpoint=self._endpoint, entity_type="user"
            ) from exc


# --------------------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------------------


def _item_name(item: Mapping[str, Any]) -> str | None:
    name = item.get("name")
    return name if isinstance(name, str) else None


def _dataset_id_of(item: Mapping[str, Any]) -> str | None:
    """The id that belongs in ``datasetIds`` — ``resourceId``, falling back to the
    Items-API item id (module docstring, point 2)."""
    resource_id = item.get("resourceId")
    if isinstance(resource_id, str) and resource_id:
        return resource_id
    item_id = item.get("id")
    return item_id if isinstance(item_id, str) and item_id else None


def _domain_of(email: str) -> str | None:
    """The domain half of an email, for logging without the personal identifier
    itself — an email is personal data (task conventions), a shared org domain is not.
    """
    _, _, domain = email.rpartition("@")
    return domain or None
