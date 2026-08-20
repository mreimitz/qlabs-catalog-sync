"""Smoke test: the Collibra connector package imports and exposes Connector."""

import qlabs_connector_collibra


def test_import() -> None:
    assert qlabs_connector_collibra.Connector is not None
    assert True
