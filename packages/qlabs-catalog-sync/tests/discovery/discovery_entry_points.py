"""Shared helper for building real ``importlib.metadata.EntryPoint`` objects, complete
with a real ``.dist``, for the discovery tests to inject.

Building an honest test double here means using the real classes: a real ``EntryPoint``
(never a mock that just records what was asserted on it), attached to a real, already
-installed ``Distribution`` looked up by name — this workspace always has
``qlabs-catalog-sync`` and the four connector packages installed, so borrowing one of
their real distribution records to stand in for "the distribution that owns this
synthetic entry point" is both realistic and requires no fake metadata files on disk.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint, distribution

from qlabs_catalog_sync_sdk.version import CONNECTOR_ENTRY_POINT_GROUP


def entry_point(
    name: str, value: str, *, distribution_name: str = "qlabs-catalog-sync"
) -> EntryPoint:
    """A real ``EntryPoint`` in the connector group, backed by a real ``Distribution``.

    Two calls with the same ``name`` but different ``distribution_name`` is exactly how
    the collision tests simulate two installed packages claiming the same connector name.
    """
    ep = EntryPoint(name=name, value=value, group=CONNECTOR_ENTRY_POINT_GROUP)
    # ``_for`` is the same private hook importlib.metadata's own entry_points() uses
    # internally to attach `.dist` to a discovered EntryPoint.
    return ep._for(distribution(distribution_name))
