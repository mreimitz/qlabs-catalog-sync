"""Test doubles the SDK ships for connector and engine tests.

WP1 / T1.10. The single public surface of this subpackage: :class:`FakeConnector`, its
config type, its call-log record type, and the two canned capability-manifest shapes
(:mod:`.manifests`) its ``read_only_source`` / ``write_target`` factories default to.

This is production code, not test code — it lives under ``src/`` and is type-checked
strictly like every other module in the SDK — but its entire purpose is to be depended
on *by* tests: the conformance kit (T1.8) tests itself against :class:`FakeConnector`,
and every engine test (WP2, WP7, WP8) exercises the sync loop against it instead of a
real Databricks workspace or Qlik tenant, neither of which exists for this build.

Importable as ``qlabs_catalog_sync_sdk.testing`` regardless of whether the SDK root
re-exports it; see this package's own tests
(``packages/qlabs-catalog-sync-sdk/tests/testing/``) for usage examples of every
behavior described in :class:`FakeConnector`'s docstring.
"""

from __future__ import annotations

from .fake_connector import (
    DEFAULT_TENANT_ID,
    CallRecord,
    ContractMethod,
    FakeConnector,
    FakeConnectorConfig,
)
from .manifests import (
    QLIK_DATA_PRODUCT_ALLOWED_UPDATE_PATHS,
    databricks_shaped_manifest,
    qlik_shaped_manifest,
)

__all__ = [
    "DEFAULT_TENANT_ID",
    "QLIK_DATA_PRODUCT_ALLOWED_UPDATE_PATHS",
    "CallRecord",
    "ContractMethod",
    "FakeConnector",
    "FakeConnectorConfig",
    "databricks_shaped_manifest",
    "qlik_shaped_manifest",
]
