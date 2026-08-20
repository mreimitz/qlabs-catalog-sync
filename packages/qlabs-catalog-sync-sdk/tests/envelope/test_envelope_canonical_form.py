"""Rules 1-5 and 8-10: key order, whitespace, unicode, numbers, and null versus absent."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from qlabs_catalog_sync_sdk.envelope import (
    ArrayOrder,
    canonical_json,
    canonicalize,
    compute_checksum,
    to_json_value,
)
from qlabs_catalog_sync_sdk.models import Party, PartyRole, TextField

# --- rule 1: key order ------------------------------------------------------------------


def test_object_keys_are_sorted_at_every_depth() -> None:
    value = {"b": {"z": 1, "a": {"n": 2, "m": 3}}, "a": 0}
    assert canonical_json(value) == '{"a":0,"b":{"a":{"m":3,"n":2},"z":1}}'


def test_key_order_does_not_change_the_checksum() -> None:
    assert compute_checksum({"a": 1, "b": 2}) == compute_checksum({"b": 2, "a": 1})


def test_key_order_inside_a_list_element_does_not_change_the_checksum() -> None:
    left = {"items": [{"key": "k", "value": "v"}, {"key": "j", "value": "w"}]}
    right = {"items": [{"value": "v", "key": "k"}, {"value": "w", "key": "j"}]}
    assert compute_checksum(left) == compute_checksum(right)


# --- rules 2-4: whitespace ----------------------------------------------------------------


def test_line_endings_normalize() -> None:
    assert compute_checksum("one\r\ntwo") == compute_checksum("one\ntwo")
    assert compute_checksum("one\rtwo") == compute_checksum("one\ntwo")


def test_outer_whitespace_is_stripped() -> None:
    assert compute_checksum("  Retail Sales\n\t") == compute_checksum("Retail Sales")
    assert compute_checksum("   ") == compute_checksum("")


def test_non_ascii_spaces_are_content_and_survive() -> None:
    """U+00A0 is a character somebody typed, not formatting; only ASCII space is trimmed."""
    assert compute_checksum("\u00a0Retail Sales") != compute_checksum("Retail Sales")


def test_internal_whitespace_is_never_collapsed() -> None:
    """Markdown is a synced field; collapsing it would hide real edits."""
    assert compute_checksum("Retail  Sales") != compute_checksum("Retail Sales")
    assert compute_checksum("a\n\n\nb") != compute_checksum("a\n\nb")


def test_end_of_line_whitespace_survives_because_it_is_a_markdown_hard_break() -> None:
    hard_break = "Overview  \nof the product."
    soft = "Overview\nof the product."
    assert compute_checksum(hard_break) != compute_checksum(soft)


def test_markdown_indentation_survives_because_it_opens_a_code_block() -> None:
    fenced = "text\n\n    select 1\n"
    flat = "text\n\nselect 1\n"
    assert compute_checksum(fenced) != compute_checksum(flat)


# --- rule 5: unicode -----------------------------------------------------------------------


def test_strings_are_nfc_normalized() -> None:
    """The same character precomposed (U+00E9) and decomposed ("e" + U+0301)."""
    assert compute_checksum("Caf\u00e9") == compute_checksum("Cafe\u0301")


def test_object_keys_are_nfc_normalized_too() -> None:
    assert compute_checksum({"Caf\u00e9": 1}) == compute_checksum({"Cafe\u0301": 1})


def test_nfkc_folding_is_deliberately_not_applied() -> None:
    """NFKC would fold these together; they are characters a human chose."""
    assert compute_checksum("\ufb01le") != compute_checksum("file")
    assert compute_checksum("\uff21") != compute_checksum("A")


def test_non_ascii_is_hashed_as_utf8_not_as_escapes() -> None:
    assert canonical_json("Caf\u00e9") == '"Caf\u00e9"'


# --- rules 8-9: numbers, booleans, strings --------------------------------------------------


def test_an_integral_float_equals_its_integer() -> None:
    assert compute_checksum(1.0) == compute_checksum(1)
    assert compute_checksum(-0.0) == compute_checksum(0)
    assert canonical_json({"n": 1.0}) == '{"n":1}'


def test_a_fractional_float_keeps_its_value() -> None:
    assert canonical_json(1.5) == "1.5"
    assert compute_checksum(1.5) != compute_checksum(1)


def test_decimals_go_through_the_same_number_rule() -> None:
    assert compute_checksum(Decimal("1.000")) == compute_checksum(1)
    assert compute_checksum(Decimal("1.50")) == compute_checksum(1.5)


def test_numbers_strings_and_booleans_never_coerce_into_each_other() -> None:
    distinct = {
        compute_checksum(1),
        compute_checksum("1"),
        compute_checksum(True),
        compute_checksum(0),
        compute_checksum("0"),
        compute_checksum(False),
        compute_checksum("true"),
    }
    assert len(distinct) == 7


def test_booleans_serialize_as_json_booleans() -> None:
    assert canonical_json({"active": True, "archived": False}) == '{"active":true,"archived":false}'


# --- rule 10: null versus absent ---------------------------------------------------------------


def test_null_and_absent_are_different() -> None:
    assert compute_checksum({"description": None}) != compute_checksum({})


def test_null_empty_string_and_empty_collections_are_all_different() -> None:
    digests = [
        compute_checksum(None),
        compute_checksum(""),
        compute_checksum([]),
        compute_checksum({}),
    ]
    assert len(set(digests)) == 4


# --- value types ---------------------------------------------------------------------------------


def test_pydantic_models_canonicalize_through_their_json_dump() -> None:
    assert compute_checksum(TextField.markdown("**hi**")) == compute_checksum(
        {"text": "**hi**", "format": "markdown"}
    )


def test_models_canonicalize_in_the_python_spelling_not_the_wire_spelling() -> None:
    """Envelope values are ``model_dump(mode="json")`` output, which is snake_case."""
    owner = Party(party_id="u-1", role=PartyRole.OWNER)
    assert compute_checksum(owner) == compute_checksum(
        {"party_id": "u-1", "display_name": None, "email": None, "role": "owner"}
    )
    assert compute_checksum(owner) != compute_checksum(
        {"partyId": "u-1", "displayName": None, "email": None, "role": "owner"}
    )


def test_enums_canonicalize_to_their_value() -> None:
    assert compute_checksum(PartyRole.STEWARD) == compute_checksum("steward")


def test_uuids_canonicalize_to_their_canonical_string() -> None:
    identifier = UUID("11111111-1111-4111-8111-111111111111")
    assert compute_checksum(identifier) == compute_checksum(str(identifier))


def test_tuples_and_lists_are_the_same_json_array() -> None:
    assert compute_checksum(("a", "b")) == compute_checksum(["a", "b"])


def test_nested_structures_normalize_recursively() -> None:
    left = {
        "outer": [
            {"b": {"y": [{"n": 1, "m": 2}], "x": 1}, "a": 1.0},
            {"z": "  trimmed\r\n"},
        ]
    }
    right = {
        "outer": [
            {"a": 1, "b": {"x": 1, "y": [{"m": 2, "n": 1}]}},
            {"z": "trimmed"},
        ]
    }
    assert compute_checksum(left) == compute_checksum(right)


def test_canonicalize_returns_a_sorted_json_tree() -> None:
    canonical = canonicalize({"b": 1, "a": {"d": 1, "c": 2}})
    assert isinstance(canonical, dict)
    assert list(canonical) == ["a", "b"]
    inner = canonical["a"]
    assert isinstance(inner, dict)
    assert list(inner) == ["c", "d"]


def test_to_json_value_is_faithful_where_canonicalize_is_lossy() -> None:
    """The stored value is what gets written; only the checksum is normalized."""
    padded = {"documentation": "  keep\r\n  me  ", "refs": ["z", "a"]}
    assert to_json_value(padded) == padded
    assert canonicalize(padded, order=ArrayOrder.SORTED) == {
        "documentation": "keep\n  me",
        "refs": ["a", "z"],
    }
