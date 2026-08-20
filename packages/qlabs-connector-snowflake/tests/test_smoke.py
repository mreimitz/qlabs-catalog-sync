"""Smoke test: the Snowflake connector package imports and exposes Connector."""

import qlabs_connector_snowflake


def test_import() -> None:
    assert qlabs_connector_snowflake.Connector is not None
    assert True
