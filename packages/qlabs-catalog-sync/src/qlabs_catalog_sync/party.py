"""Owner/Party best-effort e-mail correlation (WP7 / T7.3).

Correlates :class:`~qlabs_catalog_sync_sdk.models.Party` values across a source-side list and a
target-side list by e-mail, and by nothing else. Per the v1 scope decision
(``planning/Roadmap/RM-01-one-way-sync-mvp/decision.md``) and D3 of
``decision-databricks-to-qlik-mvp.md``, owners are best-effort descriptive metadata — never an
identity-resolution system and never an authorization decision. v1 has no access-control sync at
all, so nothing in this module is, or may become, reachable from an access-control code path.

What this module deliberately does **not** do:

* It never resolves a source e-mail to a target-endpoint user id (e.g. a Qlik ``userId``). That
  lookup is the Qlik connector's job (T3.9), against the live users API, with its own miss
  reporting. This module only correlates neutral ``Party`` values that already carry an e-mail —
  by e-mail.
* It never guesses. A ``Party`` with no e-mail at all is reported unmatched, never matched on
  ``party_id`` or ``display_name`` instead. This matters concretely for Databricks: it reports
  owners as either an e-mail or a service-principal application id, and an application id is not
  an e-mail. Recognizing that difference and choosing which ``Party`` field to populate is the
  *reading* connector's job (e.g. put an application id in ``party_id``, leave ``email`` unset);
  this module's contract is simply that ``Party.email in (None, "")`` is unconditionally
  unmatched, so a bare identifier can never be silently treated as though it correlated.

Normalization: an e-mail is compared case-folded and stripped of leading/trailing whitespace.
Dots and ``+tag`` segments in the local part are left untouched — ``a.b@x.com`` is **not**
treated as equal to ``ab@x.com``, and neither is ``a+tag@x.com`` to ``a@x.com``. Gmail-style
local-part aliasing is a single-provider convention, not a property of e-mail addresses in
general; treating it as universal across every endpoint (Databricks, Qlik, Collibra, Snowflake)
would be a correctness claim this best-effort correlation has no basis for making.

This module does no I/O. Unlike the rest of the engine's async convention, it is plain,
synchronous, pure functions on purpose: two lists in, one immutable result out, nothing awaited.
Reporting the result (e.g. logging unmatched owners for a run report) is the caller's job, so
this module never logs — an e-mail is personal data, and deciding what is safe to put in a log
line belongs with whoever writes the log line, not buried in a pure correlation function.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from qlabs_catalog_sync_sdk.models import Party, PartyRole

__all__ = [
    "CorrelationResult",
    "PartyMatch",
    "Side",
    "UnmatchedParty",
    "UnmatchedReason",
    "correlate_parties",
    "normalize_email",
]

Side = Literal["source", "target"]


class UnmatchedReason(StrEnum):
    """Why a party could not be correlated to the other side."""

    NO_EMAIL = "no_email"
    """The party carries no e-mail at all (or only whitespace), so it was never a candidate."""

    NO_MATCH = "no_match"
    """The party has an e-mail, but no party on the other side normalizes to the same one."""


def normalize_email(email: str | None) -> str | None:
    """Normalize ``email`` for comparison, or ``None`` when there is nothing to compare.

    Case-folds and strips leading/trailing whitespace only. A blank or whitespace-only string
    normalizes to ``None``, same as a missing e-mail — neither is a correlation candidate.
    Dots and ``+tag`` segments in the local part are preserved as written; see the module
    docstring for why.
    """
    if email is None:
        return None
    normalized = email.strip().casefold()
    return normalized or None


@dataclass(frozen=True, slots=True)
class UnmatchedParty:
    """One party that could not be correlated to the other side."""

    side: Side
    party: Party
    reason: UnmatchedReason

    @property
    def label(self) -> str:
        """A human-readable identifier for naming this party in a run report.

        Prefers the least sensitive attribute available (display name, then party id) and falls
        back to the e-mail only when neither was given.
        """
        return self.party.display_name or self.party.party_id or self.party.email or "<unnamed>"


@dataclass(frozen=True, slots=True)
class PartyMatch:
    """One correlated pair: a source party and the target party with the same normalized e-mail."""

    email: str
    """The normalized e-mail the two parties matched on."""

    source: Party
    target: Party

    @property
    def role(self) -> PartyRole:
        """The role to carry forward.

        v1 is upstream-only (source catalogs -> Qlik), so when the source and target disagree on
        role for the same person, the source is authoritative — the same source-wins default the
        v1 scope decision applies to manual edits generally. See :attr:`target_role` for the
        role the target side actually had, kept for diagnostics only.
        """
        return self.source.role

    @property
    def target_role(self) -> PartyRole:
        """The role the target-side party had, before the source-wins policy is applied.

        Exposed for diagnostics/logging only — nothing reads this to make an authorization
        decision, and this module has no access-control code path for it to reach.
        """
        return self.target.role


@dataclass(frozen=True, slots=True)
class CorrelationResult:
    """The full output of :func:`correlate_parties`.

    Every sequence is ordered deterministically:

    * ``matches`` follows the order the matched e-mail first appeared in ``source``.
    * ``unmatched_source`` lists no-e-mail source parties first (in ``source`` order), then
      source parties with an e-mail but no target counterpart (in ``source`` order).
    * ``unmatched_target`` follows the same shape, mirrored for ``target``.
    """

    matches: tuple[PartyMatch, ...]
    unmatched_source: tuple[UnmatchedParty, ...]
    unmatched_target: tuple[UnmatchedParty, ...]


def _no_email_dedupe_key(party: Party) -> tuple[str | None, str | None, PartyRole]:
    """Identity key for collapsing literal duplicate no-e-mail parties.

    Two no-e-mail ``Party`` values are the same logical entry only if their display name, party
    id *and* role all agree — matching is on e-mail only, so this never conflates two different
    unmatched people just because they share a role.
    """
    display_name = party.display_name.strip().casefold() if party.display_name else None
    party_id = party.party_id.strip().casefold() if party.party_id else None
    return (display_name, party_id, party.role)


def _index_side(
    parties: Sequence[Party], side: Side
) -> tuple[dict[str, Party], list[UnmatchedParty]]:
    """Split one side's parties into an e-mail index and a deduped no-e-mail unmatched list.

    ``by_email`` keeps the first party seen for a given normalized e-mail; later duplicates
    (including case/whitespace-only variants) are dropped so a repeated owner entry produces one
    match, not several. No-e-mail parties are deduped the same way via
    :func:`_no_email_dedupe_key`. Both collections preserve input order.
    """
    by_email: dict[str, Party] = {}
    no_email: list[UnmatchedParty] = []
    seen_no_email: set[tuple[str | None, str | None, PartyRole]] = set()
    for party in parties:
        email = normalize_email(party.email)
        if email is not None:
            by_email.setdefault(email, party)
            continue
        key = _no_email_dedupe_key(party)
        if key in seen_no_email:
            continue
        seen_no_email.add(key)
        no_email.append(UnmatchedParty(side=side, party=party, reason=UnmatchedReason.NO_EMAIL))
    return by_email, no_email


def correlate_parties(source: Sequence[Party], target: Sequence[Party]) -> CorrelationResult:
    """Correlate ``source`` parties against ``target`` parties by e-mail only.

    A party with no e-mail is always unmatched (``UnmatchedReason.NO_EMAIL``) — it is never
    matched by ``party_id`` or ``display_name``. Duplicate parties on either side (identical, or
    differing only by case/whitespace in the e-mail) collapse to a single match rather than one
    match per duplicate. Role is preserved on every match; see :attr:`PartyMatch.role` for the
    policy when the two sides disagree.

    This function is pure and synchronous: no I/O, no logging, deterministic output for
    deterministic input (see :class:`CorrelationResult` for the ordering contract).
    """
    source_by_email, unmatched_source = _index_side(source, "source")
    target_by_email, unmatched_target = _index_side(target, "target")

    matches: list[PartyMatch] = []
    matched_emails: set[str] = set()
    for email, source_party in source_by_email.items():
        target_party = target_by_email.get(email)
        if target_party is None:
            unmatched_source.append(
                UnmatchedParty(side="source", party=source_party, reason=UnmatchedReason.NO_MATCH)
            )
            continue
        matches.append(PartyMatch(email=email, source=source_party, target=target_party))
        matched_emails.add(email)

    for email, target_party in target_by_email.items():
        if email not in matched_emails:
            unmatched_target.append(
                UnmatchedParty(side="target", party=target_party, reason=UnmatchedReason.NO_MATCH)
            )

    return CorrelationResult(
        matches=tuple(matches),
        unmatched_source=tuple(unmatched_source),
        unmatched_target=tuple(unmatched_target),
    )
