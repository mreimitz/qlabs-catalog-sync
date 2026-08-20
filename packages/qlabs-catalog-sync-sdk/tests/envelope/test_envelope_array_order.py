"""Rule 7: array order is decided per field, never globally."""

from __future__ import annotations

from qlabs_catalog_sync_sdk.envelope import (
    ORDER_INSENSITIVE_FIELDS,
    ArrayOrder,
    canonical_json,
    compute_checksum,
    order_for,
)
from qlabs_catalog_sync_sdk.models import Tag

TAGS = [Tag(key="pii", value="false"), Tag(key="gold")]
TAGS_REORDERED = [Tag(key="gold"), Tag(key="pii", value="false")]


def test_the_default_is_order_preserving() -> None:
    assert compute_checksum(["a", "b"]) != compute_checksum(["b", "a"])


def test_an_order_sensitive_array_is_never_silently_sorted() -> None:
    ordered = ["step-1", "step-2", "step-3"]
    assert canonical_json(ordered) == '["step-1","step-2","step-3"]'
    assert compute_checksum(ordered, order=ArrayOrder.PRESERVE) != compute_checksum(
        list(reversed(ordered)), order=ArrayOrder.PRESERVE
    )


def test_an_order_insensitive_array_ignores_a_reorder() -> None:
    """Qlik datasetIds and tags are full-replace; the source returns them in any order."""
    assert compute_checksum(["ds-2", "ds-1"], order=ArrayOrder.SORTED) == compute_checksum(
        ["ds-1", "ds-2"], order=ArrayOrder.SORTED
    )
    assert compute_checksum(TAGS, order=ArrayOrder.SORTED) == compute_checksum(
        TAGS_REORDERED, order=ArrayOrder.SORTED
    )


def test_an_order_insensitive_array_still_sees_a_real_change() -> None:
    changed = [Tag(key="gold"), Tag(key="pii", value="true")]
    assert compute_checksum(TAGS, order=ArrayOrder.SORTED) != compute_checksum(
        changed, order=ArrayOrder.SORTED
    )
    added = [*TAGS, Tag(key="certified")]
    assert compute_checksum(TAGS, order=ArrayOrder.SORTED) != compute_checksum(
        added, order=ArrayOrder.SORTED
    )


def test_sorting_is_multiset_not_set_so_duplicates_are_kept() -> None:
    """Dropping a duplicate would hide a real change in a full-replace payload."""
    assert compute_checksum(["a", "a", "b"], order=ArrayOrder.SORTED) != compute_checksum(
        ["a", "b"], order=ArrayOrder.SORTED
    )
    assert canonical_json(["b", "a", "a"], order=ArrayOrder.SORTED) == '["a","a","b"]'


def test_sorting_reaches_arrays_at_every_depth_of_the_field_value() -> None:
    left = {"groups": [{"members": ["q", "p"]}, {"members": ["s", "r"]}]}
    right = {"groups": [{"members": ["r", "s"]}, {"members": ["p", "q"]}]}
    assert compute_checksum(left, order=ArrayOrder.SORTED) == compute_checksum(
        right, order=ArrayOrder.SORTED
    )
    assert compute_checksum(left) != compute_checksum(right)


def test_mixed_element_types_sort_deterministically() -> None:
    mixed = [3, "3", None, True, {"a": 1}, ["z"]]
    first = canonical_json(mixed, order=ArrayOrder.SORTED)
    assert first == canonical_json(list(reversed(mixed)), order=ArrayOrder.SORTED)


def test_a_set_is_always_sorted_because_its_iteration_order_is_not_stable() -> None:
    assert canonical_json({"b", "a", "c"}) == '["a","b","c"]'
    assert compute_checksum({"b", "a"}) == compute_checksum(["a", "b"])


def test_the_documented_order_insensitive_fields_are_exactly_these() -> None:
    assert (
        frozenset(
            {
                "asset_links",
                "classifications",
                "dataset_refs",
                "glossary_term_refs",
                "owners",
                "stewards",
                "tags",
                "term_relations",
            }
        )
        == ORDER_INSENSITIVE_FIELDS
    )


def test_order_for_accepts_both_spellings_of_a_neutral_field_name() -> None:
    assert order_for("dataset_refs") is ArrayOrder.SORTED
    assert order_for("datasetRefs") is ArrayOrder.SORTED
    assert order_for("glossaryTermRefs") is ArrayOrder.SORTED
    assert order_for("tags") is ArrayOrder.SORTED


def test_fields_outside_the_registry_keep_their_order() -> None:
    assert order_for("documentation") is ArrayOrder.PRESERVE
    assert order_for("custom_attributes") is ArrayOrder.PRESERVE
    assert order_for("customAttributes") is ArrayOrder.PRESERVE
    assert order_for("name") is ArrayOrder.PRESERVE
