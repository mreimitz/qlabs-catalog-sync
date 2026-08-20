"""``PrometheusMetrics``: each metric increments/observes as claimed, and the registry
is genuinely injectable — never the ``prometheus_client`` global default.
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY, CollectorRegistry

from qlabs_catalog_sync.observability import (
    METRIC_CYCLE_DURATION_SECONDS,
    METRIC_ERRORS_TOTAL,
    METRIC_RATE_LIMITED_TOTAL,
    METRIC_READS_TOTAL,
    METRIC_SKIPS_TOTAL,
    METRIC_WRITES_APPLIED_TOTAL,
    METRIC_WRITES_PLANNED_TOTAL,
    PrometheusMetrics,
)
from qlabs_catalog_sync_sdk.config import MetricsHandle


@pytest.fixture
def metrics() -> PrometheusMetrics:
    return PrometheusMetrics(registry=CollectorRegistry())


def test_satisfies_the_sdk_metrics_handle_protocol(metrics: PrometheusMetrics) -> None:
    # Asserted, not assumed: MetricsHandle is @runtime_checkable specifically so this line is
    # meaningful — it fails loudly the moment increment/observe/set drift out of shape.
    assert isinstance(metrics, MetricsHandle)


def test_default_registry_is_injected_fresh_not_the_global_default() -> None:
    metrics = PrometheusMetrics()
    assert metrics.registry is not REGISTRY
    assert isinstance(metrics.registry, CollectorRegistry)


def test_reads_counter_increments_with_its_labels(metrics: PrometheusMetrics) -> None:
    labels = {"pair": "db_to_qlik", "endpoint": "databricks_prod", "entity_type": "dataset"}
    metrics.increment(METRIC_READS_TOTAL, **labels)
    metrics.increment(METRIC_READS_TOTAL, **labels)
    metrics.increment(METRIC_READS_TOTAL, value=3, **labels)

    value = metrics.registry.get_sample_value(
        METRIC_READS_TOTAL,
        {"pair": "db_to_qlik", "endpoint": "databricks_prod", "entity_type": "dataset"},
    )
    assert value == 5.0


@pytest.mark.parametrize(
    ("metric_name", "labels"),
    [
        (METRIC_WRITES_PLANNED_TOTAL, {"pair": "db_to_qlik", "entity_type": "data_product"}),
        (METRIC_WRITES_APPLIED_TOTAL, {"pair": "db_to_qlik", "entity_type": "data_product"}),
        (METRIC_SKIPS_TOTAL, {"pair": "db_to_qlik", "entity_type": "data_product"}),
        (METRIC_RATE_LIMITED_TOTAL, {"endpoint": "qlik_acme"}),
        (METRIC_ERRORS_TOTAL, {"pair": "db_to_qlik", "endpoint": "qlik_acme"}),
    ],
)
def test_each_named_counter_increments(
    metrics: PrometheusMetrics, metric_name: str, labels: dict[str, str]
) -> None:
    metrics.increment(metric_name, **labels)
    metrics.increment(metric_name, **labels)

    value = metrics.registry.get_sample_value(metric_name, labels)
    assert value == 2.0


def test_cycle_duration_histogram_observes(metrics: PrometheusMetrics) -> None:
    labels = {"pair": "db_to_qlik", "entity_type": "dataset"}
    metrics.observe(METRIC_CYCLE_DURATION_SECONDS, 4.2, **labels)
    metrics.observe(METRIC_CYCLE_DURATION_SECONDS, 61.0, **labels)

    count = metrics.registry.get_sample_value(f"{METRIC_CYCLE_DURATION_SECONDS}_count", labels)
    total = metrics.registry.get_sample_value(f"{METRIC_CYCLE_DURATION_SECONDS}_sum", labels)
    assert count == 2.0
    assert total == pytest.approx(65.2)

    # One observation landed in the <=5s bucket, the other spilled into <=120s.
    le_5 = metrics.registry.get_sample_value(
        f"{METRIC_CYCLE_DURATION_SECONDS}_bucket", {**labels, "le": "5.0"}
    )
    le_120 = metrics.registry.get_sample_value(
        f"{METRIC_CYCLE_DURATION_SECONDS}_bucket", {**labels, "le": "120.0"}
    )
    assert le_5 == 1.0
    assert le_120 == 2.0


def test_unknown_counter_name_is_rejected(metrics: PrometheusMetrics) -> None:
    with pytest.raises(KeyError):
        metrics.increment("not_a_registered_metric", pair="db_to_qlik")


def test_unknown_histogram_name_is_rejected(metrics: PrometheusMetrics) -> None:
    with pytest.raises(KeyError):
        metrics.observe("not_a_registered_metric", 1.0)


def test_set_lazily_registers_and_updates_a_gauge(metrics: PrometheusMetrics) -> None:
    metrics.set("qlabs_sync_identity_map_backlog", 7, pair="db_to_qlik")
    metrics.set("qlabs_sync_identity_map_backlog", 3, pair="db_to_qlik")

    value = metrics.registry.get_sample_value(
        "qlabs_sync_identity_map_backlog", {"pair": "db_to_qlik"}
    )
    assert value == 3.0


def test_set_supports_a_label_less_gauge(metrics: PrometheusMetrics) -> None:
    metrics.set("qlabs_sync_up", 1)
    assert metrics.registry.get_sample_value("qlabs_sync_up") == 1.0


def test_two_instances_do_not_share_state(metrics: PrometheusMetrics) -> None:
    other = PrometheusMetrics(registry=CollectorRegistry())
    metrics.increment(METRIC_SKIPS_TOTAL, pair="p", entity_type="dataset")

    assert other.registry.get_sample_value(
        METRIC_SKIPS_TOTAL, {"pair": "p", "entity_type": "dataset"}
    ) is None
