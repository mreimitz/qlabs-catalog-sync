"""Bootstrap matching: what it proposes, what it refuses to guess, and what it never binds.

The load-bearing assertion in this file is repeated on purpose: after any bootstrap run,
``resolver.list_bindings()`` is empty. Bootstrap writes proposals to a file and nothing to
the database, so there is no row a later sync run could promote into a binding.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from helpers import TARGET_ENDPOINT, TARGET_TENANT, dbx, qlik, run_bootstrap

from qlabs_catalog_sync.identity import (
    IdentityResolver,
    ParentPathRule,
    ProposalStatus,
    ReviewDecision,
)
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.models import EntityType


async def test_unknown_source_key_produces_a_proposal_and_binds_nothing(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-0001", "sales")
    candidate = qlik("dp-sales", "sales")

    report = await run_bootstrap(resolver, [source], [candidate])

    assert len(report.proposed) == 1
    proposal = report.proposed[0]
    assert proposal.status is ProposalStatus.PROPOSED
    assert proposal.decision is ReviewDecision.PENDING
    assert [c.identity.native_key for c in proposal.candidates] == ["dp-sales"]
    assert proposal.applied_at is None

    # The whole point: a proposal is not a binding.
    assert await resolver.list_bindings() == ()


async def test_bootstrap_writes_nothing_to_the_identity_map_in_any_outcome(
    resolver: IdentityResolver,
) -> None:
    report = await run_bootstrap(
        resolver,
        [dbx("sch-1", "sales"), dbx("sch-2", "orders"), dbx("sch-3", "returns")],
        [qlik("dp-a", "sales"), qlik("dp-b", "sales"), qlik("dp-c", "orders")],
    )

    assert len(report.ambiguous) == 1  # "sales" matches two
    assert len(report.proposed) == 1  # "orders" matches one
    assert len(report.unmatched) == 1  # "returns" matches none
    assert await resolver.list_bindings() == ()


async def test_two_equally_good_candidates_are_ambiguous_never_a_pick(
    resolver: IdentityResolver,
) -> None:
    report = await run_bootstrap(
        resolver, [dbx("sch-0001", "sales")], [qlik("dp-a", "sales"), qlik("dp-b", "sales")]
    )

    assert report.proposed == ()
    assert len(report.ambiguous) == 1
    proposal = report.ambiguous[0]
    assert proposal.status is ProposalStatus.AMBIGUOUS
    assert sorted(c.identity.native_key for c in proposal.candidates) == ["dp-a", "dp-b"]
    assert proposal.chosen_native_key is None
    assert "no tie-break" in proposal.rationale
    assert await resolver.list_bindings() == ()


async def test_a_case_only_difference_is_ambiguity_not_a_tie_break(
    resolver: IdentityResolver,
) -> None:
    """An exactly-cased candidate does not beat a differently-cased one.

    Case is not evidence. Two Qlik products called ``Sales`` and ``sales`` are a red flag a
    person has to look at, not a reason to prefer the one whose capitalization happens to
    match the source.
    """
    report = await run_bootstrap(
        resolver, [dbx("sch-0001", "sales")], [qlik("dp-exact", "sales"), qlik("dp-caps", "Sales")]
    )

    assert len(report.ambiguous) == 1
    assert sorted(c.identity.native_key for c in report.ambiguous[0].candidates) == [
        "dp-caps",
        "dp-exact",
    ]


async def test_a_source_with_no_candidate_is_reported_unmatched(
    resolver: IdentityResolver,
) -> None:
    report = await run_bootstrap(resolver, [dbx("sch-0001", "sales")], [qlik("dp-x", "invoices")])

    assert report.proposed == ()
    assert report.ambiguous == ()
    assert len(report.unmatched) == 1
    proposal = report.unmatched[0]
    assert proposal.status is ProposalStatus.UNMATCHED
    assert proposal.candidates == ()
    assert "No unbound qlik data_product shares the natural key" in proposal.rationale


async def test_an_empty_target_catalog_reports_every_source_unmatched(
    resolver: IdentityResolver,
) -> None:
    report = await run_bootstrap(resolver, [dbx("sch-0001", "sales")], [])

    assert len(report.unmatched) == 1
    assert report.unmatched[0].target_endpoint == TARGET_ENDPOINT
    assert report.unmatched[0].target_tenant_id == TARGET_TENANT


async def test_a_candidate_already_bound_elsewhere_is_excluded_and_named(
    resolver: IdentityResolver, store: StateStore
) -> None:
    """The only name match belongs to another source object, so it is not offered.

    Offering it would only manufacture a conflict at confirmation time -- but the human
    still has to be told *why* the obvious match is missing, so it is named in the
    rationale and in ``excluded_bound_candidates``.
    """
    taken = qlik("dp-sales", "sales")
    async with store.unit_of_work() as uow:
        await uow.bind_identity(
            uuid4(), taken.identity, confirmed=True, now=datetime(2026, 1, 1, tzinfo=UTC)
        )

    report = await run_bootstrap(resolver, [dbx("sch-0001", "sales")], [taken])

    assert len(report.unmatched) == 1
    proposal = report.unmatched[0]
    assert proposal.candidates == ()
    assert proposal.excluded_bound_candidates == ("dp-sales",)
    assert "already bound to another source object" in proposal.rationale


async def test_names_compare_case_insensitively_and_whitespace_trimmed(
    resolver: IdentityResolver,
) -> None:
    """Databricks object names are case-insensitive for resolution, so matching is too."""
    report = await run_bootstrap(
        resolver, [dbx("sch-0001", "  SALES  ")], [qlik("dp-sales", "sales")]
    )

    assert len(report.proposed) == 1
    assert report.proposed[0].candidates[0].identity.native_key == "dp-sales"


async def test_internal_whitespace_is_not_collapsed(resolver: IdentityResolver) -> None:
    """Whitespace inside a name is part of the name; collapsing it would be a guess."""
    report = await run_bootstrap(
        resolver, [dbx("sch-0001", "Sales  Orders")], [qlik("dp-x", "Sales Orders")]
    )

    assert report.proposed == ()
    assert len(report.unmatched) == 1


async def test_canonically_equivalent_unicode_names_are_the_same_name(
    resolver: IdentityResolver,
) -> None:
    """NFC: ``é`` as one code point and as ``e`` + combining acute are one character."""
    composed = unicodedata.normalize("NFC", "café")
    decomposed = unicodedata.normalize("NFD", "café")
    assert composed != decomposed

    report = await run_bootstrap(resolver, [dbx("sch-0001", decomposed)], [qlik("dp-1", composed)])

    assert len(report.proposed) == 1


async def test_compatibility_equivalent_names_are_not_folded_together(
    resolver: IdentityResolver,
) -> None:
    """NFKC is deliberately not used: a full-width letter is a different character.

    Case folding is applied and does fold the few compatibility characters Unicode
    defines as case mappings (the ``ﬁ`` ligature folds to ``fi``), but the wider NFKC
    compatibility pass -- full-width forms, roman numerals, superscripts -- is not.
    """
    report = await run_bootstrap(
        resolver, [dbx("sch-0001", "ｆｉｎａｎｃｅ")],
        [qlik("dp-1", "finance")]
    )

    assert report.proposed == ()
    assert len(report.unmatched) == 1


async def test_case_folding_still_folds_a_cased_ligature(resolver: IdentityResolver) -> None:
    """Documented consequence of full case folding, asserted rather than left to surprise."""
    report = await run_bootstrap(resolver, [dbx("sch-0001", "ﬁnance")], [qlik("dp-1", "finance")])

    assert len(report.proposed) == 1


async def test_parent_path_is_part_of_the_natural_key(resolver: IdentityResolver) -> None:
    """D1: a UC schema ``main.sales`` is a data product whose parent path is ``main``."""
    report = await run_bootstrap(
        resolver,
        [dbx("sch-0001", "sales", parent_path=("main",))],
        [qlik("dp-1", "sales", parent_path=("staging",))],
    )

    assert report.proposed == ()
    assert len(report.unmatched) == 1


async def test_parent_path_rule_ignore_matches_within_one_scoped_container(
    resolver: IdentityResolver,
) -> None:
    """Inside one already-scoped target container the parent path discriminates nothing."""
    report = await run_bootstrap(
        resolver,
        [dbx("sch-0001", "sales", parent_path=("main",))],
        [qlik("dp-1", "sales", parent_path=("Sales Space",))],
        parent_path_rule=ParentPathRule.IGNORE,
    )

    assert len(report.proposed) == 1
    assert "parent path ignored" in report.proposed[0].rationale


async def test_ignoring_the_parent_path_still_reports_ambiguity(
    resolver: IdentityResolver,
) -> None:
    report = await run_bootstrap(
        resolver,
        [dbx("sch-0001", "sales")],
        [qlik("dp-1", "sales", parent_path=("a",)), qlik("dp-2", "sales", parent_path=("b",))],
        parent_path_rule=ParentPathRule.IGNORE,
    )

    assert len(report.ambiguous) == 1
    assert await resolver.list_bindings() == ()


async def test_entity_type_is_part_of_the_natural_key(resolver: IdentityResolver) -> None:
    report = await run_bootstrap(
        resolver,
        [dbx("sch-0001", "sales", entity_type=EntityType.DATA_PRODUCT)],
        [qlik("ds-1", "sales", entity_type=EntityType.DATASET)],
    )

    assert report.proposed == ()
    assert len(report.unmatched) == 1


async def test_a_name_that_normalizes_to_nothing_matches_nothing(
    resolver: IdentityResolver,
) -> None:
    report = await run_bootstrap(resolver, [dbx("sch-0001", "   ")], [qlik("dp-1", "  ")])

    assert len(report.unmatched) == 1
    assert "normalizes to an empty string" in report.unmatched[0].rationale


async def test_candidates_from_another_tenant_are_refused_outright(
    resolver: IdentityResolver,
) -> None:
    with pytest.raises(ValueError, match="not the target tenant"):
        await run_bootstrap(
            resolver, [dbx("sch-0001", "sales")], [qlik("dp-1", "sales", tenant_id="qlik-other")]
        )


async def test_candidates_from_another_endpoint_are_refused_outright(
    resolver: IdentityResolver,
) -> None:
    with pytest.raises(ValueError, match="not the target endpoint"):
        await run_bootstrap(resolver, [dbx("sch-0001", "sales")], [dbx("sch-9", "sales")])


async def test_contradictory_duplicate_source_objects_are_refused_not_picked(
    resolver: IdentityResolver,
) -> None:
    with pytest.raises(ValueError, match="share the native key"):
        await run_bootstrap(
            resolver, [dbx("sch-0001", "sales"), dbx("sch-0001", "orders")], [qlik("dp-1", "sales")]
        )


async def test_identical_duplicate_source_objects_collapse_to_one_proposal(
    resolver: IdentityResolver,
) -> None:
    report = await run_bootstrap(
        resolver, [dbx("sch-0001", "sales"), dbx("sch-0001", "sales")], [qlik("dp-1", "sales")]
    )

    assert len(report.proposed) == 1
    assert report.considered == 1
