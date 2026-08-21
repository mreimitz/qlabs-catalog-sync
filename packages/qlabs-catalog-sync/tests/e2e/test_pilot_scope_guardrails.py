"""The v1 scope guardrails, checked end to end rather than per component.

WP8 / T8.1. Each of these is already covered by a unit or conformance test somewhere in
the tree -- the Databricks connector refuses ``create()``, the Qlik connector refuses
``delete()`` unless a destructive action is enabled, the loop has no code path that calls
``delete``. What none of those can show is that the *assembled* system never issues the
call: a guardrail that holds in every component individually can still be broken by the
wiring between them. So every assertion here is made against the requests ``respx``
recorded during a real CLI run, not against a refusal a component reported.

The guardrails, from ``CLAUDE.md`` and ``decision-databricks-to-qlik-mvp.md``:

* **Upstream-only.** The source catalog is read and never written.
* **D4 -- v1 never deletes in Qlik.** No ``DELETE`` is ever issued.
* **D7 -- activation is opt-in and off.** No ``/actions/activate`` is ever issued and the
  created product stays ``activated: false``.
* **D2 -- the connector never creates a Qlik dataset.**
"""

from __future__ import annotations

import json
from pathlib import Path

import respx
from click.testing import CliRunner
from e2e_fixtures import (
    PilotTenants,
    calls_so_far,
    databricks_requests,
    databricks_unity_catalog_requests,
    qlik_mutations,
    qlik_requests,
    request_bodies,
    write_pilot_config,
)

from qlabs_catalog_sync.cli import cli
from qlabs_catalog_sync.cli.deps import CliDeps
from qlabs_catalog_sync.cli.errors import EXIT_INCOMPLETE, EXIT_OK

_DATA_PRODUCTS_PATH = "/api/data-governance/data-products"


def _root(state_db_url: str, review_path: Path) -> list[str]:
    return ["--state-db", state_db_url, "--review-file", str(review_path)]


def _exercise_the_whole_workflow(
    runner: CliRunner,
    cli_deps: CliDeps,
    tenants: PilotTenants,
    *,
    root: list[str],
    config_path: Path,
    plan_path: Path,
) -> None:
    """Drive every write path the MVP has: plan, create, update, and a settled no-op.

    A guardrail assertion is only worth as much as the traffic it was made against, so
    the guardrail tests run this rather than a single cycle -- an activation or a delete
    would most plausibly leak from the update or the reconcile path, not the create.
    """
    dry_run = ["dry-run", "--config", str(config_path), "--plan-file", str(plan_path)]
    run = ["run", "--config", str(config_path)]

    assert runner.invoke(cli, [*root, *dry_run, "--create-missing"], obj=cli_deps).exit_code == 0
    assert runner.invoke(cli, [*root, *run, "--create-missing"], obj=cli_deps).exit_code == 0
    tenants.databricks.set_schema_comment("sales.retail", "Now documented differently.")
    assert runner.invoke(cli, [*root, *dry_run], obj=cli_deps).exit_code == 0
    assert runner.invoke(cli, [*root, *run], obj=cli_deps).exit_code == 0
    assert runner.invoke(cli, [*root, *run], obj=cli_deps).exit_code == 0


def test_the_source_catalog_is_only_ever_read(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
    plan_path: Path,
) -> None:
    """Upstream-only: metadata flows Databricks -> Qlik and nothing flows back.

    Method alone does not settle this, because two of the connector's reads are
    ``POST``s: the OAuth token exchange and the Statement Execution API (which is how
    decision D6 reads UC tags). So both are checked for what they actually are -- an
    auth call and a ``SELECT`` -- rather than waved through or wrongly flagged.
    """
    config_path = write_pilot_config(tmp_path)
    _exercise_the_whole_workflow(
        runner,
        cli_deps,
        tenants,
        root=_root(state_db_url, review_path),
        config_path=config_path,
        plan_path=plan_path,
    )

    unity_catalog = databricks_unity_catalog_requests(router)
    assert unity_catalog, "the pilot should have read Unity Catalog at all"
    assert {request.method for request in unity_catalog} == {"GET"}

    non_get = [request for request in databricks_requests(router) if request.method != "GET"]
    assert non_get, "the OAuth token exchange alone should have produced some"
    for request in non_get:
        assert request.method == "POST"
        path = str(request.url.path)
        assert path in ("/oidc/v1/token", "/api/2.0/sql/statements"), path
        if path == "/api/2.0/sql/statements":
            statement = json.loads(request.content)["statement"]
            assert statement.startswith("SELECT "), statement


def test_qlik_is_never_sent_a_delete_or_a_lifecycle_action(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
    plan_path: Path,
) -> None:
    """D4 and D7 together: v1 deletes nothing and activates nothing.

    ``DELETE`` and every ``/actions/...`` endpoint are routed in this pilot's fake tenant
    precisely so that issuing one would be *recorded* rather than raising -- an assertion
    on a recorded call is a stronger claim than an assertion on an exception the engine
    might have folded into its run report.
    """
    config_path = write_pilot_config(tmp_path)
    _exercise_the_whole_workflow(
        runner,
        cli_deps,
        tenants,
        root=_root(state_db_url, review_path),
        config_path=config_path,
        plan_path=plan_path,
    )

    mutations = qlik_mutations(router)
    assert [request.method for request in mutations] == ["POST", "PATCH"]
    assert str(mutations[0].url.path) == _DATA_PRODUCTS_PATH
    assert "/actions/" not in str(mutations[1].url.path)

    assert qlik_requests(router, method="DELETE") == []
    assert [
        request for request in qlik_requests(router) if "/actions/" in str(request.url.path)
    ] == []

    # D7 is not only "no activate call": the create body must not carry activation
    # intent either, and the product must still be inactive afterwards.
    created = request_bodies(qlik_requests(router, method="POST", path_prefix=_DATA_PRODUCTS_PATH))[
        0
    ]
    assert "activated" not in created
    assert "status" not in created
    patched_paths = {
        operation["path"]
        for body in request_bodies(qlik_requests(router, method="PATCH"))
        for operation in body
    }
    assert patched_paths == {"/description"}
    assert tenants.qlik.product_named("retail")["activated"] is False


def test_no_qlik_dataset_is_ever_created(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
) -> None:
    """D2, proved through the assembled system.

    The pair is configured to sync datasets as well as data products -- the reading of D1
    ("its tables and views become datasets") an operator is most likely to try. Unity
    Catalog serves the tables, the engine plans a create for each, and the Qlik connector
    refuses every one of them from its own manifest, before any request. What reaches
    Qlik is the one data product and nothing else.

    The cycle is deliberately reported as incomplete (exit code 1) rather than quietly
    successful: three candidates never reached a terminal state, so the dataset
    watermark is held where it was. That is the loop doing the right thing with a
    configuration that cannot work, not a defect -- but it does mean the MVP's dataset
    stream has no viable configuration today, which the pilot report calls out.
    """
    config_path = write_pilot_config(tmp_path, entity_types=("data_product", "dataset"))

    before = calls_so_far(router)
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
    assert result.exit_code == EXIT_INCOMPLETE, result.output

    creates = qlik_requests(router, method="POST", path_prefix=_DATA_PRODUCTS_PATH, since=before)
    assert len(creates) == 1
    assert json.loads(creates[0].content)["name"] == "retail"
    assert len(tenants.qlik.products) == 1

    assert "reason=capability_refused" in result.stdout
    assert "can never create one" in result.stdout
    # The refusal now fires in the engine, from the target's manifest, before a create is
    # ever planned — that is what stops a dry run promising writes the apply refuses. The
    # engine cannot cite a Qlik-specific decision id in a message about an arbitrary
    # connector, so what is asserted here is the substance: nothing was created, and the
    # report says the target declares every field of that entity read-only. The connector's
    # own gate still cites D2 and still fires if anything ever reaches it; that wording is
    # asserted where it belongs, in the Qlik connector's own tests.
    assert "read-only" in result.stdout


def test_a_data_product_created_here_starts_with_no_members_rather_than_invented_ones(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
) -> None:
    """D2's documented consequence, made visible.

    ``decision-databricks-to-qlik-mvp.md`` says a synced product "may start with an empty
    or partial ``datasetIds`` list when the matching Qlik datasets do not exist yet. That
    is ... the correct behavior -- the alternative is fabricating resources in the
    target." This is that case: an empty target space, so no member resolves, so the key
    is absent and no Items lookup even happens (the neutral product carries no
    ``dataset_refs`` -- the Databricks read builds the schema's datasets as separate
    neutral entities, not as membership on the product).
    """
    config_path = write_pilot_config(tmp_path)

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
    assert result.exit_code == EXIT_OK, result.output

    body = request_bodies(qlik_requests(router, method="POST", path_prefix=_DATA_PRODUCTS_PATH))[0]
    assert "datasetIds" not in body
    assert "apiConsumableDatasetIds" not in body
    assert "glossaryIds" not in body  # D5: never sent, not even empty
    assert tenants.qlik.product_named("retail")["datasetIds"] == []
