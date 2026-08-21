"""Confirmation: the only path by which a proposal becomes a binding.

Covers the callable API the CLI (T2.8) wraps -- ``confirm``, ``reject``,
``apply_review_file``, ``list_proposals``, ``list_bindings``, ``register_source`` -- and
the cases where a confirmation meets a reality that no longer matches it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from helpers import TARGET_ENDPOINT, dbx, edit_review_json, qlik, run_bootstrap

from qlabs_catalog_sync.identity import (
    ConfirmationOutcome,
    IdentityResolver,
    ProposalStatus,
    ReviewDecision,
)
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.exceptions import ConflictError, NotFound
from qlabs_catalog_sync_sdk.models import EntityType

OUT_OF_BAND = datetime(2026, 1, 1, tzinfo=UTC)
"""Timestamp for writes a test makes straight to the store, standing in for another
operator or another process acting between bootstrap and confirmation."""


async def test_confirmation_binds_and_a_later_lookup_resolves_through_the_map(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-0001", "sales")
    candidate = qlik("dp-sales", "sales", secondary_keys={"resourceId": "res-1"})
    report = await run_bootstrap(resolver, [source], [candidate])

    result = await resolver.confirm(report.proposed[0].proposal_id)

    assert result.outcome is ConfirmationOutcome.BOUND
    assert result.target_native_key == "dp-sales"

    binding = await resolver.resolve(source.identity)
    assert binding is not None
    assert binding.neutral_id == result.neutral_id
    assert binding.confirmed is True

    counterpart = await resolver.counterpart(
        binding.neutral_id, TARGET_ENDPOINT, EntityType.DATA_PRODUCT
    )
    assert counterpart is not None
    assert counterpart.identity.native_key == "dp-sales"
    assert counterpart.identity.secondary_keys == {"resourceId": "res-1"}
    assert counterpart.confirmed is True


async def test_the_review_file_records_what_was_applied(resolver: IdentityResolver) -> None:
    report = await run_bootstrap(resolver, [dbx("sch-0001", "sales")], [qlik("dp-sales", "sales")])
    proposal_id = report.proposed[0].proposal_id

    result = await resolver.confirm(proposal_id)

    stored = (await resolver.load_review()).get(proposal_id)
    assert stored is not None
    assert stored.status is ProposalStatus.BOUND
    assert stored.decision is ReviewDecision.CONFIRM
    assert stored.chosen_native_key == "dp-sales"
    assert stored.applied_at is not None
    assert stored.applied_neutral_id == result.neutral_id


async def test_confirming_twice_is_safe(resolver: IdentityResolver) -> None:
    report = await run_bootstrap(resolver, [dbx("sch-0001", "sales")], [qlik("dp-sales", "sales")])
    proposal_id = report.proposed[0].proposal_id

    first = await resolver.confirm(proposal_id)
    second = await resolver.confirm(proposal_id, candidate_native_key="dp-sales")

    assert first.outcome is ConfirmationOutcome.BOUND
    assert second.outcome is ConfirmationOutcome.ALREADY_BOUND
    assert second.neutral_id == first.neutral_id
    # Two rows and no more: the source side and the target side of one neutral id.
    assert len(await resolver.list_bindings()) == 2


async def test_an_ambiguous_proposal_cannot_be_confirmed_without_naming_a_candidate(
    resolver: IdentityResolver,
) -> None:
    report = await run_bootstrap(
        resolver, [dbx("sch-0001", "sales")], [qlik("dp-a", "sales"), qlik("dp-b", "sales")]
    )

    with pytest.raises(ConflictError, match="does not pick"):
        await resolver.confirm(report.ambiguous[0].proposal_id)

    assert await resolver.list_bindings() == ()


async def test_an_ambiguous_proposal_binds_once_a_human_names_the_candidate(
    resolver: IdentityResolver,
) -> None:
    report = await run_bootstrap(
        resolver, [dbx("sch-0001", "sales")], [qlik("dp-a", "sales"), qlik("dp-b", "sales")]
    )

    result = await resolver.confirm(report.ambiguous[0].proposal_id, candidate_native_key="dp-b")

    assert result.outcome is ConfirmationOutcome.BOUND
    assert result.neutral_id is not None
    counterpart = await resolver.counterpart(
        result.neutral_id, TARGET_ENDPOINT, EntityType.DATA_PRODUCT
    )
    assert counterpart is not None
    assert counterpart.identity.native_key == "dp-b"


async def test_an_unmatched_proposal_has_nothing_to_confirm(resolver: IdentityResolver) -> None:
    report = await run_bootstrap(resolver, [dbx("sch-0001", "sales")], [])

    with pytest.raises(ConflictError, match="no candidate to bind"):
        await resolver.confirm(report.unmatched[0].proposal_id)


async def test_confirming_an_unknown_proposal_raises_not_found(resolver: IdentityResolver) -> None:
    await run_bootstrap(resolver, [dbx("sch-0001", "sales")], [qlik("dp-sales", "sales")])

    with pytest.raises(NotFound):
        await resolver.confirm("databricks|data_product|dbx-account-a|nope")


async def test_confirming_a_candidate_the_proposal_never_offered_raises_not_found(
    resolver: IdentityResolver,
) -> None:
    report = await run_bootstrap(resolver, [dbx("sch-0001", "sales")], [qlik("dp-sales", "sales")])

    with pytest.raises(NotFound, match="is not a candidate of proposal"):
        await resolver.confirm(report.proposed[0].proposal_id, candidate_native_key="dp-other")

    assert await resolver.list_bindings() == ()


async def test_confirming_a_target_claimed_since_the_file_was_written_raises_conflict(
    resolver: IdentityResolver, store: StateStore
) -> None:
    """Reality moved under the proposal: the Qlik object got bound to somebody else."""
    candidate = qlik("dp-sales", "sales")
    report = await run_bootstrap(resolver, [dbx("sch-0001", "sales")], [candidate])

    other = uuid4()
    async with store.unit_of_work() as uow:
        await uow.bind_identity(other, candidate.identity, confirmed=True, now=OUT_OF_BAND)

    with pytest.raises(ConflictError, match="already bound to neutral id"):
        await resolver.confirm(report.proposed[0].proposal_id)

    survivor = await resolver.resolve(candidate.identity)
    assert survivor is not None
    assert survivor.neutral_id == other  # the existing binding is untouched


async def test_confirming_a_source_bound_since_the_file_was_written_raises_conflict(
    resolver: IdentityResolver, store: StateStore
) -> None:
    """The source acquired a *different* Qlik counterpart after the proposal was written.

    Applying the stale proposal would silently rebind it. It raises instead, and the
    existing binding stands.
    """
    source = dbx("sch-0001", "sales")
    report = await run_bootstrap(resolver, [source], [qlik("dp-proposed", "sales")])

    neutral_id = uuid4()
    async with store.unit_of_work() as uow:
        await uow.bind_identity(neutral_id, source.identity, confirmed=True, now=OUT_OF_BAND)
        await uow.bind_identity(
            neutral_id, qlik("dp-elsewhere", "sales").identity, confirmed=True, now=OUT_OF_BAND
        )

    with pytest.raises(ConflictError, match="Rebinding is never automatic"):
        await resolver.confirm(report.proposed[0].proposal_id)

    counterpart = await resolver.counterpart(neutral_id, TARGET_ENDPOINT, EntityType.DATA_PRODUCT)
    assert counterpart is not None
    assert counterpart.identity.native_key == "dp-elsewhere"


async def test_reject_records_the_refusal_and_binds_nothing(resolver: IdentityResolver) -> None:
    report = await run_bootstrap(resolver, [dbx("sch-0001", "sales")], [qlik("dp-sales", "sales")])
    proposal_id = report.proposed[0].proposal_id

    result = await resolver.reject(proposal_id, reason="different product, same word")

    assert result.outcome is ConfirmationOutcome.REJECTED
    assert await resolver.list_bindings() == ()
    stored = (await resolver.load_review()).get(proposal_id)
    assert stored is not None
    assert stored.decision is ReviewDecision.REJECT
    assert stored.note == "different product, same word"


async def test_an_applied_proposal_cannot_be_rejected(resolver: IdentityResolver) -> None:
    report = await run_bootstrap(resolver, [dbx("sch-0001", "sales")], [qlik("dp-sales", "sales")])
    proposal_id = report.proposed[0].proposal_id
    await resolver.confirm(proposal_id)

    with pytest.raises(ConflictError, match="already applied"):
        await resolver.reject(proposal_id)


async def test_apply_review_file_binds_every_entry_a_human_confirmed(
    resolver: IdentityResolver,
) -> None:
    """The file drives confirmation: a human edits ``decision``, then the file is applied."""
    sources = [dbx("sch-1", "sales"), dbx("sch-2", "orders")]
    await run_bootstrap(resolver, sources, [qlik("dp-a", "sales"), qlik("dp-b", "orders")])
    for source in sources:
        edit_review_json(resolver.review_path, source.key, decision="confirm")

    report = await resolver.apply_review_file()

    assert report.ok
    assert len(report.bound) == 2
    assert len(await resolver.list_bindings(endpoint=TARGET_ENDPOINT)) == 2
    for source, expected in zip(sources, ["dp-a", "dp-b"], strict=True):
        binding = await resolver.resolve(source.identity)
        assert binding is not None
        counterpart = await resolver.counterpart(
            binding.neutral_id, TARGET_ENDPOINT, EntityType.DATA_PRODUCT
        )
        assert counterpart is not None
        assert counterpart.identity.native_key == expected


async def test_apply_review_file_reports_one_bad_entry_without_blocking_the_rest(
    resolver: IdentityResolver, store: StateStore
) -> None:
    sources = [dbx("sch-1", "sales"), dbx("sch-2", "orders")]
    taken = qlik("dp-a", "sales")
    await run_bootstrap(resolver, sources, [taken, qlik("dp-b", "orders")])
    for source in sources:
        edit_review_json(resolver.review_path, source.key, decision="confirm")

    async with store.unit_of_work() as uow:
        await uow.bind_identity(uuid4(), taken.identity, confirmed=True, now=OUT_OF_BAND)

    report = await resolver.apply_review_file()

    assert not report.ok
    assert [r.proposal_id for r in report.failed] == [sources[0].key]
    assert [r.target_native_key for r in report.bound] == ["dp-b"]


async def test_apply_review_file_skips_what_it_already_applied(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-1", "sales")
    await run_bootstrap(resolver, [source], [qlik("dp-a", "sales")])
    edit_review_json(resolver.review_path, source.key, decision="confirm")

    first = await resolver.apply_review_file()
    second = await resolver.apply_review_file()

    assert [r.outcome for r in first.results] == [ConfirmationOutcome.BOUND]
    assert [r.outcome for r in second.results] == [ConfirmationOutcome.SKIPPED]
    assert len(await resolver.list_bindings()) == 2


async def test_apply_review_file_ignores_pending_and_rejected_entries(
    resolver: IdentityResolver,
) -> None:
    sources = [dbx("sch-1", "sales"), dbx("sch-2", "orders")]
    await run_bootstrap(resolver, sources, [qlik("dp-a", "sales"), qlik("dp-b", "orders")])
    await resolver.reject(sources[1].key)

    report = await resolver.apply_review_file()

    assert report.results == ()
    assert await resolver.list_bindings() == ()


async def test_list_proposals_filters_the_queue_for_the_cli(resolver: IdentityResolver) -> None:
    await run_bootstrap(
        resolver,
        [dbx("sch-1", "sales"), dbx("sch-2", "orders"), dbx("sch-3", "returns")],
        [qlik("dp-a", "sales"), qlik("dp-b", "sales"), qlik("dp-c", "orders")],
    )

    everything = await resolver.list_proposals()
    proposed = await resolver.list_proposals(status=ProposalStatus.PROPOSED)
    ambiguous = await resolver.list_proposals(status=ProposalStatus.AMBIGUOUS)
    needs_human = await resolver.list_proposals(needs_human=True)

    assert len(everything) == 3
    assert [p.proposal_id for p in everything] == sorted(p.proposal_id for p in everything)
    assert len(proposed) == 1
    assert len(ambiguous) == 1
    # An unmatched entry needs a catalog change, not a decision, so it is not in the queue.
    assert len(needs_human) == 2


async def test_list_proposals_on_a_missing_review_file_is_empty_not_an_error(
    resolver: IdentityResolver,
) -> None:
    assert await resolver.list_proposals() == ()


async def test_register_source_is_idempotent_and_binds_only_the_source_side(
    resolver: IdentityResolver,
) -> None:
    """Minting a neutral id for a source object matches nothing, so it claims nothing."""
    source = dbx("sch-0001", "sales")

    first = await resolver.register_source(source.identity)
    second = await resolver.register_source(source.identity)

    assert first.neutral_id == second.neutral_id
    assert await resolver.list_bindings(endpoint=TARGET_ENDPOINT) == ()
    assert len(await resolver.list_bindings()) == 1


async def test_confirmation_reuses_the_neutral_id_the_source_already_has(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-0001", "sales")
    registered = await resolver.register_source(source.identity)

    report = await run_bootstrap(resolver, [source], [qlik("dp-sales", "sales")])
    result = await resolver.confirm(report.proposed[0].proposal_id)

    assert result.neutral_id == registered.neutral_id
