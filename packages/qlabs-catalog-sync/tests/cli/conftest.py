"""Shared fixtures for the CLI tests.

Two real :class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` instances play the two
v1 endpoint roles (a Databricks-shaped read-only source named ``"databricks"``, a
Qlik-shaped write target named ``"qlik"`` -- ``"qlik"`` is not a free choice: ``config.py``
hard-codes it as the only connector name a pair's target may use). ``helpers.wrap_as_class``
is the seam that lets a CLI invocation build connectors the normal way (``connector_cls()``,
matching a real connector's zero-argument constructor) while every test still gets a
handle on the exact instance the CLI built, to seed data beforehand and assert on its
call log afterward.

``--state-db``/``--review-file`` are root-group options (before the subcommand name);
every test builds its own throwaway SQLite file and review file under ``tmp_path`` so
tests never share state or touch the real default paths.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from qlabs_catalog_sync.cli.deps import CliDeps
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync_sdk.testing import FakeConnector
from qlabs_catalog_sync_sdk.testing.manifests import (
    databricks_shaped_manifest,
    qlik_shaped_manifest,
)

# pytest runs with --import-mode=importlib, which deliberately does not put a test
# directory on sys.path -- cli_helpers.py holds the shared builders, so make it importable
# from the test modules here (mirrors tests/sync/conftest.py's own sys.path insert).
sys.path.insert(0, str(Path(__file__).parent))

from cli_helpers import SOURCE_ENDPOINT, TARGET_ENDPOINT, wrap_as_class  # noqa: E402


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def source_connector() -> FakeConnector:
    """A Databricks-shaped, read-only source connector named ``"databricks"``."""
    return FakeConnector.read_only_source(
        name=SOURCE_ENDPOINT, manifest=databricks_shaped_manifest()
    )


@pytest.fixture
def target_connector() -> FakeConnector:
    """A Qlik-shaped write target named ``"qlik"`` -- the sole v1 write connector name."""
    return FakeConnector.write_target(name=TARGET_ENDPOINT, manifest=qlik_shaped_manifest())


@pytest.fixture
def registry(source_connector: FakeConnector, target_connector: FakeConnector) -> ConnectorRegistry:
    return ConnectorRegistry(
        {
            SOURCE_ENDPOINT: wrap_as_class(source_connector),
            TARGET_ENDPOINT: wrap_as_class(target_connector),
        },
        {},
    )


@pytest.fixture
def cli_deps(registry: ConnectorRegistry) -> CliDeps:
    """Injected into a `CliRunner.invoke(cli, [...], obj=cli_deps)` call so the CLI
    builds the two `FakeConnector`s above instead of doing real entry-point discovery."""
    return CliDeps(registry=registry)


@pytest.fixture
def state_db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'state.db'}"


@pytest.fixture
def review_path(tmp_path: Path) -> Path:
    return tmp_path / "identity-review.json"
