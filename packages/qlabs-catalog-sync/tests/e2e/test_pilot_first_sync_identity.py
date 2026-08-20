"""The first-sync identity problem, and why ``--create-missing`` is the only way through.

WP8 / T8.1. ``identity.py`` (T7.1) binds nothing without human confirmation, and
``SyncLoop.create_missing`` is off by default. Together those two defaults mean a
brand-new deployment pointed at an empty Qlik tenant does *nothing* on its first cycle,
which is correct and is also the first thing an operator will hit. This module works the
problem out end to end rather than leaving the pilot's use of ``--create-missing`` as an
unexplained flag.

The shape of it:

* ``identity-confirm bootstrap`` matches source objects against **existing** target
  candidates. An empty tenant has none, so there is nothing to propose -- and the command
  says so and exits 2 rather than writing an empty review file that would look like a
  completed step.
* Without ``--create-missing``, the cycle reads the source, finds no confirmed Qlik
  counterpart, skips, holds the watermark, writes nothing, and exits 1. Nothing is lost:
  the next cycle re-lists the same object.
* So on an empty target the two paths are not alternatives. ``--create-missing`` is the
  only one, and it is safe here for the reason ``sync/loop.py``'s own docstring gives: it
  binds the neutral id the engine minted to the native key the target itself returned,
  "which matches nothing and can therefore claim nothing". The default stays off because
  the *brownfield* case -- a tenant that already holds data products -- is the one where
  blind creation would duplicate every one of them.
"""

from __future__ import annotations

from pathlib import Path

import respx
from click.testing import CliRunner
from e2e_fixtures import (
    PAIR_NAME,
    RETAIL_SCHEMA_ID,
    PilotTenants,
    calls_so_far,
    qlik_mutations,
    write_pilot_config,
)

from qlabs_catalog_sync.cli import cli
from qlabs_catalog_sync.cli.deps import CliDeps
from qlabs_catalog_sync.cli.errors import EXIT_CONFIG_ERROR, EXIT_INCOMPLETE, EXIT_OK


def _root(state_db_url: str, review_path: Path) -> list[str]:
    return ["--state-db", state_db_url, "--review-file", str(review_path)]


def test_bootstrap_has_nothing_to_propose_against_an_empty_qlik_tenant(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
) -> None:
    """The identity workflow refuses cleanly rather than producing an empty review file."""
    config_path = write_pilot_config(tmp_path)

    result = runner.invoke(
        cli,
        [
            *_root(state_db_url, review_path),
            "identity-confirm",
            "bootstrap",
            "--config",
            str(config_path),
            "--pair",
            PAIR_NAME,
        ],
        obj=cli_deps,
    )

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "no existing 'qlik' objects were found" in result.output
    # No review file was written, so nothing looks like a step that was completed.
    assert not review_path.exists()
    # And bootstrap is a read-only operation: it changed nothing in the tenant.
    assert qlik_mutations(router) == []


def test_without_create_missing_the_first_cycle_writes_nothing_and_holds_the_watermark(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
) -> None:
    """The default is a safe refusal, reported as incomplete -- never a silent success."""
    config_path = write_pilot_config(tmp_path)

    result = runner.invoke(
        cli,
        [*_root(state_db_url, review_path), "run", "--config", str(config_path)],
        obj=cli_deps,
    )

    assert result.exit_code == EXIT_INCOMPLETE, result.output
    assert qlik_mutations(router) == []
    assert tenants.qlik.products == {}
    assert "reason=no_target_binding" in result.stdout
    assert "watermark_advanced=False" in result.stdout
    assert RETAIL_SCHEMA_ID in result.stdout


def test_enabling_creation_completes_the_first_sync_and_binds_both_sides(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
) -> None:
    """With creation enabled the first cycle completes, and the binding it wrote is what
    makes the *next* cycle need no flag at all -- the state store carries it forward."""
    config_path = write_pilot_config(tmp_path)
    root = _root(state_db_url, review_path)

    held_back = runner.invoke(cli, [*root, "run", "--config", str(config_path)], obj=cli_deps)
    assert held_back.exit_code == EXIT_INCOMPLETE, held_back.output

    first = runner.invoke(
        cli, [*root, "run", "--config", str(config_path), "--create-missing"], obj=cli_deps
    )
    assert first.exit_code == EXIT_OK, first.output
    assert len(tenants.qlik.products) == 1

    # A source edit now syncs with no `--create-missing` anywhere: the identity map
    # already holds the confirmed binding the create anchored.
    tenants.databricks.set_schema_comment("sales.retail", "Refreshed hourly from now on.")
    before = calls_so_far(router)
    second = runner.invoke(cli, [*root, "run", "--config", str(config_path)], obj=cli_deps)
    assert second.exit_code == EXIT_OK, second.output
    assert [request.method for request in qlik_mutations(router, since=before)] == ["PATCH"]
    assert len(tenants.qlik.products) == 1
