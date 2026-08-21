"""``COMMENT`` -> ``description`` (plain), and a listing's long-form ``description`` ->
``documentation`` (markdown) -- plus the absent-vs-empty distinction that decides whether a
sync cycle is a no-op or clears a customer's description downstream."""

from __future__ import annotations

from qlabs_catalog_sync_sdk.envelope import build_envelope, compute_checksum
from qlabs_catalog_sync_sdk.models import TextFormat
from qlabs_connector_snowflake.mapping import map_description, map_documentation

from .conftest import ENDPOINT, make_raw_listing, make_raw_schema, make_raw_table


def test_present_comment_maps_to_plain_text_description() -> None:
    raw = make_raw_table(COMMENT="Order header rows.")

    fields = map_description(raw)

    assert set(fields) == {"description"}
    description = fields["description"]
    assert description is not None
    assert description.text == "Order header rows."
    assert description.format is TextFormat.PLAIN


def test_a_schema_comment_maps_the_same_way() -> None:
    fields = map_description(make_raw_schema(COMMENT="Conformed sales dimensions."))

    description = fields["description"]
    assert description is not None
    assert description.text == "Conformed sales dimensions."
    assert description.format is TextFormat.PLAIN


def test_absent_comment_produces_no_fragment() -> None:
    raw = make_raw_table()
    del raw["COMMENT"]

    assert map_description(raw) == {}


def test_null_comment_produces_an_explicit_none() -> None:
    assert map_description(make_raw_table(COMMENT=None)) == {"description": None}


def test_empty_comment_produces_an_explicit_none() -> None:
    assert map_description(make_raw_table(COMMENT="")) == {"description": None}


def test_whitespace_only_comment_is_still_carried_verbatim() -> None:
    """Whitespace is content until ``envelope.py`` canonicalizes it for hashing; this
    module never strips a value on the way in, it only tells empty from absent."""
    fields = map_description(make_raw_table(COMMENT="   "))

    description = fields["description"]
    assert description is not None
    assert description.text == "   "
    # Canonicalization -- not this module -- is what makes it hash as the empty string.
    assert compute_checksum(description.text) == compute_checksum("")


def test_non_string_comment_is_treated_as_empty_not_raised() -> None:
    assert map_description(make_raw_table(COMMENT=123)) == {"description": None}


def test_lower_cased_comment_column_is_matched_too() -> None:
    """``SHOW``/``DESCRIBE`` lower-case their column names; the same function serves both."""
    fields = map_description({"comment": "from a SHOW row"})

    description = fields["description"]
    assert description is not None
    assert description.text == "from a SHOW row"


def test_absent_vs_empty_have_different_checksum_consequences() -> None:
    """The difference between a no-op cycle and wiping a description downstream."""
    absent = make_raw_table()
    del absent["COMMENT"]

    assert map_description(absent) == {}

    empty = map_description(make_raw_table(COMMENT=""))
    envelope = build_envelope(empty["description"], source_endpoint=ENDPOINT)
    assert envelope.value is None
    assert envelope.checksum == compute_checksum(None)


def test_listing_long_description_maps_to_markdown_documentation() -> None:
    raw = make_raw_listing()

    fields = map_documentation(raw)

    documentation = fields["documentation"]
    assert documentation is not None
    assert documentation.text.startswith("# Daily sales")
    assert documentation.format is TextFormat.MARKDOWN


def test_listing_subtitle_is_the_short_description_not_the_long_one() -> None:
    """The spelling collision RS-05 sets up: a listing's ``description`` is 7,500 chars of
    Markdown and must not land in the neutral one-line ``description``."""
    raw = make_raw_listing()

    short = map_description(raw, source_key="subtitle")["description"]
    long = map_documentation(raw)["documentation"]

    assert short is not None
    assert short.text == "Daily sales by region, refreshed nightly"
    assert short.format is TextFormat.PLAIN
    assert long is not None
    assert long.text != short.text


def test_absent_listing_description_produces_no_documentation_fragment() -> None:
    raw = make_raw_listing()
    del raw["description"]

    assert map_documentation(raw) == {}


def test_null_listing_description_produces_an_explicit_none() -> None:
    assert map_documentation(make_raw_listing(description=None)) == {"documentation": None}


def test_missing_keys_never_raise() -> None:
    assert map_description({}) == {}
    assert map_documentation({}) == {}
