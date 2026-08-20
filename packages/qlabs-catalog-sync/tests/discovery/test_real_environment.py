"""A real end-to-end check against this workspace's actual installed environment.

Every other test in this directory injects a synthetic ``entry_points`` iterable so
individual behaviors (collisions, broken loads, ...) are reproducible independent of
what happens to be installed. This file instead calls ``discover_connectors()`` with no
argument at all, exercising the exact call the engine's own startup makes, against the
real ``qlabs_catalog_sync.connectors`` entry-point group this ``uv sync --all-packages``
workspace populates.

Assertions here deliberately do not hardcode which connectors are fully implemented yet
(WP3-WP6 land on their own schedule, some behind RM-05 and blocked until v0.1): a
connector package can be *known to discovery* — registered, or recorded as broken with a
reason — well before its ``Connector`` subclass is finished. What must always hold is
that installing a connector package makes discovery aware of it one way or the other,
never silently invisible.
"""

from __future__ import annotations

from qlabs_catalog_sync.discovery import discover_connectors
from qlabs_catalog_sync_sdk.contract import Connector

#: The four connector distributions this monorepo's workspace always installs together
#: (packages/qlabs-connector-*), regardless of which work package has finished their
#: Connector implementation.
_INSTALLED_CONNECTOR_NAMES = frozenset({"qlik", "databricks", "collibra", "snowflake"})


def test_every_installed_connector_distribution_is_discovered_one_way_or_another() -> None:
    registry = discover_connectors()

    accounted_for = set(registry.names()) | set(registry.broken())
    missing = _INSTALLED_CONNECTOR_NAMES - accounted_for
    assert not missing, (
        f"installed connector(s) {sorted(missing)} were not seen by discovery at all "
        f"(registered={registry.names()!r}, broken={tuple(registry.broken())!r})"
    )


def test_registered_connectors_are_real_gated_connector_classes() -> None:
    registry = discover_connectors()

    for name in registry.names():
        connector_cls = registry.get_connector(name)
        assert issubclass(connector_cls, Connector)
        assert connector_cls.name == name
