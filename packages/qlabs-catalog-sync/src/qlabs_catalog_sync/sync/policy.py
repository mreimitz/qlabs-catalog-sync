"""Manual-edit-on-Qlik policy.

WP2 / T2.5. The single v1 conflict policy named in ``decision.md`` guardrail 2 and in
the neutral-metadata-model spec's conflict interface (RS-03 section 10, narrowed by
RM-01's scope decision to exactly this): source-wins overwrite by default, or
preserve-local per entity type / per field, configured through
:class:`~qlabs_catalog_sync.config.ManualEditPolicy`. The full bidirectional conflict
engine RS-03 gestures at is deferred to RM-02 — this module never merges two sources of
truth, it only decides, field by field, whether *this* cycle's write goes out.

:class:`ManualEditWritePolicy` plugs into the seam ``sync/loop.py`` (T2.4) already
defines and documents: :class:`~qlabs_catalog_sync.sync.loop.WritePolicy`. Two things
that seam guarantees on its own, which this module leans on rather than re-implementing:

* D7's activation withholding runs first and unconditionally — this policy never sees,
  and could not undo, the ``status`` field's fate.
* Whatever this policy withholds is excluded both from the write *and* from what gets
  persisted, so a preserved field is diffed — and re-evaluated — again next cycle
  instead of the engine quietly believing it landed.

What "manually edited" means, and how it is detected
------------------------------------------------------

A field is a manual edit when the target's **current, live** value differs from the
envelope the engine **last recorded persisting there** — not from the source. Those are
different questions: the source almost always differs from the target the moment a
diff exists at all (that is *why* a write is planned), so comparing live-to-source would
flag every ordinary sync as a conflict. The comparison that means something is
live-vs-last-known-target, and ``WriteReview.stored_target_envelopes`` is exactly that
last-known-target record — the state store's copy of what the engine believes it wrote
(or read and confirmed) at the target, checksummed the same way the diff engine
checksums everything else (``envelope.has_changed``, T1.6).

That comparison alone resolves the three cases the seam calls out:

* **The target never had the field** (no entry in ``stored_target_envelopes``): there is
  no prior belief to contradict, so this is a first write, not an edit. Excluded from
  detection before any read happens — see the re-read discipline below.
* **The engine wrote it last cycle and nothing has touched it since**: the live value is
  exactly what got persisted, so the checksums match and ``has_changed`` reports no
  change. Not a manual edit.
* **A human (or anything other than this engine) changed it out of band**: the live
  checksum no longer matches the persisted one. That, and only that, is a manual edit.

When to re-read the target, and when not to
---------------------------------------------

Detection needs the target's *live* state, which costs a ``read`` call the diff phase
did not already make (the diff runs against the state store's cached envelopes, not a
fresh read). That call is not free, so it is taken only when the answer could actually
change the outcome:

* A field resolved to :attr:`~qlabs_catalog_sync.config.ManualEditMode.SOURCE_WINS`
  (v1's default, for every field until configured otherwise) is written regardless of
  whether it was hand-edited — that is what "source wins" means. Detecting the edit
  would not change what happens, so it is never checked, and a cycle where every changed
  field is ``source_wins`` costs this policy zero calls to ``target.read``.
* A field the target has never held (no ``stored_target_envelopes`` entry) cannot be a
  manual edit regardless of its mode, so it is excluded from the read decision too — a
  ``preserve_local`` pair whose objects are all still on their first sync also costs
  nothing.
* Only when at least one changed field resolves to ``preserve_local`` *and* has a prior
  recorded value does this policy call ``target.read`` — and then exactly **once** per
  reviewed entity, however many such fields it has, because one read returns every
  field's current envelope together.

If that read itself fails (the target is unreachable, or the object vanished between
the diff and this check — any :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError`),
detection cannot be proven either way. This module fails open to v1's source-wins
default rather than the alternative of raising: an uncaught exception here would abort
the *entire* cycle (``sync/loop.py``'s outer handler treats any non-typed failure as a
fatal engine error, and even a typed one has no per-record recovery at this seam), which
is a wildly disproportionate reaction to "the extra safety read didn't work." The write
proceeds as it would have if no manual-edit policy were configured at all, and the
attempt is logged so the miss is visible.

What happens on the next cycle, under ``preserve_local``
------------------------------------------------------------

Withholding a field means it is never persisted (the seam's guarantee, not this
module's), so ``stored_target_envelopes`` for that field stays exactly what it was
*before* the human's edit — the diff keeps comparing the source against that same
stale, pre-edit baseline every cycle. Two consequences follow, and both are deliberate:

* If the source changes again, the diff still shows the field as changed (it still
  differs from the old baseline), this policy is consulted again, the live value is
  still found to differ from that same old baseline, and the field is withheld again.
  **The local edit is preserved indefinitely**, not just for one cycle — "preserve" is
  read literally. This is the only reading consistent with the field's mode being a
  standing configuration choice rather than a one-shot conflict resolution: nothing
  about a second source change makes the earlier human decision less deliberate, and a
  policy that flipped to overwrite on the second occurrence would make "preserve local"
  mean "preserve local unless it happens twice," which is not what the config says.
* This is stable, not a thrash: the same two facts (a fixed stale baseline, an unchanged
  live edit) produce the same withheld decision every cycle. Nothing here oscillates
  between writing and not writing.
* The edit stops being preserved only when a human deliberately ends the standoff: by
  reverting the target field to the last value the engine actually recorded (at which
  point live matches stored again and the field simply is not a manual edit any more),
  or by reconfiguring the field to ``source_wins`` (which stops asking the question at
  all and lets the next cycle's write land). Both are explicit actions outside this
  module, which is exactly where that decision belongs — this is a one-way overwrite
  policy, not a merge, and it does not invent a reconciliation step.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import structlog

from qlabs_catalog_sync.config import ManualEditMode
from qlabs_catalog_sync.sync.loop import WriteReview
from qlabs_catalog_sync_sdk.envelope import has_changed
from qlabs_catalog_sync_sdk.exceptions import ConnectorError

__all__ = ["MANUAL_EDIT_WITHHELD_REASON", "ManualEditWritePolicy"]

_LOG = structlog.get_logger("qlabs.catalog_sync.sync.policy")

#: The reason code this policy attaches to a field it withholds. Distinct from
#: :data:`~qlabs_catalog_sync.sync.loop.ACTIVATION_WITHHELD_REASON` -- D7 and a manual
#: edit are different reasons a write did not happen, and a run report must be able to
#: tell them apart.
MANUAL_EDIT_WITHHELD_REASON: Final[str] = "manual_edit_preserved"


class ManualEditWritePolicy:
    """The v1 manual-edit policy: :class:`~qlabs_catalog_sync.sync.loop.WritePolicy`.

    Deliberately holds no configuration of its own. Every pair already carries its own
    :attr:`~qlabs_catalog_sync.config.SyncPairConfig.manual_edit_policy`, and
    :class:`~qlabs_catalog_sync.sync.loop.WriteReview` hands this policy the pair on
    every call -- so reading ``review.pair.manual_edit_policy`` on each review is both
    simpler than caching it and automatically correct if the pair's own config is ever
    hot-reloaded. One instance is safe to share across every :class:`SyncLoop` a process
    runs, whatever pairs they cover: the module docstring's module-level constant is the
    only state, and it is immutable.

    See the module docstring for the detection rule, the re-read discipline, and what
    a subsequent source change does to a preserved field.
    """

    async def withhold(self, review: WriteReview) -> Mapping[str, str]:
        """Fields this cycle's write must not carry, mapped to why.

        Structurally satisfies :class:`~qlabs_catalog_sync.sync.loop.WritePolicy`
        (a ``Protocol``); no inheritance is needed or attempted.
        """
        policy = review.pair.manual_edit_policy
        candidates = tuple(
            change.field
            for change in review.plan.changes
            if policy.mode_for(review.entity_type, change.field) is ManualEditMode.PRESERVE_LOCAL
            and change.field in review.stored_target_envelopes
        )
        if not candidates:
            # Either every changed field is source_wins (the default -- detecting an
            # edit would not change the outcome) or every preserve_local candidate has
            # no prior recorded value at the target (so it cannot yet be a manual edit).
            # Either way, no read is worth its cost.
            return {}

        log = _LOG.bind(
            pair=review.pair.name,
            entity_type=review.entity_type.value,
            target=review.target.name,
            target_native_key=review.target_ref.native_key,
        )
        try:
            live = await review.target.read(review.target_ref)
        except ConnectorError as exc:
            # Detection needs the target's current value; unable to get it, this policy
            # fails open to source-wins rather than aborting the whole cycle over what
            # was meant to be an extra safety check. See the module docstring.
            log.warning(
                "policy.manual_edit.reread_failed",
                candidate_fields=list(candidates),
                error_kind=type(exc).__name__,
            )
            return {}

        withheld: dict[str, str] = {}
        for field in candidates:
            live_envelope = live.envelope_for(field)
            if live_envelope is None:
                # The target no longer reports this field at all. There is nothing to
                # compare, and no evidence of a human edit -- treat it the same as a
                # field the target never had, and let the write proceed.
                continue
            if has_changed(live_envelope, review.stored_target_envelopes[field]):
                withheld[field] = MANUAL_EDIT_WITHHELD_REASON

        if withheld:
            log.info("policy.manual_edit.preserved", fields=sorted(withheld))
        return withheld
