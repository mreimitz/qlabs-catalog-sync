"""The same source row mapped twice produces identical checksums, and a changed field
changes them -- the property the engine's idempotency rests on (``envelope.py``'s whole
reason for existing).

Every function under test is a pure transform of a plain dict, so this is a direct proof,
not a mocked one: map two independent copies of the same row and compare.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.envelope import build_field_envelopes, compute_checksum, order_for
from qlabs_connector_snowflake.mapping import (
    map_classifications,
    map_custom_attributes,
    map_data_product_fields,
    map_dataset_fields,
    map_listing_fields,
    map_tags,
)

from .conftest import (
    ENDPOINT,
    make_raw_listing,
    make_raw_schema,
    make_raw_table,
    system_tag,
    user_tag,
)


def _fragment_checksums(fragment: dict[str, object]) -> dict[str, str]:
    return {key: compute_checksum(value, order=order_for(key)) for key, value in fragment.items()}


def test_the_same_table_row_maps_to_the_same_checksums() -> None:
    raw = make_raw_table()
    references = [user_tag(), system_tag()]

    first = map_dataset_fields(dict(raw), tag_references=list(references))
    second = map_dataset_fields(dict(raw), tag_references=list(references))

    assert set(first) == set(second)
    assert _fragment_checksums(first) == _fragment_checksums(second)


def test_the_same_schema_row_maps_to_the_same_checksums() -> None:
    raw = make_raw_schema()

    first = map_data_product_fields(dict(raw), tag_references=[user_tag()])
    second = map_data_product_fields(dict(raw), tag_references=[user_tag()])

    assert _fragment_checksums(first) == _fragment_checksums(second)


def test_the_same_listing_row_maps_to_the_same_checksums() -> None:
    raw = make_raw_listing()

    first = map_listing_fields(dict(raw))
    second = map_listing_fields(dict(raw))

    assert _fragment_checksums(first) == _fragment_checksums(second)


def test_a_changed_comment_changes_the_description_checksum() -> None:
    unchanged = map_dataset_fields(make_raw_table())
    changed = map_dataset_fields(make_raw_table(COMMENT="Order header rows (v2)."))

    assert compute_checksum(unchanged["description"]) != compute_checksum(changed["description"])


def test_a_changed_owner_role_changes_the_owners_checksum() -> None:
    unchanged = map_dataset_fields(make_raw_table())
    changed = map_dataset_fields(make_raw_table(TABLE_OWNER="FINANCE_ENGINEER"))

    assert compute_checksum(unchanged["owners"], order=order_for("owners")) != compute_checksum(
        changed["owners"], order=order_for("owners")
    )


def test_a_changed_tag_value_changes_the_tags_checksum() -> None:
    unchanged = map_tags([user_tag("COST_CENTER", "commerce")])["tags"]
    changed = map_tags([user_tag("COST_CENTER", "finance")])["tags"]

    order = order_for("tags")
    assert compute_checksum(unchanged, order=order) != compute_checksum(changed, order=order)


def test_reordered_tags_hash_identically_because_tags_are_order_insensitive() -> None:
    """``tags`` is one of ``envelope.py``'s ``ORDER_INSENSITIVE_FIELDS``, so a source that
    returns the same set in a different order must not look like a change."""
    forwards = map_tags([user_tag("A", "1"), user_tag("B", "2")])["tags"]
    backwards = map_tags([user_tag("B", "2"), user_tag("A", "1")])["tags"]

    order = order_for("tags")
    assert compute_checksum(forwards, order=order) == compute_checksum(backwards, order=order)


def test_reordered_classifications_hash_identically_too() -> None:
    forwards = map_classifications(
        [system_tag("PRIVACY_CATEGORY", "IDENTIFIER"), system_tag("SEMANTIC_CATEGORY", "NAME")]
    )["classifications"]
    backwards = map_classifications(
        [system_tag("SEMANTIC_CATEGORY", "NAME"), system_tag("PRIVACY_CATEGORY", "IDENTIFIER")]
    )["classifications"]

    order = order_for("classifications")
    assert compute_checksum(forwards, order=order) == compute_checksum(backwards, order=order)


def test_custom_attributes_checksum_is_stable_including_nested_listing_metadata() -> None:
    raw = make_raw_listing()

    first = map_custom_attributes(dict(raw))
    second = map_custom_attributes(dict(raw))

    assert compute_checksum(first) == compute_checksum(second)


def test_a_changed_nested_listing_attribute_changes_the_checksum() -> None:
    unchanged = map_custom_attributes(make_raw_listing())
    changed = map_custom_attributes(
        make_raw_listing(data_attributes={"refresh_rate": "HOURLY", "geography": ["EU"]})
    )

    assert compute_checksum(unchanged) != compute_checksum(changed)


def test_a_whole_envelope_sidecar_is_reproducible() -> None:
    """What ``read.py`` actually builds: one envelope per populated neutral field, each
    carrying the source endpoint and a ``sha256:`` checksum."""
    raw = make_raw_table()
    content = map_dataset_fields(raw, tag_references=[user_tag()])
    values: dict[str, object] = {"name": raw["TABLE_NAME"], **content}

    first = build_field_envelopes(values, source_endpoint=ENDPOINT)
    second = build_field_envelopes(dict(values), source_endpoint=ENDPOINT)

    assert set(first) == set(second)
    for name, envelope in first.items():
        assert envelope.source_endpoint == ENDPOINT
        assert envelope.checksum is not None
        assert envelope.checksum.startswith("sha256:")
        assert envelope.checksum == second[name].checksum
