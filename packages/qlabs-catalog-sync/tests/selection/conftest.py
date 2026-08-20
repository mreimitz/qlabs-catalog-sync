"""Makes ``selection_helpers`` importable from the test modules in this directory.

pytest runs with ``--import-mode=importlib``, which deliberately does not put a test
directory on ``sys.path`` -- the same shim ``tests/configstore/conftest.py`` and
``tests/scheduler/conftest.py`` use.

There are no fixtures here, and there is nothing to tear down: T11.1's evaluator is a pure
function, so its tests need no database, no event loop, no clock and no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
