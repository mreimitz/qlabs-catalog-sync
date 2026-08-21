"""Table/view kind -> ``AssetType``: RS-05 section 1.2's six table kinds and four view
kinds collapse onto the neutral vocabulary, and anything unrecognized becomes ``OTHER``
rather than a guess."""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.models import AssetType
from qlabs_connector_snowflake.mapping import asset_type_for_object

from .conftest import make_raw_table, make_raw_view


@pytest.mark.parametrize(
    "table_type",
    [
        "BASE TABLE",
        "TABLE",
        "TRANSIENT TABLE",
        "EXTERNAL TABLE",
        "EVENT TABLE",
        "DYNAMIC TABLE",
        "ICEBERG TABLE",
        "TEMPORARY TABLE",
    ],
)
def test_every_table_kind_maps_to_table(table_type: str) -> None:
    assert asset_type_for_object(make_raw_table(TABLE_TYPE=table_type)) is AssetType.TABLE


@pytest.mark.parametrize(
    "table_type", ["VIEW", "MATERIALIZED VIEW", "SECURE VIEW", "SEMANTIC VIEW"]
)
def test_every_view_kind_maps_to_view(table_type: str) -> None:
    assert asset_type_for_object(make_raw_table(TABLE_TYPE=table_type)) is AssetType.VIEW


def test_table_type_matching_is_case_and_whitespace_insensitive() -> None:
    assert asset_type_for_object({"TABLE_TYPE": "  base table "}) is AssetType.TABLE


def test_underscored_spelling_is_accepted_too() -> None:
    """The exact ``TABLE_TYPE`` vocabulary is tenant-unverified, so both spellings a real
    account might report are handled."""
    assert asset_type_for_object({"TABLE_TYPE": "BASE_TABLE"}) is AssetType.TABLE


def test_a_views_row_with_no_table_type_is_still_a_view() -> None:
    """``INFORMATION_SCHEMA.VIEWS`` carries no ``TABLE_TYPE`` column at all."""
    assert asset_type_for_object(make_raw_view()) is AssetType.VIEW


def test_a_non_secure_views_row_falls_back_to_other_rather_than_guessing() -> None:
    """With neither ``TABLE_TYPE`` nor any positive flag there is nothing to read the kind
    from, and ``OTHER`` is the honest answer."""
    raw = make_raw_view(IS_SECURE="NO")

    assert asset_type_for_object(raw) is AssetType.OTHER


def test_iceberg_and_dynamic_flags_mark_a_table_when_table_type_is_missing() -> None:
    assert asset_type_for_object({"IS_ICEBERG": "YES"}) is AssetType.TABLE
    assert asset_type_for_object({"IS_DYNAMIC": "YES"}) is AssetType.TABLE
    assert asset_type_for_object({"IS_TRANSIENT": "YES"}) is AssetType.TABLE


def test_a_real_boolean_flag_is_honored_as_well_as_the_yes_no_string() -> None:
    """The SQL REST API's JSON encoding of a boolean column is tenant-unverified."""
    assert asset_type_for_object({"IS_MATERIALIZED": True}) is AssetType.VIEW
    assert asset_type_for_object({"IS_MATERIALIZED": False}) is AssetType.OTHER


def test_an_unrecognized_kind_maps_to_other() -> None:
    assert asset_type_for_object({"TABLE_TYPE": "SOME_FUTURE_KIND"}) is AssetType.OTHER


def test_a_missing_or_null_kind_maps_to_other() -> None:
    assert asset_type_for_object({}) is AssetType.OTHER
    assert asset_type_for_object({"TABLE_TYPE": None}) is AssetType.OTHER
    assert asset_type_for_object({"TABLE_TYPE": ""}) is AssetType.OTHER


def test_a_volume_never_appears() -> None:
    """Snowflake stages are a different object type this connector does not read, so the
    neutral ``VOLUME`` kind is unreachable here by construction."""
    for table_type in ["BASE TABLE", "VIEW", "STAGE", ""]:
        assert asset_type_for_object({"TABLE_TYPE": table_type}) is not AssetType.VOLUME
