"""Test-collection plumbing for the T7.4 ``sync.status`` tests.

Nothing behavioral lives here -- every fixture-shaped thing these tests need is a plain
function in ``status_helpers.py``, not a pytest fixture, because none of it needs
teardown or scoping (unlike ``tests/sync``/``tests/policy``, which build a real
:class:`~qlabs_catalog_sync.state.store.StateStore`; this package's tests never touch the
state store at all).
"""

from __future__ import annotations

import sys
from pathlib import Path

# pytest runs with ``--import-mode=importlib``, which deliberately does not put a test
# directory on ``sys.path``. ``status_helpers.py`` beside this file holds the shared
# builders, so make it importable from the test modules here -- matches the pattern
# ``tests/sync/conftest.py`` and ``tests/policy/conftest.py`` already use.
sys.path.insert(0, str(Path(__file__).parent))
