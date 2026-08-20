"""Shared fixtures for :mod:`qlabs_catalog_sync_sdk.testing`'s own test suite.

Every test in this package exercises the real :class:`FakeConnector`, never a further
mock around it — the whole point of this suite is that the shipped test double behaves
correctly, so nothing here is patched out.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.config import ManualClock
from qlabs_catalog_sync_sdk.testing import FakeConnector


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def source(clock: ManualClock) -> FakeConnector:
    """A Databricks-shaped read-only source, seeded with nothing yet."""
    return FakeConnector.read_only_source(clock=clock)


@pytest.fixture
def target(clock: ManualClock) -> FakeConnector:
    """A Qlik-shaped write target, empty."""
    return FakeConnector.write_target(clock=clock)
