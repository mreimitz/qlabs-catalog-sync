"""`dry-run`: a genuinely reviewable JSON plan, and zero mutations against the target.

The zero-mutation claim is proved against `FakeConnector`'s real call log -- not by
inspecting the CLI's own `dry_run` flag -- exactly as the task requires: if `dry-run`
ever called `create`/`update`/`delete` on the target, `target_connector.call_count(...)`
would show it regardless of what the CLI *believes* it did.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cli_helpers import write_engine_config
from click.testing import CliRunner

from qlabs_catalog_sync.cli import cli
from qlabs_catalog_sync.cli.deps import CliDeps
from qlabs_catalog_sync.cli.errors import EXIT_INCOMPLETE, EXIT_OK
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    DataProductStatus,
    EntityType,
    IdentityRef,
    TextField,
)
from qlabs_catalog_sync_sdk.testing import FakeConnector


def _seed_one_product(source_connector: FakeConnector) -> None:
    source_connector.seed(
        DataProduct(
            name="orders",
            description=TextField.plain("Orders data product"),
            status=DataProductStatus.ACTIVE,
        ),
        native_key="sales.orders",
    )


def _bind(state_db_url: str, source_ref: IdentityRef, target_ref: IdentityRef) -> None:
    """Pre-bind a confirmed identity, bypassing `identity-confirm` -- what T7.1's
    bootstrap+confirm workflow would have produced, built directly for test speed."""

    async def _do() -> None:
        upgrade_to_head(state_db_url)
        store = StateStore.from_url(state_db_url)
        try:
            neutral_id = uuid.uuid4()
            now = datetime.now(UTC)
            async with store.unit_of_work() as uow:
                await uow.bind_identity(neutral_id, source_ref, confirmed=True, now=now)
                await uow.bind_identity(neutral_id, target_ref, confirmed=True, now=now)
        finally:
            await store.aclose()

    asyncio.run(_do())


def test_dry_run_writes_a_reviewable_json_plan_and_mutates_nothing(
    runner: CliRunner,
    tmp_path: Path,
    state_db_url: str,
    review_path: Path,
    source_connector: FakeConnector,
    target_connector: FakeConnector,
    cli_deps: CliDeps,
) -> None:
    config_path = write_engine_config(tmp_path)
    plan_path = tmp_path / "plan.json"
    _seed_one_product(source_connector)

    args = [
        "--state-db",
        state_db_url,
        "--review-file",
        str(review_path),
        "dry-run",
        "--config",
        str(config_path),
        "--create-missing",
        "--plan-file",
        str(plan_path),
    ]
    result = runner.invoke(cli, args, obj=cli_deps)

    assert result.exit_code == EXIT_OK, result.output

    # The whole safety story: FakeConnector's own call log, not the CLI's belief about
    # itself, proves nothing reached the target.
    assert target_connector.call_count("create") == 0
    assert target_connector.call_count("update") == 0
    assert target_connector.call_count("delete") == 0
    # The source was read (dry-run still computes the full plan)...
    assert source_connector.call_count("read") == 1
    # ...but the state store was never told anything was written either: a second
    # dry-run against the same store still finds the same "would create" plan.
    second = runner.invoke(cli, args, obj=cli_deps)
    assert second.exit_code == EXIT_OK, second.output
    assert target_connector.call_count("create") == 0

    plan = json.loads(plan_path.read_text())
    assert plan["kind"] == "qlabs-catalog-sync/dry-run-plan"
    assert len(plan["runs"]) == 1
    run_report = plan["runs"][0]
    assert run_report["dry_run"] is True
    assert run_report["committed"] is False
    assert run_report["pair"] == "db-to-qlik"
    assert run_report["counts"]["created"] == 1

    records = run_report["records"]
    assert len(records) == 1
    record = records[0]
    assert record["native_key"] == "sales.orders"
    assert record["outcome"] == "created"
    # Field-level diff: which neutral fields this create would carry.
    assert "name" in record["changed_fields"]
    assert "description" in record["changed_fields"]
    # D7: activation is opt-in, so `status` is withheld even though the source set one.
    withheld_fields = {item["field"] for item in record["withheld"]}
    assert "status" in withheld_fields

    # The human-readable output is a different artifact, on stdout, not the JSON file.
    assert "dry-run" in result.stdout
    assert "creates (1)" in result.stdout
    assert "plan written to" in result.stdout
    assert "zero mutations" in result.stdout


def test_dry_run_json_plan_reports_dropped_fields_on_an_update(
    runner: CliRunner,
    tmp_path: Path,
    state_db_url: str,
    review_path: Path,
    source_connector: FakeConnector,
    target_connector: FakeConnector,
    cli_deps: CliDeps,
) -> None:
    """An update must show *why* a field could not be carried across (a ro/na manifest
    field), not just a count -- and must not touch the target either."""
    config_path = write_engine_config(tmp_path)
    plan_path = tmp_path / "plan.json"

    source_connector.seed(
        DataProduct(
            name="orders",
            description=TextField.plain("Orders data product"),
            glossary_term_refs=[uuid.uuid4()],  # qlik_shaped_manifest declares this `na`
        ),
        native_key="sales.orders",
    )
    target_ref = target_connector.seed(DataProduct(name="orders"), native_key="qlik-orders-1")
    source_ref = IdentityRef(
        endpoint=source_connector.name,
        entity_type=EntityType.DATA_PRODUCT,
        native_key="sales.orders",
        tenant_id=source_connector.tenant_id,
    )
    _bind(state_db_url, source_ref, target_ref)

    result = runner.invoke(
        cli,
        [
            "--state-db",
            state_db_url,
            "--review-file",
            str(review_path),
            "dry-run",
            "--config",
            str(config_path),
            "--plan-file",
            str(plan_path),
        ],
        obj=cli_deps,
    )

    assert result.exit_code == EXIT_OK, result.output
    assert target_connector.call_count("update") == 0

    plan = json.loads(plan_path.read_text())
    record = plan["runs"][0]["records"][0]
    assert record["outcome"] == "written"
    dropped_fields = {item["field"] for item in record["dropped"]}
    assert "glossary_term_refs" in dropped_fields


def test_dry_run_without_create_missing_holds_the_watermark_and_is_incomplete(
    runner: CliRunner,
    tmp_path: Path,
    state_db_url: str,
    review_path: Path,
    source_connector: FakeConnector,
    target_connector: FakeConnector,
    cli_deps: CliDeps,
) -> None:
    """No confirmed binding and creation disabled (the default): reported as an
    outstanding skip, which is exit code 1, not a silent success."""
    config_path = write_engine_config(tmp_path)
    plan_path = tmp_path / "plan.json"
    _seed_one_product(source_connector)

    result = runner.invoke(
        cli,
        [
            "--state-db",
            state_db_url,
            "--review-file",
            str(review_path),
            "dry-run",
            "--config",
            str(config_path),
            "--plan-file",
            str(plan_path),
        ],
        obj=cli_deps,
    )

    assert result.exit_code == EXIT_INCOMPLETE, result.output
    assert target_connector.call_count("create") == 0
    plan = json.loads(plan_path.read_text())
    record = plan["runs"][0]["records"][0]
    assert record["outcome"] == "skipped"
    assert record["reason"] == "no_target_binding"
    assert record["holds_watermark"] is True
