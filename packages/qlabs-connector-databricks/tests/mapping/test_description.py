"""``comment`` -> ``description``: plain text, and the absent-vs-empty distinction that decides
whether a sync cycle is a no-op or clears a customer's Qlik description."""

from __future__ import annotations

from qlabs_catalog_sync_sdk.envelope import build_envelope, compute_checksum
from qlabs_catalog_sync_sdk.models import TextFormat
from qlabs_connector_databricks.mapping import map_description

from .conftest import make_raw_schema


def test_present_comment_maps_to_plain_text_description() -> None:
    raw = make_raw_schema(comment="Sales domain schema.")

    fields = map_description(raw)

    assert set(fields) == {"description"}
    description = fields["description"]
    assert description is not None
    assert description.text == "Sales domain schema."
    assert description.format is TextFormat.PLAIN


def test_absent_comment_produces_no_fragment() -> None:
    raw = make_raw_schema()
    del raw["comment"]

    fields = map_description(raw)

    assert fields == {}


def test_empty_comment_produces_an_explicit_none() -> None:
    raw = make_raw_schema(comment="")

    fields = map_description(raw)

    assert fields == {"description": None}


def test_null_comment_produces_an_explicit_none() -> None:
    raw = make_raw_schema(comment=None)

    fields = map_description(raw)

    assert fields == {"description": None}


def test_non_string_comment_is_treated_as_empty_not_raised() -> None:
    raw = make_raw_schema(comment=123)

    fields = map_description(raw)

    assert fields == {"description": None}


def test_absent_vs_empty_have_different_checksum_consequences() -> None:
    """This is the difference between a no-op cycle and wiping a description downstream."""
    absent_raw = make_raw_schema()
    del absent_raw["comment"]
    empty_raw = make_raw_schema(comment="")

    absent_fields = map_description(absent_raw)
    empty_fields = map_description(empty_raw)

    # Absent: no fragment at all, so a caller never builds an envelope for "description" --
    # the field is never reported changed, and the target is left alone.
    assert absent_fields == {}
    assert "description" not in absent_fields

    # Empty: a fragment IS produced, carrying an explicit `None`. Run through build_envelope
    # (exactly what read.py's build_field_envelopes does per-field) this yields a real
    # envelope whose checksum is over `None` specifically -- distinct from "no envelope at
    # all" -- which is what lets the engine tell "clear the field" apart from "say nothing".
    envelope = build_envelope(empty_fields["description"], source_endpoint="databricks")
    assert envelope.value is None
    assert envelope.checksum == compute_checksum(None)


def test_missing_comment_key_does_not_raise() -> None:
    assert map_description({}) == {}


def test_custom_source_key_is_honored() -> None:
    fields = map_description({"note": "hello"}, source_key="note")

    assert fields["description"] is not None
    assert fields["description"].text == "hello"
