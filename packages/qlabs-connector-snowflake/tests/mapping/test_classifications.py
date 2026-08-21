"""System classification tags -> ``classifications``, kept distinct from user-defined tags.

RS-05 section 4.2 is explicit that ``SNOWFLAKE.CORE.SEMANTIC_CATEGORY`` /
``SNOWFLAKE.CORE.PRIVACY_CATEGORY`` are machine-generated and must not be treated as
authored metadata.
"""

from __future__ import annotations

from qlabs_connector_snowflake.mapping import map_classifications, map_tags

from .conftest import system_tag, user_tag


def test_a_system_tag_renders_as_name_equals_value() -> None:
    fields = map_classifications([system_tag("PRIVACY_CATEGORY", "IDENTIFIER")])

    assert fields == {"classifications": ["PRIVACY_CATEGORY=IDENTIFIER"]}


def test_both_system_categories_are_carried() -> None:
    fields = map_classifications(
        [
            system_tag("PRIVACY_CATEGORY", "IDENTIFIER", column_name="EMAIL"),
            system_tag("SEMANTIC_CATEGORY", "EMAIL", column_name="EMAIL"),
        ]
    )

    assert fields["classifications"] == [
        "PRIVACY_CATEGORY=IDENTIFIER",
        "SEMANTIC_CATEGORY=EMAIL",
    ]


def test_user_defined_tags_are_excluded() -> None:
    fields = map_classifications([user_tag("COST_CENTER", "commerce")])

    assert fields == {"classifications": []}


def test_classifications_and_tags_partition_the_same_input() -> None:
    """Every reference lands in exactly one of the two neutral fields -- neither dropped
    nor double-counted."""
    references = [
        user_tag("COST_CENTER", "commerce"),
        system_tag("PRIVACY_CATEGORY", "IDENTIFIER"),
    ]

    tags = map_tags(references)["tags"]
    classifications = map_classifications(references)["classifications"]

    assert len(tags) == 1
    assert len(classifications) == 1


def test_column_level_categories_aggregate_onto_the_table_and_deduplicate() -> None:
    """Classification is applied per column (RS-05 1.3); the same category on four columns
    is one fact about the table."""
    fields = map_classifications(
        [
            system_tag("PRIVACY_CATEGORY", "IDENTIFIER", column_name="EMAIL"),
            system_tag("PRIVACY_CATEGORY", "IDENTIFIER", column_name="PHONE"),
            system_tag("PRIVACY_CATEGORY", "QUASI_IDENTIFIER", column_name="POSTCODE"),
        ]
    )

    assert fields["classifications"] == [
        "PRIVACY_CATEGORY=IDENTIFIER",
        "PRIVACY_CATEGORY=QUASI_IDENTIFIER",
    ]


def test_a_system_tag_with_a_null_value_contributes_its_bare_name() -> None:
    fields = map_classifications([system_tag("SEMANTIC_CATEGORY", None)])

    assert fields == {"classifications": ["SEMANTIC_CATEGORY"]}


def test_none_means_the_tag_read_never_ran_and_produces_no_fragment() -> None:
    assert map_classifications(None) == {}


def test_an_empty_sequence_means_read_and_genuinely_unclassified() -> None:
    assert map_classifications([]) == {"classifications": []}
