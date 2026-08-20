"""Test-package setup for the field-diff tests.

The tests are collected in importlib mode with no package ``__init__.py``, so
``diff_helpers.py`` beside this file is only importable once its directory is on
``sys.path`` — the same arrangement the identity tests use. The module is named
``diff_helpers`` rather than ``helpers`` because that flat namespace is shared across
every test directory in the workspace, and a second ``helpers`` would shadow the first.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
