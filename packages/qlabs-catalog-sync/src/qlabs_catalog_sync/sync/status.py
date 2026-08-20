"""Decision D7 — neutral status -> Qlik activation, and the seam that requests it.

WP7 / T7.4. D7 (``decision-databricks-to-qlik-mvp.md``) is a one-line mapping: neutral
``status`` :attr:`~qlabs_catalog_sync_sdk.models.DataProductStatus.ACTIVE` corresponds to
an *activated* Qlik data product, **in a managed space only**, and **only when the pair
opted in** — every other status leaves the product deactivated. The mapping itself is
trivial; what this module actually has to work out is how the engine can honestly act on
it without importing a connector (the hard dependency rule: the engine depends only on the
SDK) and without a real Qlik activation route to call through yet.

What ``loop.py`` (T2.4) already does, and what this module does not repeat
------------------------------------------------------------------------------

``sync.loop`` already applies D7's opt-in half on the *ordinary* create/update path: it
strips ``status`` from a create candidate, and withholds it from an update diff, unless
:attr:`~qlabs_catalog_sync.config.SyncPairConfig.activation_opt_in` is set — unconditionally
and before any injected write policy, so a policy cannot weaken it (see
``sync.loop.ACTIVATION_FIELD`` / ``ACTIVATION_WITHHELD_REASON``, and
``tests/sync/test_report_and_policy.py`` /
``tests/sync/test_cycle.py::test_activation_is_written_once_the_pair_opts_in`` /
``test_a_create_carries_the_status_once_the_pair_opts_in``, all already merged). This
module does **not** re-implement that gate — doing so would be the "second, conflicting"
version the task exists to avoid. :func:`decide_activation` below reuses the exact same
reason code (:data:`ACTIVATION_NOT_OPTED_IN_REASON` — see the consistency test in
``tests/status/`` that asserts it against ``sync.loop.ACTIVATION_WITHHELD_REASON``
byte-for-byte) precisely so a report that combines both withholdings reads as one decision,
not two.

What is still missing, and why it cannot be "just send the field"
------------------------------------------------------------------------------

D7 has a second gate ``loop.py`` cannot express at all: **managed space only**.
:class:`~qlabs_catalog_sync.config.SyncPairConfig` has no managed-space field (T2.3's
scope), and even the Qlik connector's own config does not carry one —
``qlabs_connector_qlik.lifecycle.LifecycleActions.activate`` (T3.7, read here, never
imported — the hard dependency rule) takes ``managed_space_id`` as a **required, explicit**
argument rather than reading it off ``QlikConfig.space_id``, because that space is not
guaranteed managed. This module mirrors that same design choice for the same reason (see
:func:`reconcile_activation`'s ``managed_space_id`` parameter): no field to default from
exists yet, so the caller must state it, every time, rather than the engine guessing.

The other missing piece is the real activation call itself. ``status`` is declared ``rw``
with ``writable_via="lifecycle-action"`` in the Qlik-shaped manifest
(``qlabs_catalog_sync_sdk.testing.manifests.qlik_shaped_manifest``) precisely because Qlik
activation is not a field mutation — it is ``POST .../actions/activate``, a completely
different verb. ``qlabs_connector_qlik.write.py``'s ``update()`` refuses a diff that touches
``status`` with :class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError` **by design, not
by omission** (its own docstring: "the alternative — reporting them as skipped and
returning success — would let a never-applied activation look synced forever"), and
``create()`` never activates either. That refusal is correct and permanent, not a gap to
route around — sending ``status`` through the ordinary diff/candidate (which ``loop.py``
already does once opted in, for the reasons above) simply cannot carry activation to Qlik,
today or ever, regardless of opt-in. The real route is
``qlabs_connector_qlik.lifecycle.LifecycleActions.activate``/``.deactivate`` — but those are
plain methods on a connector-package class, **not** ``Connector`` ABC methods (T3.7's
finding), so the engine has no generic name to call even if it wanted to.

**The honest resolution this module builds**: a decision layer
(:func:`activation_intent_for`, :func:`decide_activation`) that is pure and connector-free,
plus a thin execution layer (:func:`reconcile_activation`) that looks for the activation
route on the *generic* ``Connector`` surface through :class:`ActivationCapable` — a
``Protocol`` shaped exactly like ``LifecycleActions.activate`` — rather than importing the
Qlik connector or assuming ``update()``/``create()`` can carry it. When the configured
target does not implement :class:`ActivationCapable` (true of every connector in this
repository today, including the real Qlik one), :func:`reconcile_activation` reports
``unsupported=True`` and issues **no call at all** rather than guessing at a field write
that would only ever be refused. This is the "must not emit ``status`` until a connector-side
route exists" branch, applied specifically to the *lifecycle-action* channel — it does not
touch, strip, or duplicate what ``loop.py`` already sends through the *field* channel.

**The exact wiring change the orchestrator must make** (this module makes none of it real
by itself — see the task report):

1. Add two concrete, non-abstract methods to ``qlabs_catalog_sync_sdk.contract.Connector``,
   mirroring how ``create``/``update``/``delete`` already default-refuse::

       async def activate(self, ref, *, name: str, managed_space_id: str) -> WriteResult:
           raise self._capability_error("activate", ref.entity_type)
       async def deactivate(self, ref) -> WriteResult:
           raise self._capability_error("deactivate", ref.entity_type)

   (This is an SDK change, outside this task's owned paths.) Once added, every connector
   satisfies :class:`ActivationCapable` structurally, and the Qlik connector's own
   ``Connector`` subclass overrides ``activate``/``deactivate`` to delegate to its existing
   ``LifecycleActions`` collaborator (T3.7), gated by that class's own ``enabled_actions``
   opt-in (a *second*, connector-level guard, deliberately separate from
   ``activation_opt_in`` — see ``lifecycle.py``'s module docstring on why a boolean default
   argument was rejected there).
2. Add a managed-space field to ``SyncPairConfig`` (e.g. ``managed_space_id: str | None =
   None``, alongside ``activation_opt_in``) — a ``config.py`` change, outside this task's
   owned paths — or resolve it dynamically some other way; either way, something must
   produce the value this module's ``managed_space_id`` parameter needs.
3. Call :func:`reconcile_activation` from ``sync.loop`` after a create or an update
   *succeeds* (using the resolved ``target_ref`` either way — the same call in both cases,
   which is why this module exposes one function for both rather than two), passing the
   pair, the source-side neutral entity, and the managed-space id from (2). This is
   additional to, not a replacement for, ``loop.py``'s existing field-level withholding.

**Whether v1 deactivates.** No. :data:`ActivationIntent` has no ``DEACTIVATE`` member and
:func:`activation_intent_for` never returns one: every status other than ``ACTIVE`` decides
:attr:`ActivationIntent.NONE`, meaning "leave whatever Qlik-side activation state currently
exists alone" — never issue a deactivate request, even for an object this pair previously
activated. This mirrors D4's spirit ("v1 never deletes/removes") applied to
discoverability rather than data: deactivating a product that other teams have since found
and built on is a breaking change in exactly the shape D4 exists to prevent, and
``decision.md`` names the manual-edit policy as v1's *only* Qlik-side conflict handling —
an automatic deactivation would be a second one it does not describe.
``qlabs_connector_qlik.lifecycle``'s own docstring makes the same call for the connector
side: "the engine has no path that calls" any of the four lifecycle actions in v1, and
``DestructiveAction.DEACTIVATE`` is gated independently of ``DestructiveAction.ACTIVATE`` in
``LifecycleActions.enabled_actions`` for exactly this reason. Revisiting this is RM-02's
two-way-sync territory, not v1's.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

import structlog

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync_sdk.contract import Connector, IdentityRef, WriteResult
from qlabs_catalog_sync_sdk.models import DataProduct, DataProductStatus

__all__ = [
    "ACTIVATION_NOT_OPTED_IN_REASON",
    "ACTIVATION_REQUIRES_MANAGED_SPACE_REASON",
    "STATUS_FIELD",
    "STATUS_NOT_ACTIVE_REASON",
    "ActivationCapable",
    "ActivationDecision",
    "ActivationIntent",
    "ActivationOutcome",
    "activation_intent_for",
    "decide_activation",
    "reconcile_activation",
]

_LOG = structlog.get_logger("qlabs.catalog_sync.sync.status")

#: The neutral field D7 governs. Used only for logging/observability here — the decision
#: functions below take a typed :class:`~qlabs_catalog_sync_sdk.models.DataProductStatus`
#: directly, never a bare field name. Deliberately a distinct constant from
#: ``sync.loop.ACTIVATION_FIELD`` (same value) rather than an import of it — see the module
#: docstring's note on why this module does not depend on ``loop.py``.
STATUS_FIELD: Final[str] = "status"

#: Reported when the source status is anything other than
#: :attr:`~qlabs_catalog_sync_sdk.models.DataProductStatus.ACTIVE` — the object's current
#: Qlik-side activation state (if any) is left exactly as it is (module docstring, "whether
#: v1 deactivates").
STATUS_NOT_ACTIVE_REASON: Final[str] = "status_not_active"

#: The same reason code ``sync.loop.ACTIVATION_WITHHELD_REASON`` already uses for the
#: field-level withholding, reused verbatim (not imported — see the module docstring) so a
#: report that reads both withholdings never has to explain two spellings of one decision.
ACTIVATION_NOT_OPTED_IN_REASON: Final[str] = "activation_not_opted_in"

#: D7's second gate, which ``loop.py`` has no way to express (no managed-space field on
#: :class:`~qlabs_catalog_sync.config.SyncPairConfig` today — module docstring, point 2).
ACTIVATION_REQUIRES_MANAGED_SPACE_REASON: Final[str] = "activation_requires_managed_space"


# ==========================================================================================
# The pure D7 mapping
# ==========================================================================================


class ActivationIntent(StrEnum):
    """What D7 says should happen to a product's Qlik-side activation state.

    No ``DEACTIVATE`` member on purpose — see the module docstring, "whether v1
    deactivates". Every status that is not :attr:`~..models.DataProductStatus.ACTIVE`
    decides :attr:`NONE`, which means "leave the current Qlik-side state alone", not
    "deactivate it".
    """

    ACTIVATE = "activate"
    NONE = "none"


def activation_intent_for(status: DataProductStatus | None) -> ActivationIntent:
    """D7's mapping, and nothing else: ``ACTIVE`` activates, everything else does not.

    Takes the bare status rather than a whole entity so it is trivially testable against
    every :class:`~qlabs_catalog_sync_sdk.models.DataProductStatus` member plus ``None``
    (an object the source never assigned a status to).
    """
    if status is DataProductStatus.ACTIVE:
        return ActivationIntent.ACTIVATE
    return ActivationIntent.NONE


@dataclass(frozen=True, slots=True)
class ActivationDecision:
    """What D7, fully gated, says about one object right now.

    ``requested`` is the load-bearing field: ``True`` only when every one of D7's
    conditions held. ``reason`` names the first condition that failed, in the same
    stable-reason-code vocabulary the run report already uses elsewhere in this package
    (:class:`~qlabs_catalog_sync.sync.loop.WithheldField`), and is ``None`` exactly when
    ``requested`` is ``True``.
    """

    intent: ActivationIntent
    requested: bool
    reason: str | None = None


def decide_activation(
    status: DataProductStatus | None,
    *,
    activation_opt_in: bool,
    managed_space_id: str | None,
) -> ActivationDecision:
    """D7, fully gated: status, then opt-in, then managed space, in that order.

    Pure and connector-free — no I/O, nothing to await — so every combination is
    exhaustively testable without a fake connector at all. ``activation_opt_in`` and
    ``managed_space_id`` are taken as plain values rather than a
    :class:`~qlabs_catalog_sync.config.SyncPairConfig` here specifically so this core
    function stays trivial to call from a table-driven test;
    :func:`reconcile_activation` is the integration entry point that reads
    ``activation_opt_in`` off the pair itself, so a real call site cannot invent a second
    switch (the task's own constraint).

    Order matters only for which single reason is reported when more than one condition
    fails: opt-in is checked before managed space because it is D7's named primary gate
    ("opt-in per pair and off by default"), matching ``loop.py``'s own precedence for the
    field-level withholding.
    """
    intent = activation_intent_for(status)
    if intent is ActivationIntent.NONE:
        return ActivationDecision(intent=intent, requested=False, reason=STATUS_NOT_ACTIVE_REASON)
    if not activation_opt_in:
        return ActivationDecision(
            intent=intent, requested=False, reason=ACTIVATION_NOT_OPTED_IN_REASON
        )
    if not managed_space_id:
        return ActivationDecision(
            intent=intent, requested=False, reason=ACTIVATION_REQUIRES_MANAGED_SPACE_REASON
        )
    return ActivationDecision(intent=intent, requested=True, reason=None)


# ==========================================================================================
# The generic-surface seam
# ==========================================================================================


@runtime_checkable
class ActivationCapable(Protocol):
    """The shape a connector needs to actually carry out a requested activation.

    Not part of ``Connector`` today — this is exactly the gap the module docstring's
    wiring note describes. Shaped to match
    ``qlabs_connector_qlik.lifecycle.LifecycleActions.activate`` (read, never imported —
    the hard dependency rule) so that once the SDK grows this as a concrete ``Connector``
    method, the Qlik connector's override is a one-line delegation, not a redesign.
    :func:`reconcile_activation` checks for this structurally (``isinstance``, thanks to
    ``@runtime_checkable``) rather than assuming any particular ``Connector`` subclass, so
    it keeps working unchanged whether the seam lands as a new ABC method, a mixin, or
    (today, in every connector this repository ships) not at all.
    """

    async def activate(
        self, ref: IdentityRef, *, name: str, managed_space_id: str
    ) -> WriteResult: ...


@dataclass(frozen=True, slots=True)
class ActivationOutcome:
    """What actually happened when :func:`reconcile_activation` ran.

    ``attempted`` is ``False`` in two different situations, told apart by
    ``unsupported``: the decision itself said no (``unsupported=False`` — D7 was not
    satisfied), or the decision said yes but the target connector has no generic way to
    carry it out (``unsupported=True`` — true of every connector in this repository
    today, including the real Qlik one, until the wiring note's SDK change lands).
    Collapsing these two into one boolean would hide the difference between "D7 says no"
    and "D7 says yes but nothing can act on it yet", which is exactly the distinction an
    operator needs to see.
    """

    decision: ActivationDecision
    attempted: bool
    write_result: WriteResult | None = None
    unsupported: bool = False


async def reconcile_activation(
    target: Connector,
    target_ref: IdentityRef,
    product: DataProduct,
    *,
    pair: SyncPairConfig,
    managed_space_id: str | None,
) -> ActivationOutcome:
    """Decide D7 for ``product`` and, only when every gate holds, request it.

    Call this once ``target_ref`` is known to be a confirmed, resolved target identity —
    after a create succeeds (using the ref the target returned) or alongside an update
    (using the existing binding). The same call either way is deliberate: D7 does not
    distinguish "just created" from "already existed", so this module does not either
    (module docstring, wiring note 3).

    ``pair.activation_opt_in`` is the only opt-in switch consulted — the same one
    ``loop.py`` already reads for the field-level withholding (module docstring). This
    never touches ``target_ref``'s stored fields, the state store, or the plain
    create/update path ``loop.py`` already drives; it only ever issues the dedicated
    :class:`ActivationCapable` call, and only when :attr:`ActivationDecision.requested` is
    ``True``.
    """
    decision = decide_activation(
        product.status,
        activation_opt_in=pair.activation_opt_in,
        managed_space_id=managed_space_id,
    )
    log = _LOG.bind(pair=pair.name, endpoint=target.name, native_key=target_ref.native_key)
    if not decision.requested:
        log.debug("status.activation.not_requested", reason=decision.reason)
        return ActivationOutcome(decision=decision, attempted=False)

    if not isinstance(target, ActivationCapable):
        log.warning(
            "status.activation.unsupported",
            detail=(
                f"connector {target.name!r} exposes no generic activation route yet; "
                "see qlabs_catalog_sync.sync.status's module docstring for the required "
                "wiring"
            ),
        )
        return ActivationOutcome(decision=decision, attempted=False, unsupported=True)

    assert managed_space_id is not None  # decision.requested guarantees this (decide_activation)
    result = await target.activate(target_ref, name=product.name, managed_space_id=managed_space_id)
    log.info("status.activation.requested", managed_space_id=managed_space_id)
    return ActivationOutcome(decision=decision, attempted=True, write_result=result)
