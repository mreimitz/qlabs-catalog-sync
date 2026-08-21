"""The review file: readable, parseable, idempotent, and honest about stale decisions."""

from __future__ import annotations

import json

import pytest
from helpers import (
    TARGET_ENDPOINT,
    dbx,
    edit_review_json,
    find_entry,
    qlik,
    read_review_json,
    run_bootstrap,
)

from qlabs_catalog_sync.identity import (
    REVIEW_FILE_KIND,
    REVIEW_FILE_VERSION,
    IdentityResolver,
    ProposalStatus,
    ReviewDecision,
)
from qlabs_catalog_sync_sdk.exceptions import ConnectorError


async def test_the_file_says_what_would_bind_to_what_and_why(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-0001", "sales")
    await run_bootstrap(resolver, [source], [qlik("dp-sales", "sales")])

    document = read_review_json(resolver.review_path)

    assert document["kind"] == REVIEW_FILE_KIND
    assert document["version"] == REVIEW_FILE_VERSION
    assert document["instructions"]  # the file explains itself to whoever opens it

    entry = find_entry(document, source.key)
    assert entry["status"] == "proposed"
    assert entry["decision"] == "pending"
    assert entry["source"]["native_key"] == "sch-0001"
    assert entry["source"]["name"] == "sales"
    assert entry["source"]["parent_path"] == ["main"]
    assert entry["target_endpoint"] == TARGET_ENDPOINT
    assert [c["native_key"] for c in entry["candidates"]] == ["dp-sales"]
    # The rationale names what would bind to what, and the rule that matched them.
    assert "'dp-sales'" in entry["rationale"]
    assert "main/sales" in entry["rationale"]
    assert "case-insensitively" in entry["rationale"]


async def test_the_three_outcomes_are_distinguishable_in_the_file(
    resolver: IdentityResolver,
) -> None:
    sources = [dbx("sch-1", "sales"), dbx("sch-2", "orders"), dbx("sch-3", "returns")]
    await run_bootstrap(
        resolver, sources, [qlik("dp-a", "sales"), qlik("dp-b", "sales"), qlik("dp-c", "orders")]
    )

    document = read_review_json(resolver.review_path)
    statuses = {entry["proposal_id"]: entry["status"] for entry in document["proposals"]}

    assert statuses[sources[0].key] == "ambiguous"
    assert statuses[sources[1].key] == "proposed"
    assert statuses[sources[2].key] == "unmatched"


async def test_the_file_round_trips_through_load_and_save(resolver: IdentityResolver) -> None:
    await run_bootstrap(
        resolver,
        [dbx("sch-1", "sales"), dbx("sch-2", "orders"), dbx("sch-3", "returns")],
        [qlik("dp-a", "sales"), qlik("dp-b", "sales"), qlik("dp-c", "orders")],
    )

    first = await resolver.load_review()
    await resolver.save_review(first)
    second = await resolver.load_review()

    assert second.proposals == first.proposals
    assert len(first.proposals) == 3


async def test_the_file_round_trips_after_a_human_decided_and_it_was_applied(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-1", "sales")
    await run_bootstrap(resolver, [source], [qlik("dp-a", "sales")])
    await resolver.confirm(source.key)

    before = await resolver.load_review()
    await resolver.save_review(before)
    after = await resolver.load_review()

    assert after.proposals == before.proposals
    applied = after.get(source.key)
    assert applied is not None
    assert applied.applied_neutral_id is not None
    assert applied.decision is ReviewDecision.CONFIRM


async def test_rerunning_bootstrap_does_not_duplicate_proposals(
    resolver: IdentityResolver,
) -> None:
    sources = [dbx("sch-1", "sales"), dbx("sch-2", "orders")]
    candidates = [qlik("dp-a", "sales"), qlik("dp-b", "orders")]

    await run_bootstrap(resolver, sources, candidates)
    first = read_review_json(resolver.review_path)["proposals"]
    await run_bootstrap(resolver, sources, candidates)
    await run_bootstrap(resolver, sources, candidates)
    third = read_review_json(resolver.review_path)["proposals"]

    assert len(third) == 2
    # Everything except "when did we last see this" is byte-identical across runs.
    for before, after in zip(first, third, strict=True):
        assert before["last_seen_at"] != after["last_seen_at"]
        del before["last_seen_at"], after["last_seen_at"]
        assert before == after


async def test_rerunning_bootstrap_preserves_a_human_decision_and_note(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-1", "sales")
    candidates = [qlik("dp-a", "sales")]
    await run_bootstrap(resolver, [source], candidates)
    edit_review_json(
        resolver.review_path, source.key, decision="confirm", note="checked with the DP owner"
    )

    await run_bootstrap(resolver, [source], candidates)

    entry = find_entry(read_review_json(resolver.review_path), source.key)
    assert entry["decision"] == "confirm"
    assert entry["note"] == "checked with the DP owner"
    assert entry["superseded_decision"] is None
    # And it still applies afterwards.
    report = await resolver.apply_review_file()
    assert len(report.bound) == 1


async def test_first_proposed_at_survives_a_rerun_while_last_seen_at_moves(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-1", "sales")
    candidates = [qlik("dp-a", "sales")]
    await run_bootstrap(resolver, [source], candidates)
    original = (await resolver.load_review()).get(source.key)
    assert original is not None

    await run_bootstrap(resolver, [source], candidates)
    later = (await resolver.load_review()).get(source.key)

    assert later is not None
    assert later.first_proposed_at == original.first_proposed_at
    assert later.last_seen_at > original.last_seen_at


async def test_a_changed_candidate_set_supersedes_a_stale_decision(
    resolver: IdentityResolver,
) -> None:
    """A verdict about one set of objects is never applied to a different set.

    The human confirmed while there was one candidate. By the next run a second Qlik
    product with the same name exists, so the situation they approved no longer exists:
    the decision is reset to pending, the old verdict is preserved verbatim, and applying
    the file binds nothing.
    """
    source = dbx("sch-1", "sales")
    await run_bootstrap(resolver, [source], [qlik("dp-a", "sales")])
    edit_review_json(resolver.review_path, source.key, decision="confirm")

    report = await run_bootstrap(resolver, [source], [qlik("dp-a", "sales"), qlik("dp-b", "sales")])

    assert report.superseded == (source.key,)
    entry = find_entry(read_review_json(resolver.review_path), source.key)
    assert entry["status"] == "ambiguous"
    assert entry["decision"] == "pending"
    assert entry["chosen_native_key"] is None
    assert "confirm" in entry["superseded_decision"]
    assert "re-review before applying" in entry["superseded_decision"]

    applied = await resolver.apply_review_file()
    assert applied.results == ()
    assert await resolver.list_bindings() == ()


async def test_a_changed_candidate_set_supersedes_a_stale_rejection(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-1", "sales")
    await run_bootstrap(resolver, [source], [qlik("dp-a", "sales")])
    await resolver.reject(source.key, reason="wrong product")

    report = await run_bootstrap(resolver, [source], [qlik("dp-b", "sales")])

    assert report.superseded == (source.key,)
    entry = find_entry(read_review_json(resolver.review_path), source.key)
    assert entry["decision"] == "pending"
    assert "reject" in entry["superseded_decision"]
    assert entry["note"] == "wrong product"  # the human's words are never lost


async def test_an_unchanged_rejection_is_not_re_proposed(resolver: IdentityResolver) -> None:
    source = dbx("sch-1", "sales")
    candidates = [qlik("dp-a", "sales")]
    await run_bootstrap(resolver, [source], candidates)
    await resolver.reject(source.key)

    report = await run_bootstrap(resolver, [source], candidates)

    assert report.superseded == ()
    assert len(report.proposed) == 1  # still the same entry ...
    assert report.proposed[0].decision is ReviewDecision.REJECT  # ... still refused
    assert report.needs_human == ()


async def test_an_applied_binding_survives_a_rerun_untouched(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-1", "sales")
    await run_bootstrap(resolver, [source], [qlik("dp-a", "sales")])
    bound = await resolver.confirm(source.key)

    await run_bootstrap(resolver, [source], [qlik("dp-a", "sales"), qlik("dp-b", "sales")])

    entry = find_entry(read_review_json(resolver.review_path), source.key)
    assert entry["status"] == ProposalStatus.BOUND.value
    assert entry["decision"] == "confirm"
    assert entry["applied_neutral_id"] == str(bound.neutral_id)
    assert entry["superseded_decision"] is None


async def test_the_file_is_written_atomically_and_leaves_no_temp_file(
    resolver: IdentityResolver,
) -> None:
    await run_bootstrap(resolver, [dbx("sch-1", "sales")], [qlik("dp-a", "sales")])

    assert resolver.review_path.exists()
    assert list(resolver.review_path.parent.iterdir()) == [resolver.review_path]


async def test_a_missing_file_reads_as_an_empty_review(resolver: IdentityResolver) -> None:
    review = await resolver.load_review()

    assert review.proposals == ()
    assert not resolver.review_path.exists()


async def test_a_file_that_is_not_json_is_refused(resolver: IdentityResolver) -> None:
    resolver.review_path.parent.mkdir(parents=True, exist_ok=True)
    resolver.review_path.write_text("not json at all", encoding="utf-8")

    with pytest.raises(ConnectorError, match="not valid JSON"):
        await resolver.load_review()


async def test_a_file_of_the_wrong_kind_is_refused(resolver: IdentityResolver) -> None:
    resolver.review_path.parent.mkdir(parents=True, exist_ok=True)
    resolver.review_path.write_text(json.dumps({"kind": "something-else"}), encoding="utf-8")

    with pytest.raises(ConnectorError, match="does not look like an identity review file"):
        await resolver.load_review()


async def test_an_unreadable_decision_value_is_refused_rather_than_guessed(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-1", "sales")
    await run_bootstrap(resolver, [source], [qlik("dp-a", "sales")])
    edit_review_json(resolver.review_path, source.key, decision="yes please")

    with pytest.raises(ConnectorError, match="allowed values are"):
        await resolver.load_review()
