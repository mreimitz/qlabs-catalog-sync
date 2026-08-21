"""``expected_revision`` wiring and the per-request operation cap."""

from __future__ import annotations

from diff_helpers import TARGET_REVISION, qlik_manifest, source, target, target_field

from qlabs_catalog_sync.diff import compute_field_diff
from qlabs_catalog_sync_sdk.manifest import ConcurrencyMode
from qlabs_catalog_sync_sdk.models import EntityType


def test_the_revision_the_diff_was_computed_against_is_carried_through() -> None:
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales v2"}),
        target_envelopes=target({"name": "Sales"}),
        manifest=qlik_manifest(),
    )

    assert plan.expected_revision == TARGET_REVISION
    assert plan.diff.expected_revision == TARGET_REVISION
    assert plan.revision_ambiguous is False


def test_a_target_with_no_revision_carries_none() -> None:
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales v2"}),
        target_envelopes=target({"name": "Sales"}, revision=None),
        manifest=qlik_manifest(),
    )

    assert plan.expected_revision is None
    assert plan.revision_ambiguous is False


def test_a_target_never_seen_before_carries_no_revision() -> None:
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales"}),
        target_envelopes={},
        manifest=qlik_manifest(),
    )

    assert plan.expected_revision is None
    assert plan.revision_ambiguous is False


def test_changed_fields_read_at_different_revisions_are_reported_not_guessed() -> None:
    """Picking one of two revisions would be a fabrication, so neither is claimed."""
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales v2", "tags": ["a", "b"]}),
        target_envelopes={
            "name": target_field("name", "Sales", revision="etag-1"),
            "tags": target_field("tags", ["a"], revision="etag-2"),
        },
        manifest=qlik_manifest(),
    )

    assert plan.changed_field_names == ("name", "tags")
    assert plan.expected_revision is None
    assert plan.revision_ambiguous is True


def test_an_unchanged_field_at_another_revision_does_not_make_the_diff_ambiguous() -> None:
    """Only the fields the write would overwrite decide what the write is guarded against."""
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales v2", "tags": ["a"]}),
        target_envelopes={
            "name": target_field("name", "Sales", revision="etag-1"),
            "tags": target_field("tags", ["a"], revision="etag-2"),
        },
        manifest=qlik_manifest(),
    )

    assert plan.changed_field_names == ("name",)
    assert plan.expected_revision == "etag-1"
    assert plan.revision_ambiguous is False


def test_the_declared_operation_cap_is_carried_for_the_writer() -> None:
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales v2"}),
        target_envelopes=target({"name": "Sales"}),
        manifest=qlik_manifest(),
    )

    assert plan.max_update_operations == 8
    assert plan.operation_count == 1
    assert plan.exceeds_operation_cap is False
    assert plan.request_count == 1


def test_a_plan_over_the_cap_is_flagged_and_never_truncated() -> None:
    """Dropping a real change to fit one request would be silent data loss."""
    capped = qlik_manifest(max_update_operations=2)

    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales v2", "description": "new", "tags": ["a", "b"]}),
        target_envelopes=target({"name": "Sales", "description": "old", "tags": ["a"]}),
        manifest=capped,
    )

    assert plan.operation_count == 3
    assert plan.changed_field_names == ("description", "name", "tags")
    assert plan.exceeds_operation_cap is True
    assert plan.request_count == 2


def test_an_endpoint_with_no_cap_needs_one_request() -> None:
    uncapped = qlik_manifest(allowed_update_paths=None, max_update_operations=None)

    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales v2", "description": "new"}),
        target_envelopes=target({"name": "Sales", "description": "old"}),
        manifest=uncapped,
    )

    assert plan.max_update_operations is None
    assert plan.exceeds_operation_cap is False
    assert plan.request_count == 1


def test_an_endpoint_without_concurrency_still_reports_what_the_envelopes_carried() -> None:
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales v2"}),
        target_envelopes=target({"name": "Sales"}),
        manifest=qlik_manifest(concurrency=ConcurrencyMode.NONE),
    )

    assert plan.expected_revision == TARGET_REVISION
