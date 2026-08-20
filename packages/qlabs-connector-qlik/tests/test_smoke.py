"""Smoke test: the Qlik connector package imports and exposes Connector."""

import qlabs_connector_qlik


def test_import() -> None:
    assert qlabs_connector_qlik.Connector is not None
    assert True
