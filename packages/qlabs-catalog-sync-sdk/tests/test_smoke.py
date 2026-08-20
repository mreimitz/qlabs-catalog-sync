"""Smoke test: the SDK package imports."""

import qlabs_catalog_sync_sdk


def test_import() -> None:
    assert qlabs_catalog_sync_sdk is not None
    assert True
