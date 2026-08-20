"""Object builders shared by the CLI tests.

A plain module rather than fixtures, for the pieces a test wants to build inline with
its own parameters (a config file with a specific pair name, say). ``conftest.py`` puts
this directory on ``sys.path`` (pytest runs with ``--import-mode=importlib``, which
deliberately does not) -- see ``tests/sync/conftest.py`` for the same pattern.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.testing import FakeConnector

SOURCE_ENDPOINT = "databricks"
TARGET_ENDPOINT = "qlik"


def wrap_as_class(instance: FakeConnector) -> type[Connector]:
    """Wrap an already-built connector instance as a zero-argument-constructible class.

    The CLI's wiring (``cli/wiring.py``) instantiates a connector the same way for every
    connector, real or fake: ``connector_cls()``, with no arguments -- exactly
    ``Connector.__init__(self) -> None``'s signature. ``FakeConnector`` itself needs a
    ``manifest=`` keyword argument, so a test builds the instance once (seeds it, keeps
    a handle to assert on its call log later) and this function returns a subclass whose
    ``__new__`` hands back that *same* instance untouched -- Python only calls
    ``__init__`` on the object ``__new__`` returns when that object is an instance of the
    class being constructed, and a base-class instance is not an instance of a subclass,
    so ``__init__`` never re-runs and never resets the seeded state.
    """
    base = type(instance)

    class _Wrapped(base):  # type: ignore[misc, valid-type]
        def __new__(cls) -> Connector:
            return instance

    return _Wrapped


def write_engine_config(
    tmp_path: Path,
    *,
    pair_name: str = "db-to-qlik",
    catalog_schema_patterns: tuple[str, ...] = ("sales.*",),
    target_space: str = "Sales Space",
    entity_types: tuple[str, ...] = ("data_product",),
    source_endpoint_key: str = "dbx",
    target_endpoint_key: str = "qlik_tenant",
    source_connector_name: str = SOURCE_ENDPOINT,
    target_connector_name: str = TARGET_ENDPOINT,
) -> Path:
    """Write a minimal, valid engine config file and return its path."""
    config: dict[str, Any] = {
        "endpoints": {
            source_endpoint_key: {"connector": source_connector_name},
            target_endpoint_key: {"connector": target_connector_name},
        },
        "pairs": [
            {
                "name": pair_name,
                "source": source_endpoint_key,
                "target": target_endpoint_key,
                "catalog_schema_patterns": list(catalog_schema_patterns),
                "target_space": target_space,
                "entity_types": list(entity_types),
            }
        ],
    }
    path = tmp_path / "engine.json"
    path.write_text(json.dumps(config))
    return path
