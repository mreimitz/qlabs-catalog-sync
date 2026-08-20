"""Conformance test kit.

WP1 / T1.8. A reusable pytest suite every connector runs to be "certified": contract
completeness, round-trip, idempotency, HTTP behavior, and capability-honesty checks,
with a respx/vcrpy harness. Built on the manifest (T1.3) and envelope (T1.6) modules,
and exercised in this SDK's own tests against
:class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` (T1.10) in both its
``read_only_source`` and ``write_target`` shapes.

**Entry point:** :class:`~qlabs_catalog_sync_sdk.conformance.suite.ConnectorConformanceSuite`.
Subclass it, provide one pytest fixture named ``connector`` yielding an already-``setup()``
connector, and pytest runs the full suite. See that class's docstring for a runnable
example — it is what T3.8 (Qlik) and T4.6 (Databricks) are written against.

**HTTP behavior** (retries, ``Retry-After``, ``If-Match``) is not auto-run by the base
suite — what a connector's write path actually calls is unavoidably connector-specific.
:mod:`~qlabs_catalog_sync_sdk.conformance.harness` ships the reusable respx/vcrpy
helpers (:func:`~.harness.capture_requests`, :func:`~.harness.assert_if_match_sent`,
:func:`~.harness.assert_no_http_calls`, :func:`~.harness.retry_then_succeed`,
:func:`~.harness.vcr_config`) a connector author points at their own connector's call in
their own test; see that module's docstring for exactly what respx can and cannot see.

:mod:`~qlabs_catalog_sync_sdk.conformance.samples` is the synthetic-entity/value factory
the suite (and, optionally, a connector author's own tests) builds fixture data from.
"""

from __future__ import annotations

from .harness import (
    assert_if_match_sent,
    assert_no_http_calls,
    capture_requests,
    retry_then_succeed,
    vcr_config,
)
from .samples import ENTITY_CLASSES, entity_field_names, sample_entity, sample_value
from .suite import ConnectorConformanceSuite

__all__ = [
    "ENTITY_CLASSES",
    "ConnectorConformanceSuite",
    "assert_if_match_sent",
    "assert_no_http_calls",
    "capture_requests",
    "entity_field_names",
    "retry_then_succeed",
    "sample_entity",
    "sample_value",
    "vcr_config",
]
