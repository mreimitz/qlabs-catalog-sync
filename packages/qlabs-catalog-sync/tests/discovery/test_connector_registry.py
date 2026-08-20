"""ConnectorRegistry: the lookup surface the rest of the engine (T2.4, T2.8) builds on.

Constructed directly here (rather than via discover_connectors) to test the registry's
own contract in isolation from entry-point enumeration.
"""

from __future__ import annotations

import pytest
from fixtures import AnotherGoodConnector, GoodConnector

from qlabs_catalog_sync.discovery import (
    BrokenConnector,
    ConnectorBrokenError,
    ConnectorNotRegisteredError,
    ConnectorRegistry,
)


def _registry() -> ConnectorRegistry:
    return ConnectorRegistry(
        connectors={"good": GoodConnector, "another_good": AnotherGoodConnector},
        broken={
            "broken": BrokenConnector(
                name="broken", distribution="dist-x", stage="load", reason="boom"
            )
        },
    )


def test_get_connector_returns_the_registered_class() -> None:
    registry = _registry()
    assert registry.get_connector("good") is GoodConnector
    assert registry.get_connector("another_good") is AnotherGoodConnector


def test_get_connector_on_a_broken_name_raises_connector_broken_error() -> None:
    registry = _registry()
    with pytest.raises(ConnectorBrokenError) as excinfo:
        registry.get_connector("broken")
    assert excinfo.value.broken.reason == "boom"
    assert "boom" in str(excinfo.value)
    assert "dist-x" in str(excinfo.value)


def test_get_connector_on_an_unknown_name_raises_not_registered_error() -> None:
    registry = _registry()
    with pytest.raises(ConnectorNotRegisteredError) as excinfo:
        registry.get_connector("unknown")
    assert excinfo.value.name == "unknown"
    assert set(excinfo.value.available) == {"good", "another_good"}


def test_not_registered_error_on_a_fully_empty_registry_still_reads_clearly() -> None:
    registry = ConnectorRegistry(connectors={}, broken={})
    with pytest.raises(ConnectorNotRegisteredError, match="no connectors"):
        registry.get_connector("anything")


def test_contains_len_and_iter_reflect_registered_names_only() -> None:
    registry = _registry()

    assert "good" in registry
    assert "broken" not in registry  # broken entries are not "in" the registry
    assert "unknown" not in registry
    assert len(registry) == 2
    assert set(iter(registry)) == {"good", "another_good"}


def test_names_and_broken_are_queryable_independently() -> None:
    registry = _registry()

    assert set(registry.names()) == {"good", "another_good"}
    assert set(registry.broken()) == {"broken"}


def test_registry_is_defensively_copied_from_its_constructor_arguments() -> None:
    connectors = {"good": GoodConnector}
    broken: dict[str, BrokenConnector] = {}
    registry = ConnectorRegistry(connectors=connectors, broken=broken)

    connectors["another_good"] = AnotherGoodConnector
    broken["broken"] = BrokenConnector(name="broken", distribution="d", stage="load", reason="r")

    assert registry.names() == ("good",)
    assert registry.broken() == {}


def test_broken_mapping_returned_is_a_copy_not_a_live_view() -> None:
    registry = _registry()
    snapshot = registry.broken()
    snapshot["injected"] = BrokenConnector(
        name="injected", distribution="d", stage="load", reason="r"
    )

    assert "injected" not in registry.broken()
