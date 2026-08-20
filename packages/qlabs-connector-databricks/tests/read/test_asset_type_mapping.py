"""table_type -> AssetType: the neutral classification a Dataset carries."""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.models import AssetType
from qlabs_connector_databricks.read import asset_type_for_table


@pytest.mark.parametrize(
    "table_type",
    ["MANAGED", "EXTERNAL", "FOREIGN", "STREAMING_TABLE", "MANAGED_SHALLOW_CLONE"],
)
def test_physical_table_shapes_map_to_table(table_type: str) -> None:
    assert asset_type_for_table(table_type) is AssetType.TABLE


@pytest.mark.parametrize("table_type", ["VIEW", "MATERIALIZED_VIEW"])
def test_view_shapes_map_to_view(table_type: str) -> None:
    assert asset_type_for_table(table_type) is AssetType.VIEW


def test_unrecognized_table_type_maps_to_other() -> None:
    assert asset_type_for_table("SOME_FUTURE_TYPE") is AssetType.OTHER


def test_missing_table_type_maps_to_other() -> None:
    assert asset_type_for_table(None) is AssetType.OTHER
