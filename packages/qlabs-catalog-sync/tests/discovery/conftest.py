"""Make the helper modules beside this file importable from the test modules in it."""

from __future__ import annotations

import sys
from pathlib import Path

# pytest runs with ``--import-mode=importlib``, which deliberately does not put a test
# directory on ``sys.path``. ``fixtures.py``, ``broken_fixture.py`` and
# ``discovery_entry_points.py`` beside this file hold real objects the tests point
# synthetic ``importlib.metadata.EntryPoint``s at, so make this directory importable from
# the test modules in it (same trick as ``tests/identity/conftest.py`` — note the helper
# module here is deliberately *not* named ``helpers.py`` like that one: both
# directories' conftest.py insert themselves onto the same process-wide ``sys.path``,
# and two same-named top-level modules would silently shadow each other).
sys.path.insert(0, str(Path(__file__).parent))
