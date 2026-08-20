"""Importing (or reloading) this module must never register anything on the global registry.

A module that creates ``Counter``/``Histogram``/``Gauge`` objects bound to
``prometheus_client``'s default registry at import time is untestable (every test process can
construct it exactly once) and breaks on reload. ``observability.py`` only ever creates metric
objects inside ``PrometheusMetrics.__init__``, against whatever registry is passed to (or
defaulted, fresh, by) that constructor — never at module scope.
"""

from __future__ import annotations

import importlib

from prometheus_client import REGISTRY

import qlabs_catalog_sync.observability as observability_module


def test_reloading_the_module_does_not_touch_the_global_registry() -> None:
    collectors_before = set(REGISTRY._collector_to_names)  # noqa: SLF001 - introspecting for the test

    importlib.reload(observability_module)

    collectors_after = set(REGISTRY._collector_to_names)  # noqa: SLF001
    assert collectors_before == collectors_after


def test_constructing_metrics_repeatedly_does_not_touch_the_global_registry() -> None:
    collectors_before = set(REGISTRY._collector_to_names)  # noqa: SLF001

    observability_module.PrometheusMetrics()
    observability_module.PrometheusMetrics()

    collectors_after = set(REGISTRY._collector_to_names)  # noqa: SLF001
    assert collectors_before == collectors_after
