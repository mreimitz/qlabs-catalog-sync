"""Widening, array order, and the null-versus-absent rule.

These are the tests that decide whether the sync rewrites customer data. The emitted
value must be the source's, verbatim: canonicalization exists to answer "did this
change?", never to reshape what gets sent.
"""

from __future__ import annotations

from diff_helpers import qlik_manifest, source, target

from qlabs_catalog_sync.diff import compute_field_diff
from qlabs_catalog_sync_sdk.manifest import (
    CapabilityManifest,
    ConcurrencyMode,
    EntityCapability,
    FieldCapability,
)
from qlabs_catalog_sync_sdk.models import EntityType, FieldUpdateMode


def test_a_partial_update_false_array_widens_to_a_full_replace() -> None:
    result = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"tags": ["c", "a", "b"]}),
        target_envelopes=target({"tags": ["a", "b"]}),
        manifest=qlik_manifest(),
    )

    change = result.change_for("tags")
    assert change is not None
    assert change.mode is FieldUpdateMode.REPLACE
    assert change.is_full_replace is True
    assert result.diff.requires_full_replace("tags") is True


def test_the_widened_value_is_the_complete_source_value_not_a_patch_of_the_target() -> None:
    """One added tag means sending the whole array — the source's whole array."""
    result = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"tags": ["c", "a", "b"]}),
        target_envelopes=target({"tags": ["a", "b"]}),
        manifest=qlik_manifest(),
    )

    change = result.change_for("tags")
    assert change is not None
    assert change.value == ["c", "a", "b"]
    assert change.value != ["c"]
    assert change.value != ["a", "b", "c"]
    assert change.previous == ["a", "b"]


def test_a_partial_update_true_field_stays_a_patch() -> None:
    patchable = CapabilityManifest(
        entities={
            EntityType.DATA_PRODUCT: EntityCapability(
                supported=True,
                identity_keys=["qri"],
                fields={"description": FieldCapability.rw(partial_update=True)},
            )
        },
        concurrency=ConcurrencyMode.NONE,
    )

    result = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"description": "new"}),
        target_envelopes=target({"description": "old"}),
        manifest=patchable,
    )

    change = result.change_for("description")
    assert change is not None
    assert change.mode is FieldUpdateMode.PATCH


def test_reordering_an_order_insensitive_array_is_not_a_change() -> None:
    """The phantom diff. ``tags`` is set-like, so source order is arbitrary noise."""
    result = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"tags": ["revenue", "sales"]}),
        target_envelopes=target({"tags": ["sales", "revenue"]}),
        manifest=qlik_manifest(),
    )

    assert result.is_empty
    assert result.change_for("tags") is None
    assert result.dropped == ()


def test_reordering_dataset_refs_is_not_a_change_either() -> None:
    result = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"dataset_refs": ["ds-2", "ds-1"]}),
        target_envelopes=target({"dataset_refs": ["ds-1", "ds-2"]}),
        manifest=qlik_manifest(),
    )

    assert result.is_empty


def test_a_genuinely_changed_order_insensitive_array_does_produce_one_change() -> None:
    result = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"dataset_refs": ["ds-2", "ds-1", "ds-3"]}),
        target_envelopes=target({"dataset_refs": ["ds-1", "ds-2"]}),
        manifest=qlik_manifest(),
    )

    change = result.change_for("dataset_refs")
    assert change is not None
    assert change.value == ["ds-2", "ds-1", "ds-3"]


def test_a_duplicate_in_a_set_like_array_is_a_real_change_not_a_reorder() -> None:
    result = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"tags": ["sales", "sales"]}),
        target_envelopes=target({"tags": ["sales"]}),
        manifest=qlik_manifest(),
    )

    change = result.change_for("tags")
    assert change is not None
    assert change.value == ["sales", "sales"]


def test_null_at_the_source_clears_the_target() -> None:
    result = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"description": None}),
        target_envelopes=target({"description": "Curated sales datasets"}),
        manifest=qlik_manifest(),
    )

    change = result.change_for("description")
    assert change is not None
    assert change.value is None
    assert change.previous == "Curated sales datasets"


def test_a_field_the_source_did_not_report_leaves_the_target_alone() -> None:
    result = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "Sales"}),
        target_envelopes=target({"name": "Sales", "description": "Curated sales datasets"}),
        manifest=qlik_manifest(),
    )

    assert result.is_empty
    assert result.change_for("description") is None
    assert result.dropped == ()


def test_internal_markdown_whitespace_is_a_real_change_and_survives_verbatim() -> None:
    """Indentation opens code blocks; collapsing it would make a real edit invisible."""
    body = "# Sales\n\n    indented code\n"

    result = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"documentation": body}),
        target_envelopes=target({"documentation": "# Sales\n\nindented code\n"}),
        manifest=qlik_manifest(),
    )

    change = result.change_for("documentation")
    assert change is not None
    assert change.value == body


def test_outer_whitespace_alone_is_not_a_change() -> None:
    result = compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source({"name": "  Sales Analytics  "}),
        target_envelopes=target({"name": "Sales Analytics"}),
        manifest=qlik_manifest(),
    )

    assert result.is_empty
