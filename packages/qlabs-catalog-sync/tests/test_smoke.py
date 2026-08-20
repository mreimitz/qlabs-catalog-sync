"""Smoke test: the engine package imports."""

import qlabs_catalog_sync


def test_import() -> None:
    assert qlabs_catalog_sync is not None
    assert True
