"""Minimality: what the diff engine emits when nothing, or one thing, differs."""

from __future__ import annotations

import pytest
from diff_helpers import qlik_manifest, source, target

from qlabs_catalog_sync.diff import DiffPlan, compute_field_diff
from qlabs_catalog_sync_sdk.envelope import CanonicalizationError
from qlabs_catalog_sync_sdk.models import EntityType, FieldEnvelope

PRODUCT = {
    "name": "Sales Analytics",
    "description": "Curated sales datasets",
    "tags": ["sales", "revenue"],
    "dataset_refs": ["ds-1", "ds-2"],
}


def plan_for(
    source_values: dict[str, object], target_values: dict[str, object]
) -> DiffPlan:
    return compute_field_diff(
        entity_type=EntityType.DATA_PRODUCT,
        source_envelopes=source(source_values),
        target_envelopes=target(target_values),
        manifest=qlik_manifest(),
        endpoint="qlik",
    )


def test_identical_envelopes_produce_an_empty_diff() -> None:
    plan = plan_for(dict(PRODUCT), dict(PRODUCT))

    assert plan.is_empty
    assert plan.diff.changes == []
    assert plan.changed_field_names == ()
    assert plan.dropped == ()
    assert plan.operation_count == 0
    assert plan.request_count == 0


def test_an_empty_diff_is_a_computed_result_not_a_missing_one() -> None:
    """The sync loop turns an empty diff into a skipped write, so the two must differ.

    A plan always comes back for a supported entity type, and it still knows what it was
    computed for. "No diff computed" is not an empty plan — it is no plan at all, i.e.
    the raise covered in ``test_capability_shaping``.
    """
    plan = plan_for(dict(PRODUCT), dict(PRODUCT))

    assert isinstance(plan, DiffPlan)
    assert plan.entity_type is EntityType.DATA_PRODUCT
    assert plan.is_empty is True


def test_one_changed_field_produces_exactly_one_change() -> None:
    changed = dict(PRODUCT) | {"description": "Curated sales datasets, refreshed daily"}

    plan = plan_for(changed, dict(PRODUCT))

    assert plan.changed_field_names == ("description",)
    assert plan.operation_count == 1
    change = plan.change_for("description")
    assert change is not None
    assert change.value == "Curated sales datasets, refreshed daily"
    assert change.previous == "Curated sales datasets"


def test_the_change_carries_the_source_envelope_as_provenance() -> None:
    plan = plan_for(dict(PRODUCT) | {"name": "Sales Analytics v2"}, dict(PRODUCT))

    change = plan.change_for("name")
    assert change is not None
    assert change.envelope is not None
    assert change.envelope.source_endpoint == "databricks"
    assert change.envelope.checksum is not None


def test_an_unchanged_field_never_appears_even_beside_a_changed_one() -> None:
    plan = plan_for(dict(PRODUCT) | {"tags": ["sales", "revenue", "finance"]}, dict(PRODUCT))

    assert plan.changed_field_names == ("tags",)
    assert plan.change_for("name") is None
    assert plan.change_for("description") is None


def test_a_target_that_has_never_been_seen_plans_every_writable_field() -> None:
    plan = plan_for(dict(PRODUCT), {})

    assert plan.changed_field_names == ("dataset_refs", "description", "name", "tags")
    assert all(change.previous is None for change in plan.changes)
    assert plan.expected_revision is None


def test_changed_field_names_are_sorted_so_a_plan_is_reproducible() -> None:
    plan = plan_for(
        dict(PRODUCT) | {"tags": ["x"], "name": "Other", "description": "Other"},
        dict(PRODUCT),
    )

    assert list(plan.changed_field_names) == sorted(plan.changed_field_names)


def test_a_source_envelope_without_a_checksum_is_a_bug_and_says_so() -> None:
    unchecksummed = {
        "name": FieldEnvelope[object](value="Sales Analytics", source_endpoint="databricks")
    }

    with pytest.raises(CanonicalizationError):
        compute_field_diff(
            entity_type=EntityType.DATA_PRODUCT,
            source_envelopes=unchecksummed,  # type: ignore[arg-type]
            target_envelopes=target(dict(PRODUCT)),
            manifest=qlik_manifest(),
        )
