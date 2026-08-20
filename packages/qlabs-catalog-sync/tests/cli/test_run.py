"""`run`: applies writes for real, and its exit codes reflect what actually happened."""

from __future__ import annotations

from pathlib import Path

from cli_helpers import write_engine_config
from click.testing import CliRunner

from qlabs_catalog_sync.cli import cli
from qlabs_catalog_sync.cli.deps import CliDeps
from qlabs_catalog_sync.cli.errors import EXIT_ENDPOINT_UNREACHABLE, EXIT_OK
from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_catalog_sync_sdk.models import DataProduct, TextField
from qlabs_catalog_sync_sdk.testing import FakeConnector


def test_run_applies_a_create_to_the_target(
    runner: CliRunner,
    tmp_path: Path,
    state_db_url: str,
    review_path: Path,
    source_connector: FakeConnector,
    target_connector: FakeConnector,
    cli_deps: CliDeps,
) -> None:
    config_path = write_engine_config(tmp_path)
    source_connector.seed(
        DataProduct(name="orders", description=TextField.plain("Orders data product")),
        native_key="sales.orders",
    )

    result = runner.invoke(
        cli,
        [
            "--state-db",
            state_db_url,
            "--review-file",
            str(review_path),
            "run",
            "--config",
            str(config_path),
            "--create-missing",
        ],
        obj=cli_deps,
    )

    assert result.exit_code == EXIT_OK, result.output
    assert target_connector.call_count("create") == 1
    created = target_connector.calls("create")[0]
    assert created.args["entity"].name == "orders"

    # A second run against the same store is idempotent -- the watermark already
    # advanced past the created object, so there is nothing left to re-read or write.
    second = runner.invoke(
        cli,
        [
            "--state-db",
            state_db_url,
            "--review-file",
            str(review_path),
            "run",
            "--config",
            str(config_path),
            "--create-missing",
        ],
        obj=cli_deps,
    )
    assert second.exit_code == EXIT_OK, second.output
    assert target_connector.call_count("create") == 1
    assert "created=0" in second.stdout


def test_run_reports_an_unreachable_endpoint_with_its_own_exit_code(
    runner: CliRunner,
    tmp_path: Path,
    state_db_url: str,
    review_path: Path,
    source_connector: FakeConnector,
    target_connector: FakeConnector,
    cli_deps: CliDeps,
) -> None:
    config_path = write_engine_config(tmp_path)
    source_connector.seed(DataProduct(name="orders"), native_key="sales.orders")
    source_connector.fail_next(
        "healthcheck", AuthError("bad credentials", endpoint=source_connector.name)
    )

    result = runner.invoke(
        cli,
        [
            "--state-db",
            state_db_url,
            "--review-file",
            str(review_path),
            "run",
            "--config",
            str(config_path),
        ],
        obj=cli_deps,
    )

    assert result.exit_code == EXIT_ENDPOINT_UNREACHABLE, result.output
    assert target_connector.call_count("create") == 0
    assert target_connector.call_count("update") == 0
