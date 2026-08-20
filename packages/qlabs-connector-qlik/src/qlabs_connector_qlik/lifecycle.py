"""Qlik lifecycle actions — activate, deactivate, move — and ``delete()``.

WP3 / T3.7. Everything in this module talks to the ``.../actions/*`` sub-resources of a
Qlik data product plus the plain ``DELETE`` on the product itself (RS-02
``qlik-catalog-api-reference.md`` section 3.5's actions list; exact bodies in
``qlik-two-way-sync-readiness.md`` section 2):

```
POST   /api/data-governance/data-products/{id}/actions/activate    {"name": str, "spaceId": str}
POST   /api/data-governance/data-products/{id}/actions/deactivate  {}
POST   /api/data-governance/data-products/{id}/actions/move        {"spaceId": str}
DELETE /api/data-governance/data-products/{id}
```

**Why this module exists even though v1 never calls it.** Decision D4
(``decision-databricks-to-qlik-mvp.md``) is explicit: "v1 never deletes in Qlik ... The
Qlik connector implements ``delete()`` and lifecycle actions for contract completeness;
the engine has no path that calls them." This is not dead code — it is what makes the
connector *contract* complete and lets the SDK conformance kit certify it — but it is
also the most dangerous code in this package: a single call can make a customer's data
product tenant-wide discoverable, sever access by moving it out of a space, or delete it
outright. It has to be safe to leave lying around unused, not merely safe when used
correctly.

**Which actions are "destructive" (all four).** Argued, not assumed:

* **Delete** is unambiguously destructive — irreversible data loss at the target.
* **Move** requires *delete* permission in the source space in addition to *create*
  permission in the target (RS-02 section 5) — Qlik's own permission model already
  treats it as a delete-shaped operation. It can also sever access for anyone who could
  see the product in the old space but not the new one, and (module docstring, "Identity
  across a move" below) its effect on identity is not fully documented, which means it
  runs in a zone of behavioral uncertainty against a live customer catalog.
* **Deactivate** removes a product from tenant-wide discovery. Anyone already consuming
  it because it *was* discoverable loses that access without warning — the same "breaks
  something a stranger now depends on" shape as delete, just aimed at visibility instead
  of the object itself.
* **Activate** is called out by name in decision D7: "activation is opt-in per pair and
  off by default because activation makes a product discoverable tenant-wide." Nothing
  is deleted, but tenant-wide discoverability is a one-way door in practice — once other
  teams find and build on a product, deactivating it later is itself a breaking change
  (see "deactivate" above). D7 already treats it as needing the same guard as the other
  three, and this module agrees.

All four are therefore gated by the same mechanism, :class:`DestructiveAction` /
:attr:`LifecycleActions.enabled_actions`, below.

**The opt-in shape, and why a boolean default argument was rejected.** A per-call
``confirm: bool = False`` parameter (or worse, ``confirm: bool = True``) is exactly the
failure mode decision D4 warns about: a future call site that matches the signature —
including the ABC's fixed ``Connector.delete(self, ref) -> None``, which cannot carry an
extra parameter at all without breaking Liskov — trips the guard by construction, not by
intent. This module instead makes the opt-in a **constructor-level capability**:

* :class:`LifecycleActions` takes ``enabled_actions: frozenset[DestructiveAction]``,
  defaulting to the empty set — nothing enabled, by construction, with no call-site
  argument able to override that default per call.
* Enabling one action (say :attr:`DestructiveAction.ACTIVATE` for the future T7.4
  reconciliation) does **not** enable the others — :attr:`DestructiveAction.DELETE`
  stays refused unless it is separately named. A per-pair config that turns on
  activation can never accidentally turn on delete as a side effect.
* Every one of :meth:`~LifecycleActions.activate`, :meth:`~LifecycleActions.deactivate`,
  :meth:`~LifecycleActions.move`, :meth:`~LifecycleActions.delete` checks
  ``enabled_actions`` **before issuing any HTTP request** (:meth:`LifecycleActions._ensure_enabled`,
  first line of every one of those methods) and raises
  :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError` — the SDK's own vocabulary
  for "the plan was wrong, never retry" — when it is not.

This is what makes the scenario the task exists to prevent structurally impossible
rather than merely undocumented: even if a future engine change calls
``await connector.delete(ref)`` — a completely ordinary, ABC-conforming call — nothing
happens unless whatever built that connector's :class:`LifecycleActions` explicitly
named ``DELETE`` in ``enabled_actions``. Wiring that opt-in through from config down to
construction is the exact seam this task's report describes for the orchestrator; see
this package's ``__init__.py`` (owned by another task) for where it has to land.

**Identity across a move.** RS-02's readiness notes (section 3) say data products "keep
the same ``id``/``qri`` across a ``.../actions/move``" because the action "patches the
space, not the identifier" — but flag this as *strongly implied, not explicitly
documented*. This module codes that assumption as the default (:meth:`LifecycleActions.move`
returns a :class:`~qlabs_catalog_sync_sdk.contract.WriteResult` whose ``ref`` is the same
``IdentityRef`` that was passed in) but does not simply trust it blindly: if the move
response *does* carry a JSON body with an ``id`` that disagrees with the input ref's
native key, that disagreement is logged as ``qlik.lifecycle.move.identity_changed`` and
the returned ref is rebuilt around the new id instead of silently returning a ref that no
longer matches the target. **TENANT_UNVERIFIED**: whether the move response carries a
body at all, and whether ``id``/``qri`` really survive a move, per RS-02.

**Activation and a non-managed target space.** RS-02 section 2 documents activation as
"managed space only" but no captured page documents what HTTP status Qlik returns when
the given ``spaceId`` is not managed. Rather than let that surface as Qlik's own generic
error, :meth:`LifecycleActions.activate` treats any *unclassified* 4xx from
``actions/activate`` (i.e. anything :func:`~qlabs_connector_qlik.auth.classify_response_error`
would otherwise turn into a bare :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError`
carrying only "Qlik returned HTTP <code>") as very likely this precondition, and raises a
:class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError` that says so explicitly, naming
the space that was rejected. Status codes Qlik already gives a specific meaning — 401/403
(:class:`~qlabs_catalog_sync_sdk.exceptions.AuthError`), 404
(:class:`~qlabs_catalog_sync_sdk.exceptions.NotFound`), 429/5xx
(:class:`~qlabs_catalog_sync_sdk.exceptions.TransientError`) — are left exactly as
``auth.py`` classifies them; this module never relabels a genuine permission or
not-found error as a space-management problem. **TENANT_UNVERIFIED**: the exact status
code for a non-managed-space rejection.

**Deactivate's request body.** RS-02 documents the endpoint but no body fields for it.
This module POSTs an empty JSON object (``{}``) — a well-formed request with nothing to
say — rather than omitting the body outright, since an unspecified body is more likely to
trip a strict ``Content-Type: application/json`` requirement on the wire than an empty
one is to be rejected as unexpected content. **TENANT_UNVERIFIED**.

**Concurrency.** RS-02 documents no ``if-match``/ETag support for any of these four
endpoints (only the data-product ``PATCH`` and the glossary family), so none of these
calls sends one. ``read.py`` (T3.3) and ``write.py`` (T3.4/T3.5) own the ETag seam for the
paths that do.

Every request in this module goes through the same classified seam T3.1 built
(:func:`~qlabs_connector_qlik.auth.classify_response_error` /
:func:`~qlabs_connector_qlik.auth.classify_transport_error`), exactly like ``read.py`` and
``write.py``: a 403 becomes :class:`~qlabs_catalog_sync_sdk.exceptions.AuthError`, a 404
becomes :class:`~qlabs_catalog_sync_sdk.exceptions.NotFound`, and an exhausted 429/5xx (Tier
2, 100 req/min — RS-02 section 3.4) or transport failure becomes
:class:`~qlabs_catalog_sync_sdk.exceptions.TransientError`. ``HttpEndpoint`` itself already
retries 429/5xx before either of those classifiers ever runs, so no retry logic is
reinvented here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

import httpx
import structlog

from qlabs_catalog_sync_sdk.contract import EntityType, IdentityRef, WriteResult
from qlabs_catalog_sync_sdk.exceptions import CapabilityError, ConnectorError
from qlabs_catalog_sync_sdk.http import HttpEndpoint

from .auth import classify_response_error, classify_transport_error
from .read import DATA_PRODUCTS_PATH

__all__ = [
    "ALL_DESTRUCTIVE_ACTIONS",
    "DestructiveAction",
    "LifecycleActions",
    "build_lifecycle_actions",
]

_logger = structlog.get_logger("qlabs_connector_qlik.lifecycle")

#: HTTP statuses ``auth.classify_response_error`` already gives a specific, correct
#: meaning to. :meth:`LifecycleActions._send_activate` only adds its "likely a
#: non-managed space" hint outside this set, so it never relabels a real auth/not-found/
#: rate-limit error.
_CLASSIFIED_STATUSES: Final = frozenset({401, 403, 404, 429})


class DestructiveAction(StrEnum):
    """The four Qlik data-governance actions this module can perform.

    Named individually (rather than one bare ``bool``) so a caller can opt into exactly
    the actions a given deployment needs — e.g. T7.4's neutral-status reconciliation only
    ever needs :attr:`ACTIVATE` (plus, symmetrically, :attr:`DEACTIVATE` for the reverse
    transition) and should never be handed :attr:`DELETE` as a side effect of that.
    """

    ACTIVATE = "activate"
    DEACTIVATE = "deactivate"
    MOVE = "move"
    DELETE = "delete"


#: Convenience for the one caller that genuinely wants every action enabled — a live-
#: tenant probe script, or a test — spelled out so `frozenset(DestructiveAction)` does not
#: need to be re-derived (and re-justified) at every call site that wants it.
ALL_DESTRUCTIVE_ACTIONS: Final[frozenset[DestructiveAction]] = frozenset(DestructiveAction)


class LifecycleActions:
    """The four Qlik data-governance actions, each refused unless explicitly enabled.

    One instance per configured endpoint, built once (mirrors
    :class:`~qlabs_connector_qlik.write.QlikWriter`'s lifetime) and reused — it holds no
    per-call state. ``enabled_actions`` defaults to the empty set: **every** method
    refuses with :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError`, before any
    HTTP call, unless its own :class:`DestructiveAction` was named at construction time.
    See the module docstring for why this has to be a constructor-level capability rather
    than a per-call flag.
    """

    def __init__(
        self,
        http: HttpEndpoint,
        *,
        endpoint: str,
        enabled_actions: frozenset[DestructiveAction] = frozenset(),
    ) -> None:
        self._http = http
        self._endpoint = endpoint
        self.enabled_actions = enabled_actions

    # -- activate -----------------------------------------------------------------------

    async def activate(self, ref: IdentityRef, *, name: str, managed_space_id: str) -> WriteResult:
        """``POST .../actions/activate`` with ``{"name": name, "spaceId": managed_space_id}``.

        ``managed_space_id`` is a required, explicit argument — never
        :class:`~qlabs_connector_qlik.config.QlikConfig`'s general ``space_id`` — because
        activation is documented as **managed space only** (RS-02 section 2) and the
        connector's configured working space is not guaranteed to be one. Making the
        caller state the managed space by name, every time, is what turns "activated into
        the wrong space" into a call-site bug a reviewer can see rather than a silent
        default. See the module docstring for what happens when it is not actually
        managed.
        """
        self._ensure_enabled(DestructiveAction.ACTIVATE)
        self._ensure_data_product(ref)
        native_id = _native_id(ref)
        response = await self._send_activate(
            native_id, {"name": name, "spaceId": managed_space_id}
        )
        raw = _json_object(
            response,
            url=_action_url(native_id, DestructiveAction.ACTIVATE),
            endpoint=self._endpoint,
        )
        await _logger.ainfo(
            "qlik.lifecycle.activate.ok",
            endpoint=self._endpoint,
            native_key=native_id,
            space_id=managed_space_id,
            activated=raw.get("activated"),
            trust_score=raw.get("trustScore"),
        )
        return WriteResult.updated(
            ref,
            source_revision=response.headers.get("etag"),
            written_fields=["status"],
            detail=f"activated data product {native_id!r} into managed space {managed_space_id!r}",
        )

    # -- deactivate ---------------------------------------------------------------------

    async def deactivate(self, ref: IdentityRef) -> WriteResult:
        """``POST .../actions/deactivate`` with an empty body (module docstring)."""
        self._ensure_enabled(DestructiveAction.DEACTIVATE)
        self._ensure_data_product(ref)
        native_id = _native_id(ref)
        response = await self._send(
            "POST", _action_url(native_id, DestructiveAction.DEACTIVATE), json={}
        )
        await _logger.ainfo(
            "qlik.lifecycle.deactivate.ok", endpoint=self._endpoint, native_key=native_id
        )
        return WriteResult.updated(
            ref,
            source_revision=response.headers.get("etag"),
            written_fields=["status"],
            detail=f"deactivated data product {native_id!r}",
        )

    # -- move ---------------------------------------------------------------------------

    async def move(self, ref: IdentityRef, *, target_space_id: str) -> WriteResult:
        """``POST .../actions/move`` with the required ``{"spaceId": target_space_id}``.

        Assumes the moved product keeps ``ref``'s identity (module docstring, "Identity
        across a move"); logs and rebuilds the ref instead of trusting that blindly when
        the response body disagrees.
        """
        self._ensure_enabled(DestructiveAction.MOVE)
        self._ensure_data_product(ref)
        native_id = _native_id(ref)
        response = await self._send(
            "POST",
            _action_url(native_id, DestructiveAction.MOVE),
            json={"spaceId": target_space_id},
        )
        result_ref = self._identity_after_move(ref, response, native_id=native_id)
        await _logger.ainfo(
            "qlik.lifecycle.move.ok",
            endpoint=self._endpoint,
            native_key=native_id,
            target_space_id=target_space_id,
        )
        return WriteResult.updated(
            result_ref,
            source_revision=response.headers.get("etag"),
            written_fields=["placement"],
            detail=(
                f"moved data product {native_id!r} to space {target_space_id!r}; identity is "
                "assumed unchanged across a move (RS-02 — TENANT_UNVERIFIED)"
            ),
        )

    # -- delete ---------------------------------------------------------------------------

    async def delete(self, ref: IdentityRef) -> None:
        """``DELETE /api/data-governance/data-products/{id}``. Returns ``None`` on success,
        matching :meth:`~qlabs_catalog_sync_sdk.contract.Connector.delete`'s ABC signature.
        """
        self._ensure_enabled(DestructiveAction.DELETE)
        self._ensure_data_product(ref)
        native_id = _native_id(ref)
        await self._send("DELETE", f"{DATA_PRODUCTS_PATH}/{native_id}")
        await _logger.ainfo(
            "qlik.lifecycle.delete.ok", endpoint=self._endpoint, native_key=native_id
        )
        return None

    # -- HTTP + response mapping ----------------------------------------------------------

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One request, T3.1's classification, nothing invented on top (matches ``write.py``)."""
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

    async def _send_activate(self, native_id: str, body: dict[str, Any]) -> httpx.Response:
        """Like :meth:`_send`, plus the "likely a non-managed space" hint (module docstring)."""
        url = _action_url(native_id, DestructiveAction.ACTIVATE)
        try:
            return await self._http.request("POST", url, json=body)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in _CLASSIFIED_STATUSES and status < 500:
                raise ConnectorError(
                    f"Qlik rejected activation of data product {native_id!r} (HTTP {status}); "
                    "the actions/activate endpoint requires the target space to be a Qlik "
                    f"*managed* space (RS-02 section 2) — verify space {body.get('spaceId')!r} "
                    "is managed before retrying. TENANT_UNVERIFIED: the exact status code Qlik "
                    "returns for a non-managed space is not documented.",
                    endpoint=self._endpoint,
                    entity_type=EntityType.DATA_PRODUCT.value,
                    cause=exc,
                ) from exc
            raise classify_response_error(
                exc, endpoint=self._endpoint, entity_type=EntityType.DATA_PRODUCT.value
            ) from exc
        except httpx.TransportError as exc:
            raise classify_transport_error(
                exc, endpoint=self._endpoint, entity_type=EntityType.DATA_PRODUCT.value
            ) from exc

    def _identity_after_move(
        self, ref: IdentityRef, response: httpx.Response, *, native_id: str
    ) -> IdentityRef:
        """``ref`` unchanged, unless the (undocumented) response body disagrees.

        The default *is* trusting the RS-02 assumption that a move never changes
        identity — returning ``ref`` untouched. It only deviates, loudly, when there
        actually is a JSON body and its ``id`` names something else, since that is the
        one signal available that the assumption did not hold this time.
        """
        raw = _maybe_json_object(response)
        if raw is None:
            return ref
        new_id = raw.get("id")
        if not isinstance(new_id, str) or not new_id or new_id == native_id:
            return ref
        _logger.warning(
            "qlik.lifecycle.move.identity_changed",
            endpoint=self._endpoint,
            previous_native_key=native_id,
            new_native_key=new_id,
        )
        return IdentityRef(
            endpoint=ref.endpoint,
            entity_type=ref.entity_type,
            native_key=new_id,
            tenant_id=ref.tenant_id,
            secondary_keys=_secondary_keys(id=new_id, qri=raw.get("qri"), mainId=raw.get("mainId")),
        )

    # -- guards -----------------------------------------------------------------------------

    def _ensure_enabled(self, action: DestructiveAction) -> None:
        """Refuse, before any HTTP call, an action not named in ``enabled_actions``.

        This is the guard the whole module exists to prove: default construction leaves
        ``enabled_actions`` empty, so every one of the four methods raises here — zero
        requests issued — until something upstream deliberately names the action.
        """
        if action not in self.enabled_actions:
            raise CapabilityError(
                f"connector {self._endpoint!r} refused {action.value!r}: destructive Qlik "
                "lifecycle actions are off by default (decision D4/D7) and must be enabled "
                "explicitly per action via LifecycleActions(..., enabled_actions={...}) — "
                "see lifecycle.py's module docstring for why a default argument is not "
                "considered a safe way to opt in",
                endpoint=self._endpoint,
                entity_type=EntityType.DATA_PRODUCT.value,
                operation=action.value,
            )

    def _ensure_data_product(self, ref: IdentityRef) -> None:
        """Refuse, before any HTTP call, a ref for anything but a data product.

        Qlik's data-governance actions and ``DELETE`` are data-product-only endpoints;
        there is no dataset/glossary/category equivalent for this connector to call.
        """
        if ref.entity_type is not EntityType.DATA_PRODUCT:
            raise CapabilityError(
                f"connector {self._endpoint!r} only implements lifecycle actions for "
                f"{EntityType.DATA_PRODUCT.value!r}, not {ref.entity_type.value!r} — Qlik's "
                "data-governance actions and DELETE endpoints are data-product only",
                endpoint=self._endpoint,
                entity_type=ref.entity_type.value,
            )


def build_lifecycle_actions(
    http: HttpEndpoint,
    *,
    endpoint: str,
    enabled_actions: frozenset[DestructiveAction] = frozenset(),
) -> LifecycleActions:
    """Build a :class:`LifecycleActions`. Mirrors ``write.py``'s ``build_writer`` shape so
    ``Connector.setup()`` (owned by another task) can wire this collaborator the same way
    it already wires the writer — see this package's ``__init__.py`` docstring for the
    exact change required there.
    """
    return LifecycleActions(http, endpoint=endpoint, enabled_actions=enabled_actions)


# --------------------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------------------


def _action_url(native_id: str, action: DestructiveAction) -> str:
    return f"{DATA_PRODUCTS_PATH}/{native_id}/actions/{action.value}"


def _native_id(ref: IdentityRef) -> str:
    """The data-governance path id — ``secondary_keys['id']`` if present, else the native
    key itself (for a data product the two are the same value; matches ``read.py``/
    ``write.py``'s convention)."""
    return ref.secondary_keys.get("id", ref.native_key)


def _secondary_keys(**candidates: object) -> dict[str, str]:
    """Deliberately a local copy of ``read.py``'s/``write.py``'s private helper of the
    same name, not an import of either: neither module exports it, and reaching across
    that ownership line for a two-line dict comprehension is not worth it (``write.py``
    makes the identical call for the identical reason)."""
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


def _maybe_json_object(response: httpx.Response) -> dict[str, Any] | None:
    """A JSON object body if the response actually carries one, else ``None``.

    Used only where RS-02 does not confirm a response body exists at all (move) — unlike
    :func:`_json_object`, this never raises: an empty ``204`` and an unparseable body are
    both treated as "no signal available", not an error.
    """
    if not response.content:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None
