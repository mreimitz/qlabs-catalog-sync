"""catalog.schema selector-pattern matching: includes what it should, excludes what it
should not (RS-01 4.1 identity; the task's own worked example)."""

from __future__ import annotations

from qlabs_connector_databricks.read import (
    literal_catalog_names,
    matches_any_pattern,
    matches_catalog_schema,
)


def test_wildcard_schema_segment_matches() -> None:
    assert matches_catalog_schema("prod.sales_*", "prod", "sales_eu") is True
    assert matches_catalog_schema("prod.sales_*", "prod", "sales_archive") is True


def test_wrong_catalog_is_excluded_even_with_a_matching_schema_shape() -> None:
    # The task's own example: "staging.sales_x" must not match "prod.sales_*".
    assert matches_catalog_schema("prod.sales_*", "staging", "sales_x") is False


def test_schema_that_does_not_fit_the_glob_is_excluded() -> None:
    assert matches_catalog_schema("prod.sales_*", "prod", "archive_sales") is False


def test_a_three_part_table_full_name_never_matches_a_schema_pattern() -> None:
    # The task's own example: "prod.sales_archive.orders" (a table's full name) must
    # never be treated as if it were a catalog.schema pair.
    assert matches_catalog_schema("prod.sales_*", "prod", "sales_archive.orders") is False


def test_exact_literal_pattern_matches_only_itself() -> None:
    assert matches_catalog_schema("prod.sales", "prod", "sales") is True
    assert matches_catalog_schema("prod.sales", "prod", "sales_eu") is False


def test_malformed_pattern_matches_nothing_rather_than_raising() -> None:
    assert matches_catalog_schema("no-dot-here", "prod", "sales") is False
    assert matches_catalog_schema("too.many.dots", "too", "many") is False


def test_matches_any_pattern_across_a_list() -> None:
    patterns = ["staging.*", "prod.sales_*"]
    assert matches_any_pattern("prod", "sales_eu", patterns) is True
    assert matches_any_pattern("staging", "anything", patterns) is True
    assert matches_any_pattern("finance", "reports", patterns) is False


def test_case_sensitive_matching() -> None:
    # Unity Catalog identifiers are case-sensitive as stored (mirrors the engine's own
    # matches_catalog_schema).
    assert matches_catalog_schema("Prod.Sales", "prod", "sales") is False


def test_literal_catalog_names_lifts_non_wildcarded_catalog_segments() -> None:
    patterns = ["prod.sales_*", "prod.finance", "staging.*", "prod_*.wildcarded"]

    names = literal_catalog_names(patterns)

    # "prod" appears once, in first-seen order; "staging" is literal too; the
    # wildcarded catalog segment ("prod_*") is excluded, not guessed at.
    assert names == ["prod", "staging"]


def test_literal_catalog_names_ignores_malformed_patterns() -> None:
    assert literal_catalog_names(["no-dot-here"]) == []
