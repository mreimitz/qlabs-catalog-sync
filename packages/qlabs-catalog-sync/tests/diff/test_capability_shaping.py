"""Capability shaping: what the manifest forbids is never emitted, and always reported."""

from __future__ import annotations

import pytest
from diff_helpers import (
    QLIK_UPDATE_PATHS,
    qlik_manifest,
    readonly_manifest,
    source,
    target,
)

from qlabs_catalog_sync.diff import DropReason, compute_field_diff
from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.manifest import FieldCapabilityMode
from qlabs_catalog_sync_sdk.models import EntityType


def test_a_read_only_field_that_differs_is_dropped_and_reported() -> None:
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales", "status": "active"}),
        target_envelopes=target({"name": "Sales", "status": "draft"}),
        manifest=qlik_manifest(),
    )

    assert plan.is_empty
    assert plan.change_for("status") is None
    dropped = plan.dropped_for("status")
    assert dropped is not None
    assert dropped.reason is DropReason.READ_ONLY
    assert dropped.capability_mode is FieldCapabilityMode.RO
    assert dropped.value == "active"


def test_a_not_applicable_field_that_differs_is_dropped_and_reported() -> None:
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"lineage": ["upstream.table"]}),
        target_envelopes=target({"lineage": []}),
        manifest=qlik_manifest(),
    )

    assert plan.is_empty
    dropped = plan.dropped_for("lineage")
    assert dropped is not None
    assert dropped.reason is DropReason.NOT_APPLICABLE
    assert dropped.capability_mode is FieldCapabilityMode.NA


def test_an_undeclared_field_fails_closed_and_is_reported_as_undeclared() -> None:
    """Silence in a manifest is never permission — an unmentioned field is treated as na."""
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"custom_attributes": {"cost_centre": "42"}}),
        target_envelopes=target({}),
        manifest=qlik_manifest(),
    )

    assert plan.is_empty
    dropped = plan.dropped_for("custom_attributes")
    assert dropped is not None
    assert dropped.reason is DropReason.UNDECLARED
    assert dropped.capability_mode is None


def test_a_read_only_field_that_matches_is_not_reported_because_nothing_was_lost() -> None:
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales v2", "status": "active"}),
        target_envelopes=target({"name": "Sales", "status": "active"}),
        manifest=qlik_manifest(),
    )

    assert plan.changed_field_names == ("name",)
    assert plan.dropped == ()


def test_a_read_only_target_manifest_writes_nothing_and_reports_everything() -> None:
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source(
            {"name": "Sales v2", "description": "new", "tags": ["a"], "owners": []}
        ),
        target_envelopes=target({"name": "Sales", "description": "old", "tags": [], "owners": []}),
        manifest=readonly_manifest(),
    )

    assert plan.is_empty
    assert plan.dropped_field_names == ("description", "name", "tags")
    reasons = {dropped.field: dropped.reason for dropped in plan.dropped}
    assert reasons == {
        "description": DropReason.READ_ONLY,
        "name": DropReason.READ_ONLY,
        "tags": DropReason.NOT_APPLICABLE,
    }


def test_an_entity_type_declared_unsupported_raises_rather_than_looking_in_sync() -> None:
    with pytest.raises(CapabilityError) as excinfo:
        compute_field_diff(
            entity_type=EntityType.GLOSSARY_TERM,
            source_envelopes=source({"name": "EBIT"}),
            target_envelopes=target({}),
            manifest=qlik_manifest(),
            endpoint="qlik",
        )

    assert excinfo.value.retryable is False
    assert excinfo.value.endpoint == "qlik"
    assert excinfo.value.entity_type == EntityType.GLOSSARY_TERM


def test_an_entity_type_the_manifest_never_mentions_also_raises() -> None:
    with pytest.raises(CapabilityError):
        compute_field_diff(
            entity_type=EntityType.CATEGORY,
            source_envelopes=source({"name": "Finance"}),
            target_envelopes=target({}),
            manifest=qlik_manifest(),
        )


def test_a_field_outside_the_allowed_update_paths_is_refused_when_the_mapping_is_given() -> None:
    """``/spaceId`` is in Qlik's changelog vocabulary but not in its PATCH path enum."""
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales v2", "placement": "space-b"}),
        target_envelopes=target({"name": "Sales", "placement": "space-a"}),
        manifest=qlik_manifest(),
        native_update_paths=QLIK_UPDATE_PATHS,
    )

    assert plan.changed_field_names == ("name",)
    dropped = plan.dropped_for("placement")
    assert dropped is not None
    assert dropped.reason is DropReason.NO_UPDATE_PATH
    assert dropped.native_path == "/spaceId"


def test_a_writable_field_with_no_entry_in_the_supplied_mapping_fails_closed() -> None:
    incomplete = {name: path for name, path in QLIK_UPDATE_PATHS.items() if name != "tags"}

    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"tags": ["a", "b"]}),
        target_envelopes=target({"tags": ["a"]}),
        manifest=qlik_manifest(),
        native_update_paths=incomplete,
    )

    assert plan.is_empty
    dropped = plan.dropped_for("tags")
    assert dropped is not None
    assert dropped.reason is DropReason.NO_UPDATE_PATH
    assert dropped.native_path is None


def test_without_a_mapping_the_engine_does_not_guess_the_path_enum() -> None:
    """The connector owns neutral-to-native translation; refusing on a guess would be worse."""
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"placement": "space-b"}),
        target_envelopes=target({"placement": "space-a"}),
        manifest=qlik_manifest(),
    )

    assert plan.changed_field_names == ("placement",)
    assert plan.dropped == ()


def test_an_endpoint_with_no_closed_path_enum_never_refuses_on_paths() -> None:
    open_surface = qlik_manifest(allowed_update_paths=None, max_update_operations=None)

    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"placement": "space-b"}),
        target_envelopes=target({"placement": "space-a"}),
        manifest=open_surface,
        native_update_paths=QLIK_UPDATE_PATHS,
    )

    assert plan.changed_field_names == ("placement",)
    assert plan.max_update_operations is None
