"""Smoke test: the Databricks connector package imports and exposes Connector."""

import qlabs_connector_databricks


def test_import() -> None:
    assert qlabs_connector_databricks.Connector is not None
    assert True
