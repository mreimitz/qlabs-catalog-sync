"""Unit tests for ``is_reportable`` -- which records earn a ``run_items`` row.

Direct, hand-built :class:`RecordReport` inputs are the right tool here: this is a pure
predicate over one record's fields, not the recorder's behavior against a real cycle
(that is ``test_recorder_lifecycle.py`` and friends, which build the input through a real
``SyncLoop`` cycle as the task requires). See ``qlabs_catalog_sync.runs.models``'s module
docstring for the full rationale a big metastore's filtered-out objects must not become
10,000 rows nobody reads.
"""

from __future__ import annotations

from qlabs_catalog_sync.runs.recorder import is_reportable
from qlabs_catalog_sync.sync.loop import RecordOutcome, RecordReport, SkipReason
from qlabs_catalog_sync_sdk.models import EntityType


def _record(**overrides: object) -> RecordReport:
    defaults: dict[str, object] = {
        "native_key": "sales.orders",
        "entity_type": EntityType.DATA_PRODUCT,
        "outcome": RecordOutcome.UNCHANGED,
    }
    defaults.update(overrides)
    return RecordReport(**defaults)  # type: ignore[arg-type]


def test_a_plain_unchanged_record_is_not_reportable() -> None:
    assert is_reportable(_record(outcome=RecordOutcome.UNCHANGED)) is False


def test_a_clean_created_record_is_not_reportable() -> None:
    assert is_reportable(_record(outcome=RecordOutcome.CREATED)) is False


def test_a_filtered_not_selected_record_is_not_reportable() -> None:
    """The big-metastore case: thousands of these must never become rows."""
    record = _record(
        outcome=RecordOutcome.FILTERED,
        reason=SkipReason.NOT_SELECTED,
        holds_watermark=False,
    )
    assert is_reportable(record) is False


def test_a_record_that_holds_the_watermark_is_reportable() -> None:
    record = _record(
        outcome=RecordOutcome.SKIPPED,
        reason=SkipReason.NO_TARGET_BINDING,
        holds_watermark=True,
    )
    assert is_reportable(record) is True


def test_an_orphaned_record_is_reportable() -> None:
    assert is_reportable(_record(outcome=RecordOutcome.ORPHANED)) is True


def test_a_failed_record_is_reportable() -> None:
    assert is_reportable(_record(outcome=RecordOutcome.FAILED)) is True


def test_a_written_record_with_unresolved_target_fields_is_reportable() -> None:
    """Decisions D2/D3: the write succeeded, but the target could not resolve every field."""
    record = _record(
        outcome=RecordOutcome.WRITTEN,
        target_skipped_fields=("owners",),
    )
    assert is_reportable(record) is True


def test_a_terminal_skip_reason_alone_is_not_reportable() -> None:
    """NOTHING_TO_WRITE is terminal (see sync/loop.py's _TERMINAL_SKIP_REASONS): it does
    not hold the watermark, and by itself it reports nothing unresolved either."""
    record = _record(
        outcome=RecordOutcome.SKIPPED,
        reason=SkipReason.NOTHING_TO_WRITE,
        holds_watermark=False,
    )
    assert is_reportable(record) is False
