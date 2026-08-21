"""Passthrough -> ``custom_attributes``: the listing metadata RS-05 section 2.2 defines but
RS-03 has no neutral field for -- categories, business needs, data attributes, compliance
badges, the data dictionary, V1/V2 targeting -- round-trips verbatim, and the columns
promoted to their own neutral field do not appear twice."""

from __future__ import annotations

from qlabs_connector_snowflake.mapping import (
    MAPPED_LISTING_FIELDS,
    MAPPED_SOURCE_FIELDS,
    map_custom_attributes,
)

from .conftest import make_raw_listing, make_raw_schema, make_raw_table

_OBJECT_STRUCTURAL_FIELDS = (
    frozenset({"TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME"}) | MAPPED_SOURCE_FIELDS
)
_LISTING_STRUCTURAL_FIELDS = frozenset({"global_name", "name"}) | MAPPED_LISTING_FIELDS


def test_comment_is_excluded_by_default() -> None:
    attributes = map_custom_attributes(make_raw_table())

    assert "COMMENT" not in attributes
    assert attributes["TABLE_TYPE"] == "BASE TABLE"


def test_the_owner_column_is_deliberately_kept() -> None:
    """Unlike Databricks, the neutral ``owners`` entry carries only a role display name,
    so the raw owner column stays available for a consumer with a role directory."""
    attributes = map_custom_attributes(make_raw_table())

    assert attributes["TABLE_OWNER"] == "SALES_ENGINEER"


def test_the_native_kind_survives_even_though_asset_type_collapses_it() -> None:
    attributes = map_custom_attributes(
        make_raw_table(TABLE_TYPE="ICEBERG TABLE"), exclude=_OBJECT_STRUCTURAL_FIELDS
    )

    assert attributes["TABLE_TYPE"] == "ICEBERG TABLE"
    assert attributes["IS_TRANSIENT"] == "NO"


def test_a_caller_supplied_exclude_removes_structural_columns_and_keeps_the_rest() -> None:
    attributes = map_custom_attributes(make_raw_table(), exclude=_OBJECT_STRUCTURAL_FIELDS)

    for column in ("TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "COMMENT"):
        assert column not in attributes
    assert attributes["CREATED"] == "1700000000.000000000"
    assert attributes["LAST_ALTERED"] == "1700003600.000000000"


def test_exclusion_is_case_insensitive_on_both_sides() -> None:
    attributes = map_custom_attributes({"Comment": "x", "other": 1}, exclude=frozenset({"COMMENT"}))

    assert attributes == {"other": 1}


def test_listing_metadata_round_trips_byte_identical_including_nested_structures() -> None:
    raw = make_raw_listing()

    attributes = map_custom_attributes(raw, exclude=_LISTING_STRUCTURAL_FIELDS)

    assert attributes["categories"] == raw["categories"]
    assert attributes["business_needs"] == raw["business_needs"]
    assert attributes["data_attributes"] == raw["data_attributes"]
    assert attributes["compliance_badges"] == raw["compliance_badges"]
    assert attributes["data_dictionary"] == raw["data_dictionary"]


def test_v1_targets_and_v2_external_targets_both_round_trip() -> None:
    """RS-05 section 2.3's V1/V2 split has no neutral field; both encodings survive."""
    v1 = map_custom_attributes(make_raw_listing(), exclude=_LISTING_STRUCTURAL_FIELDS)
    v2_raw = make_raw_listing(
        external_targets={"all_organizations": True},
        locations={"access_regions": ["PUBLIC"]},
        pricing_plans=[{"name": "standard"}],
    )
    del v2_raw["targets"]
    v2 = map_custom_attributes(v2_raw, exclude=_LISTING_STRUCTURAL_FIELDS)

    assert v1["targets"] == {"accounts": ["Org1.Account1"]}
    assert "targets" not in v2
    assert v2["external_targets"] == {"all_organizations": True}
    assert v2["locations"] == {"access_regions": ["PUBLIC"]}
    assert v2["pricing_plans"] == [{"name": "standard"}]


def test_a_listing_comment_is_not_dropped_even_though_object_rows_promote_comment() -> None:
    """The listing's ``comment`` is a different field from its ``subtitle``/``description``
    and nothing promotes it, so the caller's exclusion set deliberately leaves it in."""
    attributes = map_custom_attributes(make_raw_listing(), exclude=_LISTING_STRUCTURAL_FIELDS)

    assert attributes["comment"] == "Managed by the commercial analytics team."


def test_the_promoted_listing_columns_do_not_appear_twice() -> None:
    attributes = map_custom_attributes(make_raw_listing(), exclude=_LISTING_STRUCTURAL_FIELDS)

    for column in ("title", "subtitle", "description", "global_name", "name"):
        assert column not in attributes


def test_the_publish_state_stays_in_the_passthrough_because_status_is_lossy() -> None:
    attributes = map_custom_attributes(make_raw_listing(), exclude=_LISTING_STRUCTURAL_FIELDS)

    assert attributes["state"] == "PUBLISHED"
    assert attributes["review_state"] == "APPROVED"


def test_nothing_is_ever_renamed_or_re_nested() -> None:
    raw = make_raw_listing()

    attributes = map_custom_attributes(raw, exclude=_LISTING_STRUCTURAL_FIELDS)

    assert attributes["data_dictionary"]["featured"]["objects"][0]["name"] == "ORDERS"
    assert "data_dictionary" not in attributes["data_dictionary"]


def test_a_schema_row_passes_through_its_non_promoted_columns() -> None:
    attributes = map_custom_attributes(
        make_raw_schema(),
        exclude=frozenset({"CATALOG_NAME", "SCHEMA_NAME"}) | MAPPED_SOURCE_FIELDS,
    )

    assert attributes["SCHEMA_OWNER"] == "SYSADMIN"
    assert attributes["IS_MANAGED_ACCESS"] == "NO"
    assert "COMMENT" not in attributes


def test_an_unknown_future_column_is_preserved_rather_than_dropped() -> None:
    attributes = map_custom_attributes(
        make_raw_table(SOME_FUTURE_COLUMN={"nested": [1, 2, 3]}),
        exclude=_OBJECT_STRUCTURAL_FIELDS,
    )

    assert attributes["SOME_FUTURE_COLUMN"] == {"nested": [1, 2, 3]}


def test_an_empty_row_does_not_raise() -> None:
    assert map_custom_attributes({}) == {}


def test_mapped_source_fields_is_exactly_comment() -> None:
    assert frozenset({"comment"}) == MAPPED_SOURCE_FIELDS


def test_mapped_listing_fields_is_exactly_the_three_promoted_text_columns() -> None:
    assert frozenset({"title", "subtitle", "description"}) == MAPPED_LISTING_FIELDS
