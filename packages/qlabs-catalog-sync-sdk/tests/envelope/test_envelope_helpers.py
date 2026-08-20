"""Envelope construction, refresh, and the "did this change" comparison the diff calls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import JsonValue

from qlabs_catalog_sync_sdk.envelope import (
    ArrayOrder,
    CanonicalizationError,
    build_envelope,
    build_field_envelopes,
    changed_fields,
    compute_checksum,
    has_changed,
    order_for,
    refresh_checksum,
)
from qlabs_catalog_sync_sdk.models import FieldEnvelope, Party, PartyRole, Tag, TextField

UTC = UTC
CEST = timezone(timedelta(hours=2))

READ_AT = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)
MODIFIED_AT = datetime(2026, 8, 19, 17, 5, 30, tzinfo=CEST)

SOURCE_FIELDS: dict[str, object] = {
    "name": "Retail Sales",
    "description": TextField.plain("Curated retail sales data."),
    "documentation": TextField.markdown("# Retail Sales\n\nOverview  \nof the product.\n"),
    "tags": [Tag(key="domain", value="sales"), Tag(key="gold")],
    "owners": [Party(email="ada@example.com", role=PartyRole.OWNER)],
    "dataset_refs": ["ds-2", "ds-1"],
}


# --- construction ------------------------------------------------------------------------


def test_build_envelope_fills_the_checksum_and_the_provenance() -> None:
    envelope = build_envelope(
        "Retail Sales",
        source_endpoint="databricks",
        source_revision="rev-7",
        last_modified_at=MODIFIED_AT,
        last_synced_at=READ_AT,
    )
    assert envelope.checksum == compute_checksum("Retail Sales")
    assert envelope.source_endpoint == "databricks"
    assert envelope.source_revision == "rev-7"
    assert envelope.last_modified_at == MODIFIED_AT
    assert envelope.last_synced_at == READ_AT


def test_the_stored_value_is_faithful_not_canonical() -> None:
    """What the envelope carries is what a writer will send; only the hash is normalized."""
    envelope = build_envelope(
        {"documentation": "  keep\r\n  me  ", "refs": ["z", "a"]},
        source_endpoint="databricks",
        order=ArrayOrder.SORTED,
    )
    assert envelope.value == {"documentation": "  keep\r\n  me  ", "refs": ["z", "a"]}


def test_an_envelope_rebuilt_from_its_own_stored_value_keeps_its_checksum() -> None:
    """State survives a round trip through the store without inventing a diff."""
    for value in SOURCE_FIELDS.values():
        envelope = build_envelope(value, source_endpoint="databricks", order=ArrayOrder.SORTED)
        rebuilt = build_envelope(
            envelope.value, source_endpoint="databricks", order=ArrayOrder.SORTED
        )
        assert rebuilt.checksum == envelope.checksum


def test_build_envelope_stores_typed_values_as_json() -> None:
    envelope = build_envelope(TextField.markdown("**hi**"), source_endpoint="qlik")
    assert envelope.value == {"text": "**hi**", "format": "markdown"}


def test_build_field_envelopes_applies_the_per_field_ordering_policy() -> None:
    envelopes = build_field_envelopes(
        SOURCE_FIELDS, source_endpoint="databricks", last_synced_at=READ_AT
    )
    assert set(envelopes) == set(SOURCE_FIELDS)
    assert envelopes["tags"].checksum == compute_checksum(
        SOURCE_FIELDS["tags"], order=ArrayOrder.SORTED
    )
    assert envelopes["name"].checksum == compute_checksum(SOURCE_FIELDS["name"])
    assert all(envelope.last_synced_at == READ_AT for envelope in envelopes.values())


# --- refresh -----------------------------------------------------------------------------


def test_refresh_checksum_fills_in_a_missing_checksum() -> None:
    stored: FieldEnvelope[JsonValue] = FieldEnvelope(value="Retail Sales", source_endpoint="qlik")
    assert stored.checksum is None
    refreshed = refresh_checksum(stored)
    assert refreshed.checksum == compute_checksum("Retail Sales")
    assert refreshed.source_endpoint == "qlik"


def test_refresh_checksum_can_stamp_the_sync_time_and_leaves_the_rest_alone() -> None:
    envelope = build_envelope(
        ["b", "a"],
        source_endpoint="databricks",
        source_revision="rev-7",
        last_modified_at=MODIFIED_AT,
    )
    refreshed = refresh_checksum(envelope, order=ArrayOrder.SORTED, last_synced_at=READ_AT)
    assert refreshed.last_synced_at == READ_AT
    assert refreshed.source_revision == "rev-7"
    assert refreshed.last_modified_at == MODIFIED_AT
    assert refreshed.value == ["b", "a"]
    assert refreshed.checksum == compute_checksum(["a", "b"], order=ArrayOrder.SORTED)
    assert refreshed.checksum != envelope.checksum


# --- comparison ---------------------------------------------------------------------------


def test_nothing_stored_means_changed() -> None:
    fresh = build_envelope("Retail Sales", source_endpoint="databricks")
    assert has_changed(fresh, None) is True


def test_an_identical_value_is_not_a_change() -> None:
    fresh = build_envelope("Retail Sales", source_endpoint="databricks")
    stored = build_envelope("Retail Sales", source_endpoint="qlik", last_synced_at=READ_AT)
    assert has_changed(fresh, stored) is False


def test_a_different_value_is_a_change() -> None:
    fresh = build_envelope("Retail Sales v2", source_endpoint="databricks")
    stored = build_envelope("Retail Sales", source_endpoint="qlik")
    assert has_changed(fresh, stored) is True


def test_provenance_alone_is_not_a_change() -> None:
    """A new read time or a new revision counter is not a reason to write."""
    fresh = build_envelope(
        "Retail Sales",
        source_endpoint="databricks",
        source_revision="rev-9",
        last_synced_at=READ_AT,
    )
    stored = build_envelope(
        "Retail Sales",
        source_endpoint="databricks",
        source_revision="rev-7",
        last_synced_at=READ_AT - timedelta(days=3),
    )
    assert has_changed(fresh, stored) is False


def test_a_stored_envelope_without_a_checksum_counts_as_changed() -> None:
    fresh = build_envelope("Retail Sales", source_endpoint="databricks")
    stored: FieldEnvelope[JsonValue] = FieldEnvelope(value="Retail Sales", source_endpoint="qlik")
    assert has_changed(fresh, stored) is True


def test_a_fresh_envelope_without_a_checksum_is_a_programming_error() -> None:
    fresh: FieldEnvelope[JsonValue] = FieldEnvelope(value="x", source_endpoint="databricks")
    with pytest.raises(CanonicalizationError):
        has_changed(fresh, None)


# --- sidecar diff ---------------------------------------------------------------------------


def test_changed_fields_reports_only_what_actually_moved() -> None:
    stored = build_field_envelopes(SOURCE_FIELDS, source_endpoint="databricks")
    next_read = dict(SOURCE_FIELDS)
    next_read["name"] = "Retail Sales EMEA"
    fresh = build_field_envelopes(next_read, source_endpoint="databricks")
    assert changed_fields(fresh, stored) == ["name"]


def test_changed_fields_ignores_a_field_the_source_did_not_report() -> None:
    """Absent is not null: upstream-only sync leaves an unreported field alone."""
    stored = build_field_envelopes(SOURCE_FIELDS, source_endpoint="databricks")
    fresh = build_field_envelopes({"name": SOURCE_FIELDS["name"]}, source_endpoint="databricks")
    assert changed_fields(fresh, stored) == []


def test_changed_fields_reports_a_field_that_has_never_been_seen() -> None:
    fresh = build_field_envelopes(SOURCE_FIELDS, source_endpoint="databricks")
    assert changed_fields(fresh, {}) == sorted(SOURCE_FIELDS)


def test_a_rerun_over_a_noisily_reformatted_source_produces_no_changes() -> None:
    """The idempotency claim, end to end.

    The source re-reports the same product with its tags in another order, its owner list
    reversed, its dataset refs shuffled, CRLF line endings in the documentation, padding
    around the name, and its timestamp in a different offset. Nothing changed, so the
    engine must find nothing to write.
    """
    documentation = SOURCE_FIELDS["documentation"]
    assert isinstance(documentation, TextField)
    tags = SOURCE_FIELDS["tags"]
    assert isinstance(tags, list)

    stored = build_field_envelopes(
        SOURCE_FIELDS, source_endpoint="databricks", last_modified_at=MODIFIED_AT
    )
    noisy: dict[str, object] = {
        "name": "  Retail Sales\n",
        "description": SOURCE_FIELDS["description"],
        "documentation": TextField.markdown(documentation.text.replace("\n", "\r\n")),
        "tags": list(reversed(tags)),
        "owners": SOURCE_FIELDS["owners"],
        "dataset_refs": ["ds-1", "ds-2"],
    }
    fresh = build_field_envelopes(
        noisy,
        source_endpoint="databricks",
        last_modified_at=MODIFIED_AT.astimezone(UTC),
        last_synced_at=READ_AT,
    )
    assert changed_fields(fresh, stored) == []


def test_a_real_edit_still_gets_through_the_noise() -> None:
    stored = build_field_envelopes(SOURCE_FIELDS, source_endpoint="databricks")
    documentation = SOURCE_FIELDS["documentation"]
    assert isinstance(documentation, TextField)
    edited = dict(SOURCE_FIELDS)
    # One added space inside the body: a genuine edit that whitespace collapsing would hide.
    edited["documentation"] = TextField.markdown(
        documentation.text.replace("of the product.", "of  the product.")
    )
    edited["dataset_refs"] = ["ds-1", "ds-2", "ds-3"]
    fresh = build_field_envelopes(edited, source_endpoint="databricks")
    assert changed_fields(fresh, stored) == ["dataset_refs", "documentation"]


def test_order_for_and_build_field_envelopes_agree() -> None:
    for field, value in SOURCE_FIELDS.items():
        envelope = build_field_envelopes({field: value}, source_endpoint="databricks")[field]
        assert envelope.checksum == compute_checksum(value, order=order_for(field))
