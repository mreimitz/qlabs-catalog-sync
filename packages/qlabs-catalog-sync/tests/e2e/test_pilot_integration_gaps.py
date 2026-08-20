"""Integration mismatches this pilot found, written as the behavior that *should* hold.

WP8 / T8.1. Every test in this module asserts what the decisions and the components'
own docstrings say must happen, and every one is marked ``xfail(strict=True)`` because
the assembled system does not do it today. Nothing here asserts current behavior as if
it were correct, and nothing here is worked around elsewhere in this suite.

``strict=True`` is the point: the moment the underlying fix lands, the test stops being
an expected failure and pytest reports it as an ``XPASS`` failure, which forces the
marker to be removed rather than letting a fixed defect keep a stale "known broken"
label. So this file is a to-do list that cannot rot.

These are all engine-side or contract-side; **this task owns only
``packages/qlabs-catalog-sync/tests/e2e/`` and made no change outside it.** Each test
names the file the fix belongs in.

One finding has no test here, because the MVP cannot exercise it
----------------------------------------------------------------

**GAP 6 (engine + Qlik connector).** The Qlik connector declares three injectable
lookups the orchestrator is meant to set before ``setup()`` --
``dataset_identity_lookup`` (decision D2 tier 1: resolve ``datasetIds`` through the
engine's IdentityMap), ``dataset_name_lookup``, and ``dataset_ref_lookup`` (map Qlik's
native ``datasetIds`` back to neutral ids on read). ``cli/wiring.py::build_connector_pool``
constructs every connector as ``connector_cls()`` and calls ``setup(context)``
immediately, and the Qlik connector consumes those attributes *inside* ``setup()`` (they
are passed to ``write.build_writer``), so there is no point at which the engine could
supply them. Nothing outside ``scripts/tenant_probe.py`` sets any of them.

Consequence today: D2's tier-1 IdentityMap resolution is unreachable in production, and
only tier-2 name matching within the target space can ever run; ``read()`` never reports
``dataset_refs``. Nothing is *lost* in this MVP, because the Databricks read builds a
schema's tables as separate neutral ``Dataset`` entities rather than as ``dataset_refs``
membership on the ``DataProduct``, so there is never a member id to resolve -- which is
also exactly why this pilot cannot turn it into a failing test. It is recorded here
because the seam is dead the moment a source does populate ``dataset_refs``. The fix
belongs in ``packages/qlabs-catalog-sync/src/qlabs_catalog_sync/cli/wiring.py``: give
``build_connector_pool`` a way to hand a connector engine-owned callbacks between
construction and ``setup()``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from click.testing import CliRunner
from e2e_fixtures import (
    HR_SCHEMA_ID,
    RETAIL_SCHEMA_ID,
    SPACE_ID,
    PilotTenants,
    qlik_requests,
    request_bodies,
    write_pilot_config,
)

from qlabs_catalog_sync.cli import cli
from qlabs_catalog_sync.cli.deps import CliDeps
from qlabs_catalog_sync.cli.errors import EXIT_CONFIG_ERROR

_DATA_PRODUCTS_PATH = "/api/data-governance/data-products"


def _root(state_db_url: str, review_path: Path) -> list[str]:
    return ["--state-db", state_db_url, "--review-file", str(review_path)]


def test_the_pair_selector_excludes_a_schema_outside_its_catalog_schema_patterns(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
) -> None:
    """D1: only the schemas the pair selects may become Qlik data products.

    The pair selects ``sales.*``. The Databricks endpoint is left at its documented
    default of ``*.*`` -- which ``DatabricksConfig.catalog_schema_patterns`` describes as
    "everything the service principal can see", with "the per-pair selector
    (SyncPairConfig.catalog_schema_patterns, decision D1) ... applied by the engine on
    top of this and ... the one an operator normally edits".

    Today the engine applies no such thing, so ``hr.people`` -- a schema in an entirely
    different catalog -- is created as a data product in the customer's Qlik space.
    """
    config_path = write_pilot_config(
        tmp_path,
        catalog_schema_patterns=("sales.*",),
        endpoint_catalog_schema_patterns=("*.*",),
    )

    result = runner.invoke(
        cli,
        [
            *_root(state_db_url, review_path),
            "run",
            "--config",
            str(config_path),
            "--create-missing",
        ],
        obj=cli_deps,
    )
    assert result.exit_code == 0, result.output

    created_names = [
        body["name"]
        for body in request_bodies(
            qlik_requests(router, method="POST", path_prefix=_DATA_PRODUCTS_PATH)
        )
    ]
    assert created_names == ["retail"]
    assert sorted(
        stored.body["name"] for stored in tenants.qlik.products.values()
    ) == ["retail"]
    # ...and the out-of-scope schema is reported as what it is.
    assert "filtered=1" in result.stdout


def test_an_out_of_scope_schema_is_reported_as_filtered_not_as_vanished(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
    plan_path: Path,
) -> None:
    """A schema outside the selector was never in scope; it did not disappear.

    The distinction matters operationally: ``deleted_unknown_object`` is the orphan
    channel (decision D4), which is what an operator watches to find out that source
    objects are vanishing from under a live sync. Filling it with every schema the pair
    was never meant to touch makes that signal useless on any multi-catalog metastore.
    """
    config_path = write_pilot_config(
        tmp_path,
        catalog_schema_patterns=("sales.*",),
        endpoint_catalog_schema_patterns=("sales.*",),
    )

    result = runner.invoke(
        cli,
        [
            *_root(state_db_url, review_path),
            "dry-run",
            "--config",
            str(config_path),
            "--create-missing",
            "--plan-file",
            str(plan_path),
        ],
        obj=cli_deps,
    )
    assert result.exit_code == 0, result.output

    records = {
        record["native_key"]: record
        for record in json.loads(plan_path.read_text())["runs"][0]["records"]
    }
    assert records[RETAIL_SCHEMA_ID]["outcome"] == "created"
    assert records[HR_SCHEMA_ID]["outcome"] == "filtered"
    assert records[HR_SCHEMA_ID]["reason"] == "not_selected"


def test_the_dry_run_plan_does_not_promise_a_create_the_apply_will_refuse(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
    plan_path: Path,
) -> None:
    """The plan and the apply must reach the same decision for the same input.

    Configured to sync datasets as well as data products, the apply correctly refuses
    every dataset with ``capability_refused`` (D2 -- see
    ``test_pilot_scope_guardrails.py``). The dry-run over the identical input plans them
    as creates instead.
    """
    config_path = write_pilot_config(tmp_path, entity_types=("data_product", "dataset"))
    root = _root(state_db_url, review_path)

    planned = runner.invoke(
        cli,
        [*root, "dry-run", "--config", str(config_path), "--create-missing", "--plan-file",
         str(plan_path)],
        obj=cli_deps,
    )
    assert planned.exit_code in (0, 1), planned.output

    dataset_run = next(
        run for run in json.loads(plan_path.read_text())["runs"] if run["entity_type"] == "dataset"
    )
    assert dataset_run["counts"]["created"] == 0
    # Some dataset records are now legitimately `filtered` — the pair's catalog.schema
    # selector works (GAP 1), so out-of-scope objects never reach the create decision at
    # all. What this test is about is the rest: the plan must not promise a single create
    # the apply would then refuse.
    outcomes = {record["outcome"] for record in dataset_run["records"]}
    assert "created" not in outcomes
    assert outcomes <= {"skipped", "filtered"}
    assert {
        record["reason"] for record in dataset_run["records"] if record["outcome"] == "skipped"
    } == {"capability_refused"}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP 4 (engine, low severity). On the update path the diff reports a source field "
        "the target's manifest cannot carry as dropped(undeclared) -- this pilot asserts "
        "that for custom_attributes in test_pilot_databricks_to_qlik.py. On the create "
        "path there is no diff, so the same field appears in changed_fields, never lands "
        "in Qlik, and is reported nowhere: not in written_fields, not in dropped, not in "
        "target_skipped_fields. Fix in packages/qlabs-catalog-sync/src/qlabs_catalog_sync/"
        "sync/loop.py::SyncLoop._create_or_skip -- report the source fields the target's "
        "manifest does not declare as dropped, so the create record is as honest as the "
        "update record."
    ),
)
def test_a_field_the_target_cannot_carry_is_reported_on_the_create_path_too(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
    plan_path: Path,
) -> None:
    """A create's run report should account for every field it read but did not write."""
    config_path = write_pilot_config(tmp_path)

    result = runner.invoke(
        cli,
        [
            *_root(state_db_url, review_path),
            "dry-run",
            "--config",
            str(config_path),
            "--create-missing",
            "--plan-file",
            str(plan_path),
        ],
        obj=cli_deps,
    )
    assert result.exit_code == 0, result.output

    record = next(
        item
        for item in json.loads(plan_path.read_text())["runs"][0]["records"]
        if item["native_key"] == RETAIL_SCHEMA_ID
    )
    assert "custom_attributes" in record["changed_fields"]
    accounted_for = (
        set(record["written_fields"])
        | {dropped["field"] for dropped in record["dropped"]}
        | {withheld["field"] for withheld in record["withheld"]}
        | set(record["target_skipped_fields"])
    )
    assert "custom_attributes" in accounted_for


def test_a_pair_whose_target_space_contradicts_the_qlik_endpoint_is_a_config_error(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
) -> None:
    """A contradiction between two config values should fail at load, not per record.

    The assertion is on the outcome an operator needs -- the mistake surfaces as a
    configuration problem before anything is written -- rather than on which of the two
    candidate fixes is taken.
    """
    config_path = write_pilot_config(tmp_path)
    config_path.write_text(
        config_path.read_text().replace(
            f"target_space: {SPACE_ID}", "target_space: a-space-that-is-not-the-endpoints"
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        cli,
        [
            *_root(state_db_url, review_path),
            "run",
            "--config",
            str(config_path),
            "--create-missing",
        ],
        obj=cli_deps,
    )

    assert result.exit_code == EXIT_CONFIG_ERROR, result.output
    assert qlik_requests(router, method="POST", path_prefix=_DATA_PRODUCTS_PATH) == []
