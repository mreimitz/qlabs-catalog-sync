"""Owner/Party best-effort e-mail correlation (T7.3).

Proves the behavior the module's docstring promises: e-mail-only matching, the exact
normalization rule (including what it deliberately does *not* treat as equal), that a party
with no e-mail is reported rather than guessed at, duplicate collapsing, role preservation and
the source-wins policy when the two sides disagree, and deterministic output ordering.
"""

from __future__ import annotations

from qlabs_catalog_sync.party import (
    CorrelationResult,
    UnmatchedReason,
    correlate_parties,
    normalize_email,
)
from qlabs_catalog_sync_sdk.models import Party, PartyRole

# --------------------------------------------------------------------------------------
# normalize_email
# --------------------------------------------------------------------------------------


def test_normalize_email_is_case_folded() -> None:
    assert normalize_email("Alice@Example.COM") == "alice@example.com"


def test_normalize_email_strips_surrounding_whitespace() -> None:
    assert normalize_email("  alice@example.com  ") == "alice@example.com"
    assert normalize_email("\talice@example.com\n") == "alice@example.com"


def test_normalize_email_none_and_blank_both_normalize_to_none() -> None:
    assert normalize_email(None) is None
    assert normalize_email("") is None
    assert normalize_email("   ") is None


def test_normalize_email_does_not_collapse_dot_aliasing() -> None:
    """Gmail-style aliasing is a single-provider convention, not treated as universal."""
    assert normalize_email("a.b@x.com") != normalize_email("ab@x.com")
    assert normalize_email("a.b@x.com") == "a.b@x.com"


def test_normalize_email_does_not_collapse_plus_tag_aliasing() -> None:
    assert normalize_email("a+tag@x.com") != normalize_email("a@x.com")
    assert normalize_email("a+tag@x.com") == "a+tag@x.com"


# --------------------------------------------------------------------------------------
# correlate_parties: matching
# --------------------------------------------------------------------------------------


def test_correlate_exact_email_match() -> None:
    source = [Party(email="alice@example.com", role=PartyRole.OWNER)]
    target = [Party(email="alice@example.com", role=PartyRole.OWNER, party_id="qlik-u-1")]

    result = correlate_parties(source, target)

    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.email == "alice@example.com"
    assert match.source is source[0]
    assert match.target is target[0]
    assert result.unmatched_source == ()
    assert result.unmatched_target == ()


def test_correlate_matches_case_insensitively() -> None:
    source = [Party(email="Alice@Example.com", role=PartyRole.OWNER)]
    target = [Party(email="alice@example.com", role=PartyRole.OWNER)]

    result = correlate_parties(source, target)

    assert len(result.matches) == 1
    assert result.matches[0].email == "alice@example.com"


def test_correlate_matches_whitespace_padded_emails() -> None:
    source = [Party(email="  alice@example.com  ", role=PartyRole.OWNER)]
    target = [Party(email="alice@example.com", role=PartyRole.OWNER)]

    result = correlate_parties(source, target)

    assert len(result.matches) == 1


def test_correlate_does_not_match_dot_or_plus_variants() -> None:
    source = [Party(email="a.b@x.com", role=PartyRole.OWNER)]
    target = [Party(email="ab@x.com", role=PartyRole.OWNER)]

    result = correlate_parties(source, target)

    assert result.matches == ()
    assert len(result.unmatched_source) == 1
    assert len(result.unmatched_target) == 1
    assert result.unmatched_source[0].reason is UnmatchedReason.NO_MATCH
    assert result.unmatched_target[0].reason is UnmatchedReason.NO_MATCH


# --------------------------------------------------------------------------------------
# correlate_parties: no e-mail is reported, never guessed
# --------------------------------------------------------------------------------------


def test_correlate_party_with_no_email_is_reported_unmatched_not_guessed() -> None:
    """A service-principal application id in party_id must never stand in for an e-mail."""
    source = [Party(party_id="app-1234", display_name="svc-ingest", role=PartyRole.OWNER)]
    # Target side happens to carry the exact same display_name/party_id — a naive
    # party_id-or-display_name match would wrongly pair these. Email-only matching must not.
    target = [Party(party_id="app-1234", display_name="svc-ingest", role=PartyRole.OWNER)]

    result = correlate_parties(source, target)

    assert result.matches == ()
    assert len(result.unmatched_source) == 1
    assert len(result.unmatched_target) == 1
    assert result.unmatched_source[0].reason is UnmatchedReason.NO_EMAIL
    assert result.unmatched_target[0].reason is UnmatchedReason.NO_EMAIL
    assert result.unmatched_source[0].label == "svc-ingest"


def test_correlate_blank_email_is_treated_as_no_email() -> None:
    source = [Party(email="   ", display_name="Blank Owner", role=PartyRole.OWNER)]
    target: list[Party] = []

    result = correlate_parties(source, target)

    assert result.matches == ()
    assert result.unmatched_source[0].reason is UnmatchedReason.NO_EMAIL


# --------------------------------------------------------------------------------------
# correlate_parties: duplicates
# --------------------------------------------------------------------------------------


def test_correlate_duplicate_source_parties_produce_one_match() -> None:
    source = [
        Party(email="alice@example.com", role=PartyRole.OWNER),
        Party(email="  Alice@Example.com  ", role=PartyRole.OWNER, display_name="Dupe"),
    ]
    target = [Party(email="alice@example.com", role=PartyRole.OWNER)]

    result = correlate_parties(source, target)

    assert len(result.matches) == 1
    # First occurrence wins as the representative.
    assert result.matches[0].source is source[0]


def test_correlate_duplicate_target_parties_produce_one_match() -> None:
    source = [Party(email="alice@example.com", role=PartyRole.OWNER)]
    target = [
        Party(email="alice@example.com", role=PartyRole.OWNER, party_id="qlik-u-1"),
        Party(email="ALICE@EXAMPLE.COM", role=PartyRole.OWNER, party_id="qlik-u-2"),
    ]

    result = correlate_parties(source, target)

    assert len(result.matches) == 1
    assert result.matches[0].target is target[0]


def test_correlate_duplicate_no_email_parties_are_collapsed() -> None:
    source = [
        Party(display_name="svc-ingest", party_id="app-1", role=PartyRole.OWNER),
        Party(display_name="svc-ingest", party_id="app-1", role=PartyRole.OWNER),
    ]

    result = correlate_parties(source, [])

    assert len(result.unmatched_source) == 1


def test_correlate_distinct_no_email_parties_are_not_conflated() -> None:
    """Two different unmatched owners must not collapse just because they share a role."""
    source = [
        Party(display_name="svc-a", role=PartyRole.OWNER),
        Party(display_name="svc-b", role=PartyRole.OWNER),
    ]

    result = correlate_parties(source, [])

    assert len(result.unmatched_source) == 2
    assert {u.label for u in result.unmatched_source} == {"svc-a", "svc-b"}


# --------------------------------------------------------------------------------------
# correlate_parties: unmatched reporting
# --------------------------------------------------------------------------------------


def test_correlate_unmatched_on_both_sides_are_reported_and_nameable() -> None:
    source = [
        Party(email="alice@example.com", display_name="Alice", role=PartyRole.OWNER),
        Party(email="only-source@example.com", display_name="Only Source", role=PartyRole.OWNER),
    ]
    target = [
        Party(email="alice@example.com", display_name="Alice Q", role=PartyRole.OWNER),
        Party(email="only-target@example.com", display_name="Only Target", role=PartyRole.CONTACT),
    ]

    result = correlate_parties(source, target)

    assert len(result.matches) == 1
    assert [u.label for u in result.unmatched_source] == ["Only Source"]
    assert [u.label for u in result.unmatched_target] == ["Only Target"]
    assert result.unmatched_source[0].reason is UnmatchedReason.NO_MATCH
    assert result.unmatched_target[0].reason is UnmatchedReason.NO_MATCH
    assert result.unmatched_source[0].side == "source"
    assert result.unmatched_target[0].side == "target"


def test_unmatched_party_label_falls_back_through_identifiers() -> None:
    source = [
        Party(display_name="Named", party_id="p-1", role=PartyRole.OWNER),
        Party(party_id="p-2", role=PartyRole.OWNER),
        Party(email="only-email@example.com", role=PartyRole.OWNER),
    ]

    result = correlate_parties(source, [])

    labels = [u.label for u in result.unmatched_source]
    assert labels[0] == "Named"
    assert labels[1] == "p-2"
    # The email-only entry has an email but no target — NO_MATCH, not NO_EMAIL — and still
    # produces a usable label.
    assert labels[2] == "only-email@example.com"


# --------------------------------------------------------------------------------------
# correlate_parties: role
# --------------------------------------------------------------------------------------


def test_correlate_preserves_role_on_a_match() -> None:
    source = [Party(email="grace@example.com", role=PartyRole.STEWARD)]
    target = [Party(email="grace@example.com", role=PartyRole.STEWARD)]

    result = correlate_parties(source, target)

    assert result.matches[0].role is PartyRole.STEWARD


def test_correlate_role_disagreement_is_source_wins() -> None:
    source = [Party(email="grace@example.com", role=PartyRole.OWNER)]
    target = [Party(email="grace@example.com", role=PartyRole.CONTACT)]

    result = correlate_parties(source, target)

    match = result.matches[0]
    assert match.role is PartyRole.OWNER
    assert match.target_role is PartyRole.CONTACT


# --------------------------------------------------------------------------------------
# correlate_parties: determinism and shape
# --------------------------------------------------------------------------------------


def test_correlate_is_deterministic() -> None:
    source = [
        Party(email="b@example.com", role=PartyRole.OWNER),
        Party(email="a@example.com", role=PartyRole.OWNER),
        Party(display_name="no-email", role=PartyRole.OWNER),
    ]
    target = [
        Party(email="a@example.com", role=PartyRole.OWNER),
        Party(email="c@example.com", role=PartyRole.OWNER),
    ]

    first = correlate_parties(source, target)
    second = correlate_parties(source, target)

    assert first == second
    assert isinstance(first, CorrelationResult)
    # Matches follow source order: "b" is unmatched, "a" matches.
    assert [m.email for m in first.matches] == ["a@example.com"]


def test_correlate_match_order_follows_source_first_occurrence() -> None:
    source = [
        Party(email="z@example.com", role=PartyRole.OWNER),
        Party(email="a@example.com", role=PartyRole.OWNER),
    ]
    target = [
        Party(email="a@example.com", role=PartyRole.OWNER),
        Party(email="z@example.com", role=PartyRole.OWNER),
    ]

    result = correlate_parties(source, target)

    assert [m.email for m in result.matches] == ["z@example.com", "a@example.com"]


def test_correlate_empty_inputs_produce_empty_result() -> None:
    result = correlate_parties([], [])

    assert result == CorrelationResult(matches=(), unmatched_source=(), unmatched_target=())
