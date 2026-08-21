"""Every returned entity carries a correct field envelope, and the same source payload
always produces the same checksum.

This is the property the engine's idempotency rests on: a re-run over an unchanged source
must perform zero writes, and a genuinely changed field must be detected. Proven here
against the entity builders directly, so the assertion is about what ``read()`` returns
rather than about how many HTTP calls got it there.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from qlabs_catalog_sync_sdk.envelope import compute_checksum, has_changed, order_for
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_snowflake.mapping import TagReference
from qlabs_connector_snowflake.read import (
    StatementClient,
    build_dataset,
    build_listing_data_product,
    build_schema_data_product,
    read_dataset,
)

from .conftest import (
    COLUMNS_COLUMNS,
    ENDPOINT,
    TABLES_COLUMNS,
    TAG_REFERENCE_COLUMNS,
    TENANT_ID,
    StatementRouter,
    column_row,
    dataset_ref,
    statements_url,
    table_row,
    tag_reference_row,
)

_RAW_TABLE = {
    "TABLE_CATALOG": "SALES_DB",
    "TABLE_SCHEMA": "PUBLIC",
    "TABLE_NAME": "ORDERS",
    "TABLE_OWNER": "SALES_ENGINEER",
    "TABLE_TYPE": "BASE TABLE",
    "COMMENT": "Order header rows.",
    "LAST_ALTERED": "1700003600.000000000",
}

_RAW_SCHEMA = {
    "CATALOG_NAME": "SALES_DB",
    "SCHEMA_NAME": "PUBLIC",
    "SCHEMA_OWNER": "SYSADMIN",
    "COMMENT": "Conformed sales dimensions.",
    "LAST_ALTERED": "1700003600.000000000",
}

_RAW_LISTING = {
    "global_name": "GZTSZAS2KH9",
    "name": "SALES_DAILY",
    "title": "Daily sales",
    "subtitle": "Daily sales by region",
    "description": "# Daily sales",
    "state": "PUBLISHED",
    "owner": "SALES_PROVIDER",
    "updated_on": "1700003600.000000000",
}

_TAGS = [TagReference(tag_database="GOVERNANCE", tag_schema="TAGS", tag_name="PII", tag_value="t")]


def _dataset(**overrides: object) -> object:
    raw = dict(_RAW_TABLE)
    raw.update(overrides)
    return build_dataset(raw, endpoint=ENDPOINT, tenant_id=TENANT_ID, tag_references=_TAGS)


def test_every_populated_field_gets_an_envelope_and_nothing_else_does() -> None:
    dataset = build_dataset(
        dict(_RAW_TABLE), endpoint=ENDPOINT, tenant_id=TENANT_ID, tag_references=_TAGS
    )

    assert set(dataset.field_envelopes) == {
        "name",
        "asset_type",
        "custom_attributes",
        "description",
        "owners",
        "physical_ref",
        "tags",
        "classifications",
    }
    for envelope in dataset.field_envelopes.values():
        assert envelope.source_endpoint == ENDPOINT
        assert envelope.checksum is not None
        assert envelope.checksum.startswith("sha256:")


def test_a_field_the_source_did_not_report_gets_no_envelope() -> None:
    raw = dict(_RAW_TABLE)
    del raw["COMMENT"]
    del raw["TABLE_OWNER"]

    dataset = build_dataset(raw, endpoint=ENDPOINT, tenant_id=TENANT_ID, tag_references=None)

    assert "description" not in dataset.field_envelopes
    assert "owners" not in dataset.field_envelopes
    assert "tags" not in dataset.field_envelopes
    assert "classifications" not in dataset.field_envelopes


def test_last_altered_becomes_the_envelope_provenance_timestamp() -> None:
    dataset = build_dataset(
        dict(_RAW_TABLE), endpoint=ENDPOINT, tenant_id=TENANT_ID, tag_references=None
    )

    stamped = dataset.field_envelopes["name"].last_modified_at
    assert stamped == datetime(2023, 11, 14, 23, 13, 20, tzinfo=UTC)


def test_an_iso_8601_last_altered_is_parsed_too() -> None:
    """The SQL REST API's timestamp encoding is tenant-unverified; both forms are read."""
    dataset = build_dataset(
        {**_RAW_TABLE, "LAST_ALTERED": "2026-03-01T09:30:00+02:00"},
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        tag_references=None,
    )

    assert dataset.field_envelopes["name"].last_modified_at == datetime(
        2026, 3, 1, 7, 30, tzinfo=UTC
    )


def test_an_unparseable_last_altered_costs_only_the_provenance_hint() -> None:
    dataset = build_dataset(
        {**_RAW_TABLE, "LAST_ALTERED": "not a timestamp"},
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        tag_references=None,
    )

    assert dataset.field_envelopes["name"].last_modified_at is None
    assert dataset.name == "ORDERS"


def test_a_missing_last_altered_is_not_invented() -> None:
    raw = dict(_RAW_TABLE)
    del raw["LAST_ALTERED"]

    dataset = build_dataset(raw, endpoint=ENDPOINT, tenant_id=TENANT_ID, tag_references=None)

    assert dataset.field_envelopes["name"].last_modified_at is None


def test_the_same_payload_produces_the_same_checksums_for_every_entity_shape() -> None:
    for build, raw in (
        (build_dataset, _RAW_TABLE),
        (build_schema_data_product, _RAW_SCHEMA),
        (build_listing_data_product, _RAW_LISTING),
    ):
        first = build(dict(raw), endpoint=ENDPOINT, tenant_id=TENANT_ID, tag_references=_TAGS)
        second = build(dict(raw), endpoint=ENDPOINT, tenant_id=TENANT_ID, tag_references=_TAGS)

        assert set(first.field_envelopes) == set(second.field_envelopes)
        for name, envelope in first.field_envelopes.items():
            assert envelope.checksum == second.field_envelopes[name].checksum, name


def test_a_changed_comment_changes_the_description_checksum_and_nothing_else() -> None:
    before = _dataset()
    after = _dataset(COMMENT="Order header rows (v2).")

    assert has_changed(after.field_envelopes["description"], before.field_envelopes["description"])
    assert not has_changed(after.field_envelopes["name"], before.field_envelopes["name"])
    assert not has_changed(after.field_envelopes["owners"], before.field_envelopes["owners"])


def test_a_changed_owner_role_changes_the_owners_checksum() -> None:
    before = _dataset()
    after = _dataset(TABLE_OWNER="FINANCE_ENGINEER")

    assert has_changed(after.field_envelopes["owners"], before.field_envelopes["owners"])


def test_a_changed_tag_value_changes_the_tags_checksum() -> None:
    before = build_dataset(
        dict(_RAW_TABLE), endpoint=ENDPOINT, tenant_id=TENANT_ID, tag_references=_TAGS
    )
    after = build_dataset(
        dict(_RAW_TABLE),
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        tag_references=[
            TagReference(
                tag_database="GOVERNANCE", tag_schema="TAGS", tag_name="PII", tag_value="f"
            )
        ],
    )

    assert has_changed(after.field_envelopes["tags"], before.field_envelopes["tags"])


def test_a_changed_passthrough_column_changes_the_custom_attributes_checksum() -> None:
    before = _dataset()
    after = _dataset(TABLE_TYPE="ICEBERG TABLE")

    assert has_changed(
        after.field_envelopes["custom_attributes"], before.field_envelopes["custom_attributes"]
    )


def test_the_envelope_value_is_the_faithful_projection_not_the_canonical_one() -> None:
    """``envelope.py`` stores what a writer would send and hashes a normalized copy; a
    trailing newline therefore survives in the value while not affecting the checksum."""
    dataset = _dataset(COMMENT="Order header rows.\n")

    envelope = dataset.field_envelopes["description"]
    assert envelope.value == {"text": "Order header rows.\n", "format": "plain"}
    assert envelope.checksum == compute_checksum({"text": "Order header rows.", "format": "plain"})


def test_tags_hash_order_insensitively_as_the_sdk_declares() -> None:
    forwards = [
        TagReference(tag_database="G", tag_schema="T", tag_name="A", tag_value="1"),
        TagReference(tag_database="G", tag_schema="T", tag_name="B", tag_value="2"),
    ]
    a = build_dataset(
        dict(_RAW_TABLE), endpoint=ENDPOINT, tenant_id=TENANT_ID, tag_references=forwards
    )
    b = build_dataset(
        dict(_RAW_TABLE),
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        tag_references=list(reversed(forwards)),
    )

    assert a.field_envelopes["tags"].checksum == b.field_envelopes["tags"].checksum
    assert order_for("tags").value == "sorted"


async def test_two_identical_reads_over_http_produce_identical_checksums(
    respx_mock: object,
    http: HttpEndpoint,
    make_client: Callable[..., StatementClient],
    router: StatementRouter,
) -> None:
    """End to end: the same source, read twice, is a no-op cycle for the engine."""
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row()])
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [column_row()])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [tag_reference_row()])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]
    client = make_client(http)

    first = await read_dataset(client, dataset_ref())
    second = await read_dataset(client, dataset_ref())

    assert first.neutral_id != second.neutral_id  # engine-assigned, fresh per read
    for name, envelope in first.field_envelopes.items():
        assert not has_changed(second.field_envelopes[name], envelope), name
