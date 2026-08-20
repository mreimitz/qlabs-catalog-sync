"""Shared fixtures for the scheduler tests.

The scheduler under test is real: a genuine ``apscheduler`` ``AsyncIOScheduler``, driven
through its real, public job-processing machinery (``wakeup()``, ``get_job()``, ``Job.
modify()``, its event bus) via the helpers in ``scheduler_helpers.py`` beside this file. Only
the *pair runner* it fires is a lightweight scripted double
(:class:`~scheduler_helpers.ScriptedRunner`) -- see that module's docstring for why.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from qlabs_catalog_sync.observability import HealthRegistry


@pytest.fixture
def health() -> HealthRegistry:
    return HealthRegistry()


# pytest runs with ``--import-mode=importlib``, which deliberately does not put a test
# directory on ``sys.path``. ``scheduler_helpers.py`` beside this file holds the shared
# builders, so make it importable from the test modules here.
sys.path.insert(0, str(Path(__file__).parent))
