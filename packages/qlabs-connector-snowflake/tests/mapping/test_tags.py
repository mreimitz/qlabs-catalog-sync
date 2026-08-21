"""Tag assignments -> neutral ``tags``: keyed by the tag's fully-qualified name, with the
``NULL``-vs-empty-string distinction preserved and system classification tags routed
elsewhere."""

from __future__ import annotations

from qlabs_connector_snowflake.mapping import (
    TagReference,
    is_system_classification_tag,
    map_tags,
    tag_reference_from_row,
)

from .conftest import system_tag, user_tag


def test_a_tag_assignment_becomes_a_key_value_tag() -> None:
    fields = map_tags([user_tag("COST_CENTER", "commerce")])

    assert set(fields) == {"tags"}
    (tag,) = fields["tags"]
    assert tag.key == "GOVERNANCE.TAGS.COST_CENTER"
    assert tag.value == "commerce"


def test_the_tag_key_is_the_fully_qualified_name_not_the_bare_name() -> None:
    """Two tags sharing a bare name in different schemas are different tags (RS-05 4.3);
    collapsing them to the bare name would merge unrelated classifications."""
    fields = map_tags(
        [
            user_tag("OWNER", "commerce", database="GOVERNANCE", schema="TAGS"),
            user_tag("OWNER", "finance", database="FINANCE", schema="TAGS"),
        ]
    )

    assert {tag.key for tag in fields["tags"]} == {
        "GOVERNANCE.TAGS.OWNER",
        "FINANCE.TAGS.OWNER",
    }


def test_a_null_tag_value_stays_none_and_an_empty_string_stays_empty() -> None:
    fields = map_tags([user_tag("BARE", None), user_tag("EMPTY", "")])

    values = {tag.key: tag.value for tag in fields["tags"]}
    assert values["GOVERNANCE.TAGS.BARE"] is None
    assert values["GOVERNANCE.TAGS.EMPTY"] == ""


def test_tag_names_are_never_case_folded() -> None:
    fields = map_tags([user_tag("MixedCase", "v")])

    assert fields["tags"][0].key.endswith("MixedCase")


def test_system_classification_tags_are_excluded_from_tags() -> None:
    fields = map_tags([user_tag(), system_tag("PRIVACY_CATEGORY", "IDENTIFIER")])

    assert [tag.key for tag in fields["tags"]] == ["GOVERNANCE.TAGS.COST_CENTER"]


def test_the_same_tag_on_several_columns_is_reported_once() -> None:
    fields = map_tags(
        [
            user_tag("PII", "true", column_name="EMAIL"),
            user_tag("PII", "true", column_name="PHONE"),
        ]
    )

    assert len(fields["tags"]) == 1


def test_the_same_tag_with_two_values_is_reported_twice() -> None:
    """De-duplication is on the ``(key, value)`` pair -- two different values are two
    different facts, not a repeat."""
    fields = map_tags([user_tag("PII", "true"), user_tag("PII", "false")])

    assert len(fields["tags"]) == 2


def test_none_means_the_tag_read_never_ran_and_produces_no_fragment() -> None:
    assert map_tags(None) == {}


def test_an_empty_sequence_means_read_and_genuinely_untagged() -> None:
    assert map_tags([]) == {"tags": []}


def test_is_system_classification_matches_the_whole_namespace_not_the_name() -> None:
    """A user-defined tag that happens to be named ``PRIVACY_CATEGORY`` is authored
    metadata and belongs in ``tags``."""
    impostor = user_tag("PRIVACY_CATEGORY", "IDENTIFIER", database="ACME", schema="TAGS")

    assert not is_system_classification_tag(impostor)
    assert is_system_classification_tag(system_tag("PRIVACY_CATEGORY", "IDENTIFIER"))


def test_a_row_maps_to_a_tag_reference_case_insensitively() -> None:
    reference = tag_reference_from_row(
        {
            "TAG_DATABASE": "GOVERNANCE",
            "TAG_SCHEMA": "TAGS",
            "TAG_NAME": "COST_CENTER",
            "TAG_VALUE": "commerce",
            "OBJECT_DATABASE": "SALES_DB",
            "OBJECT_SCHEMA": "PUBLIC",
            "OBJECT_NAME": "ORDERS",
            "COLUMN_NAME": None,
            "DOMAIN": "TABLE",
        }
    )

    assert reference is not None
    assert reference.tag_fqn == "GOVERNANCE.TAGS.COST_CENTER"
    assert reference.object_fqn == "SALES_DB.PUBLIC.ORDERS"
    assert reference.column_name is None


def test_a_row_with_no_tag_name_is_dropped_rather_than_yielding_an_empty_key() -> None:
    assert tag_reference_from_row({"TAG_VALUE": "commerce"}) is None
    assert tag_reference_from_row({"TAG_NAME": ""}) is None
    assert tag_reference_from_row({}) is None


def test_a_row_missing_the_object_columns_still_yields_a_tag_with_no_object_fqn() -> None:
    reference = tag_reference_from_row({"TAG_NAME": "PII", "TAG_VALUE": "true"})

    assert reference is not None
    assert reference.object_fqn is None
    assert reference.tag_fqn == "..PII"


def test_a_non_string_tag_value_is_stringified_but_null_is_preserved() -> None:
    numeric = tag_reference_from_row({"TAG_NAME": "LEVEL", "TAG_VALUE": 3})
    null = tag_reference_from_row({"TAG_NAME": "LEVEL", "TAG_VALUE": None})

    assert numeric is not None
    assert numeric.tag_value == "3"
    assert null is not None
    assert null.tag_value is None


def test_tag_reference_is_hashable_and_frozen() -> None:
    """Frozen so a tag reference can never be mutated between being read and being
    mapped -- the same guarantee the raw rows themselves carry."""
    reference = TagReference(tag_database="A", tag_schema="B", tag_name="C", tag_value=None)

    assert reference.tag_fqn == "A.B.C"
    assert {reference, reference} == {reference}
