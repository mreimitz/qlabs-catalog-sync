"""Make the shared checksum corpus importable from the tests in this directory.

pytest runs with ``--import-mode=importlib``, which deliberately does not put a test
directory on ``sys.path``; the corpus is a plain helper module rather than a fixture so
that a subprocess (the cross-process determinism test) can load exactly the same data.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
