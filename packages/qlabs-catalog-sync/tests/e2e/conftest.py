"""Fixtures for the WP8 Databricks-to-Qlik end-to-end pilot.

Every fixture here is deliberately thin. The pilot's whole point is that the *real*
pieces are wired together by the *real* CLI, so what a fixture may supply is limited to
things a deployment supplies anyway: a state-store location, a config file path, and the
two mocked vendor APIs. It never supplies a connector instance, a ``SyncLoop``, or a
pre-seeded binding.

Tests in this directory are synchronous, not ``async``. That is not a style choice: the
CLI's ``run``/``dry-run`` commands call :func:`asyncio.run` themselves (``cli/
sync_commands.py``), which cannot be called from inside an already-running event loop.
Driving the CLI therefore means letting *it* own the loop, exactly as it does in
production.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import respx
from click.testing import CliRunner

from qlabs_catalog_sync.cli.deps import CliDeps

# pytest runs with --import-mode=importlib, which deliberately does not put a test
# directory on sys.path; e2e_fixtures.py holds this suite's builders, so make it
# importable from the test modules here (the same insert tests/cli/conftest.py does).
sys.path.insert(0, str(Path(__file__).parent))

from e2e_fixtures import (  # noqa: E402
    FakeQlikCloud,
    FakeUnityCatalog,
    PilotTenants,
    pilot_connector_registry,
    register_pilot_routes,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def tenants() -> PilotTenants:
    """A fresh Unity Catalog metastore and an **empty** Qlik space."""
    return PilotTenants(databricks=FakeUnityCatalog(), qlik=FakeQlikCloud())


@pytest.fixture
def router(tenants: PilotTenants) -> Iterator[respx.MockRouter]:
    """One respx router for the whole run, recording every request both connectors make.

    ``assert_all_mocked=True`` makes any unlisted call fail rather than reach the
    network; ``assert_all_called=False`` because several routes exist precisely so that a
    call the pilot forbids (a Qlik ``DELETE``, an ``/actions/activate``) is *recorded*
    rather than raising -- an assertion on a recorded call is a stronger claim than an
    assertion on an exception the engine might have folded into its run report.
    """
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock_router:
        register_pilot_routes(mock_router, tenants)
        yield mock_router


@pytest.fixture
def cli_deps() -> CliDeps:
    """Real entry-point discovery, with only the Databricks vendor client faked.

    See ``e2e_fixtures.PilotDatabricksConnector`` for exactly what is substituted and why
    that one substitution is unavoidable without a live workspace.
    """
    return CliDeps(registry=pilot_connector_registry())


@pytest.fixture
def state_db_url(tmp_path: Path) -> str:
    """A real, migrated-on-first-use SQLite state store, thrown away after the test."""
    return f"sqlite:///{tmp_path / 'pilot-state.db'}"


@pytest.fixture
def review_path(tmp_path: Path) -> Path:
    return tmp_path / "identity-review.json"


@pytest.fixture
def plan_path(tmp_path: Path) -> Path:
    return tmp_path / "dry-run-plan.json"
