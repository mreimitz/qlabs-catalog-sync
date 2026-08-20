"""``OrphanPolicy`` (the confirmation threshold) and ``summarize_orphans`` (the digest).

Pure unit tests over :class:`~qlabs_catalog_sync.state.store.OrphanRecord` -- no state
store or connector needed to prove the classification and wording rules themselves.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from qlabs_catalog_sync.state.store import OrphanRecord
from qlabs_catalog_sync.sync.orphans import OrphanConfidence, OrphanPolicy, summarize_orphans
from qlabs_catalog_sync_sdk.models import EntityType

FIRST = datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 20, 10, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 8, 20, 11, 0, 0, tzinfo=UTC)


def _record(**overrides: object) -> OrphanRecord:
    fields: dict[str, object] = {
        "neutral_id": uuid.uuid4(),
        "endpoint": "fake-source",
        "entity_type": EntityType.DATA_PRODUCT,
        "native_key": "sales.orders",
        "first_missing_at": FIRST,
        "last_missing_at": FIRST,
        "last_seen_at": None,
        "resolved_at": None,
    }
    fields.update(overrides)
    return OrphanRecord(**fields)  # type: ignore[arg-type]


def test_a_single_miss_is_tentative_by_default() -> None:
    assert OrphanPolicy().confidence(_record()) is OrphanConfidence.TENTATIVE


def test_a_second_independent_miss_confirms_it() -> None:
    record = _record(last_missing_at=LATER)
    assert OrphanPolicy().confidence(record) is OrphanConfidence.CONFIRMED


def test_require_reconfirmation_false_confirms_on_the_first_miss() -> None:
    policy = OrphanPolicy(require_reconfirmation=False)
    assert policy.confidence(_record()) is OrphanConfidence.CONFIRMED


def test_recommended_action_differs_by_confidence_and_names_the_object() -> None:
    policy = OrphanPolicy()
    tentative = policy.recommended_action(_record(), OrphanConfidence.TENTATIVE)
    confirmed = policy.recommended_action(
        _record(last_missing_at=LATER), OrphanConfidence.CONFIRMED
    )

    assert "No action needed yet" in tentative
    assert "never deletes or deactivates" in confirmed
    assert "sales.orders" in confirmed


def test_summarize_orphans_excludes_resolved_rows() -> None:
    open_record = _record()
    resolved_record = _record(neutral_id=uuid.uuid4(), resolved_at=LATER)

    digest = summarize_orphans([open_record, resolved_record], now=NOW)

    assert [advisory.neutral_id for advisory in digest.advisories] == [open_record.neutral_id]


def test_digest_splits_confirmed_from_tentative_and_groups_by_endpoint() -> None:
    tentative = _record(neutral_id=uuid.uuid4())
    confirmed = _record(neutral_id=uuid.uuid4(), last_missing_at=LATER, endpoint="other-source")

    digest = summarize_orphans([tentative, confirmed], now=NOW)

    assert [a.neutral_id for a in digest.tentative] == [tentative.neutral_id]
    assert [a.neutral_id for a in digest.confirmed] == [confirmed.neutral_id]
    assert set(digest.by_endpoint()) == {"fake-source", "other-source"}


def test_headline_is_never_a_bare_count() -> None:
    digest = summarize_orphans([_record()], now=NOW)
    assert "orphan" in digest.headline()
    assert "never deletes" in digest.headline()
    assert summarize_orphans([], now=NOW).headline() == "No orphans outstanding."


def test_to_json_carries_enough_to_act_on() -> None:
    digest = summarize_orphans([_record(last_missing_at=LATER)], now=NOW)
    payload = digest.to_json()

    assert payload["confirmed_count"] == 1
    assert payload["tentative_count"] == 0
    advisory = payload["advisories"][0]
    assert advisory["confidence"] == "confirmed"
    assert advisory["native_key"] == "sales.orders"
    assert advisory["missing_for_seconds"] == (NOW - FIRST).total_seconds()
    assert advisory["recommended_action"]
