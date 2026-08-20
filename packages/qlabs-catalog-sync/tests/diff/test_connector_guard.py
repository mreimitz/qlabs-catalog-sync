"""The diff reaches the connector already clean.

``Connector.ensure_writable`` is the write connector's belt and braces, not the diff
engine's excuse to emit junk. These tests run the real guard from the SDK contract
against real plans.
"""

from __future__ import annotations

import pytest
from diff_helpers import (
    QLIK_UPDATE_PATHS,
    QlikLikeConnector,
    readonly_manifest,
    source,
    target,
)

from qlabs_catalog_sync.diff import compute_field_diff
from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.models import EntityType, FieldChange, FieldDiff

MIXED_SOURCE = {
    "name": "Sales v2",
    "description": "Curated sales datasets, refreshed daily",
    "tags": ["sales", "revenue", "finance"],
    "dataset_refs": ["ds-2", "ds-1"],
    "status": "active",
    "lineage": ["upstream.table"],
    "custom_attributes": {"cost_centre": "42"},
    "placement": "space-b",
}

MIXED_TARGET = {
    "name": "Sales",
    "description": "Curated sales datasets",
    "tags": ["sales", "revenue"],
    "dataset_refs": ["ds-1", "ds-2"],
    "status": "draft",
    "lineage": [],
    "custom_attributes": {},
    "placement": "space-a",
}


def test_a_plan_from_a_mixed_entity_passes_the_connectors_own_guard() -> None:
    connector = QlikLikeConnector()

    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source(MIXED_SOURCE),
        target_envelopes=target(MIXED_TARGET),
        manifest=connector.capabilities(),
        native_update_paths=QLIK_UPDATE_PATHS,
        endpoint=connector.name,
    )

    assert plan.changed_field_names == ("description", "name", "tags")
    assert plan.dropped_field_names == ("custom_attributes", "lineage", "placement", "status")

    connector.ensure_writable(plan.diff)


def test_an_empty_plan_also_passes_the_guard() -> None:
    connector = QlikLikeConnector()

    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source(MIXED_SOURCE),
        target_envelopes=target(MIXED_SOURCE),
        manifest=connector.capabilities(),
        endpoint=connector.name,
    )

    assert plan.is_empty
    connector.ensure_writable(plan.diff)


def test_the_guard_is_not_vacuous_a_read_only_field_really_does_raise() -> None:
    connector = QlikLikeConnector()
    hand_built = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="status", value="active")],
    )

    with pytest.raises(CapabilityError):
        connector.ensure_writable(hand_built)


def test_a_read_only_target_produces_a_plan_no_write_connector_would_reject() -> None:
    plan = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales v2", "tags": ["a"]}),
        target_envelopes=target({"name": "Sales", "tags": []}),
        manifest=readonly_manifest(),
    )

    assert plan.is_empty
    QlikLikeConnector(manifest=readonly_manifest()).ensure_writable(plan.diff)
