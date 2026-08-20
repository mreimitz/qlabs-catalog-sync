"""The WP8 pilot: one Unity Catalog schema becomes one Qlik data product, end to end.

WP8 / T8.1. This is the first time all fourteen independently-built pieces are made to
work together: the SDK's contract and neutral model, the engine's discovery, config,
state store, diff, identity map and sync loop, its CLI, and both connectors -- driven by
the command an operator actually types.

Why the CLI, and not ``SyncLoop`` directly
------------------------------------------

``SyncLoop`` would have been defensible and simpler: fewer moving parts, no Click
harness, direct access to the :class:`~qlabs_catalog_sync.sync.loop.SyncRunReport`. It
was rejected because driving it directly means *this test* does the wiring -- building
connectors, calling ``setup()``, migrating the store, choosing ``create_missing`` -- and
the wiring is precisely what has never been exercised before. A pilot that hand-wires
the system proves the pieces fit the way the test author imagined; driving
``qlabs-catalog-sync run`` proves they fit the way the shipped code assembles them.
Concretely, going through the CLI is what puts real entry-point discovery, real
``EngineConfig`` loading and credential resolution, real Alembic migration, the real
``build_connector_pool``/``build_sync_loop`` path, the real dry-run plan file and the
real exit-code contract inside the blast radius of these tests. Two of the findings this
pilot reports live in exactly that layer and are invisible from ``SyncLoop``.

What "nothing was written" is checked against
---------------------------------------------

Never the engine's ``dry_run`` flag, and never a connector's own call log: every
zero-write claim here is made against :func:`qlik_mutations`, which reads the requests
``respx`` actually recorded on the wire. If the loop believed it was in dry-run and
posted anyway, these assertions would still catch it.

The first-sync identity problem, and how this pilot resolves it
---------------------------------------------------------------

``identity.py`` binds nothing without human confirmation, and ``create_missing`` is off
by default -- so a first cycle against an empty Qlik tenant writes nothing at all. That
default is deliberate (blind creation on a brownfield tenant duplicates every product
already in it), so a first sync has to opt in, and this pilot does, with
``--create-missing``.

The reason that is the *only* honest path here, rather than one of two, is proved in
``test_pilot_first_sync_identity.py`` rather than asserted: ``identity-confirm
bootstrap`` matches source objects against *existing* target candidates, and an empty
tenant has none -- so there is nothing to propose, nothing to confirm, and no binding
any amount of reviewing could produce. ``--create-missing`` is safe here for the reason
the loop's own docstring gives: it binds a neutral id the engine minted to the native
key the target itself returned, which matches nothing and can therefore claim nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from click.testing import CliRunner
from e2e_fixtures import (
    OWNER_EMAIL,
    OWNER_USER_ID,
    PAIR_NAME,
    RETAIL_SCHEMA_ID,
    SPACE_ID,
    PilotTenants,
    calls_so_far,
    qlik_mutations,
    qlik_requests,
    request_bodies,
    write_pilot_config,
)

from qlabs_catalog_sync.cli import cli
from qlabs_catalog_sync.cli.deps import CliDeps
from qlabs_catalog_sync.cli.errors import EXIT_OK

_DATA_PRODUCTS_PATH = "/api/data-governance/data-products"

#: The exact ``POST /api/data-governance/data-products`` body this pilot expects, field
#: for field, against RS-02 ``qlik-two-way-sync-readiness.md`` section 2's documented
#: create schema. Every key is here for a reason and every documented key that is *not*
#: here is absent for a reason:
#:
#: * ``name``          <- the UC schema's ``name`` (D1: the schema is the data product).
#: * ``spaceId``       <- ``QlikConfig.space_id``, never the neutral ``placement``.
#: * ``description``   <- the UC schema's ``comment``.
#: * ``tags``          <- ``INFORMATION_SCHEMA.SCHEMA_TAGS`` (D6), flattened ``key=value``
#:                        because Qlik's ``tags`` is a bare ``string[]``.
#: * ``keyContacts``   <- the UC ``owner`` email resolved to a Qlik ``userId`` (D3).
#: * ``datasetIds``    absent -- the source's tables have no Qlik dataset to resolve to,
#:                        and D2 forbids inventing one.
#: * ``readMe``        absent -- Databricks has no documentation field to map.
#: * ``glossaryIds``   absent -- D5 puts glossary out of the MVP; never sent, not even ``[]``.
#: * anything about activation absent -- D7: activation is opt-in and off.
EXPECTED_CREATE_BODY = {
    "name": "retail",
    "spaceId": SPACE_ID,
    "description": "Curated retail sales tables, refreshed nightly.",
    "tags": ["domain=retail", "pii=false"],
    "keyContacts": [{"userId": OWNER_USER_ID, "role": "owner"}],
}

#: What the source can offer for this schema: the five neutral fields the Databricks
#: connector populated envelopes for.
EXPECTED_SOURCE_FIELDS = ["custom_attributes", "description", "name", "owners", "tags"]


def _root_args(state_db_url: str, review_path: Path) -> list[str]:
    return ["--state-db", state_db_url, "--review-file", str(review_path)]


def _dry_run_args(config_path: Path, plan_path: Path, *, create_missing: bool = True) -> list[str]:
    args = ["dry-run", "--config", str(config_path), "--plan-file", str(plan_path)]
    if create_missing:
        args.append("--create-missing")
    return args


def _run_args(config_path: Path, *, create_missing: bool = True) -> list[str]:
    args = ["run", "--config", str(config_path)]
    if create_missing:
        args.append("--create-missing")
    return args


def test_dry_run_applies_nothing_and_plans_the_create_in_full(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
    plan_path: Path,
) -> None:
    """Claim 1: ``dry-run`` reaches Qlik zero times to change anything, and the JSON plan
    it writes describes the intended create down to the field."""
    config_path = write_pilot_config(tmp_path)

    result = runner.invoke(
        cli,
        [*_root_args(state_db_url, review_path), *_dry_run_args(config_path, plan_path)],
        obj=cli_deps,
    )

    assert result.exit_code == EXIT_OK, result.output

    # Against the wire, not against the engine's belief about itself.
    assert qlik_mutations(router) == []
    assert tenants.qlik.products == {}
    # ...and the target was not even asked to resolve a reference, because nothing was
    # ever going to be written: `create()` is never reached in dry-run mode.
    assert qlik_requests(router, path_prefix="/api/v1/users") == []

    plan = json.loads(plan_path.read_text())
    assert plan["kind"] == "qlabs-catalog-sync/dry-run-plan"
    assert plan["version"] == 1
    assert plan["config_file"] == str(config_path)
    assert len(plan["runs"]) == 1

    run = plan["runs"][0]
    assert run["pair"] == PAIR_NAME
    assert run["source_endpoint"] == "databricks"
    assert run["target_endpoint"] == "qlik"
    assert run["entity_type"] == "data_product"
    assert run["dry_run"] is True
    assert run["committed"] is False
    assert run["create_enabled"] is True
    assert run["watermark"]["advanced"] is False
    assert run["counts"]["created"] == 1
    assert run["counts"]["read"] == 1
    assert run["errors"] == []
    assert run["orphans"] == []

    created = next(
        record for record in run["records"] if record["native_key"] == RETAIL_SCHEMA_ID
    )
    assert created["outcome"] == "created"
    assert created["entity_type"] == "data_product"
    # D1: identity is the stable `schema_id`; the dotted `catalog.schema` is the label.
    assert created["display_name"] == "sales.retail"
    assert created["was_read"] is True
    assert created["holds_watermark"] is False
    # The field-level detail an operator reviews before letting this run for real.
    assert created["changed_fields"] == EXPECTED_SOURCE_FIELDS
    # Nothing has been written yet, so nothing is claimed as written.
    assert created["written_fields"] == []
    assert created["target_native_key"] is None

    assert "plan written to" in result.stdout
    assert "zero mutations" in result.stdout


def test_apply_posts_exactly_the_expected_qlik_data_product_body(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
    plan_path: Path,
) -> None:
    """Claim 2: the same cycle, applied, sends one create with exactly the documented body.

    The dry-run runs first on purpose: the plan an operator approved and the request that
    is then sent have to be the same decision, and running both in one test is what makes
    that comparable rather than assumed.
    """
    config_path = write_pilot_config(tmp_path)
    root = _root_args(state_db_url, review_path)

    planned = runner.invoke(cli, [*root, *_dry_run_args(config_path, plan_path)], obj=cli_deps)
    assert planned.exit_code == EXIT_OK, planned.output

    before_apply = calls_so_far(router)
    applied = runner.invoke(cli, [*root, *_run_args(config_path)], obj=cli_deps)
    assert applied.exit_code == EXIT_OK, applied.output

    creates = qlik_requests(
        router, method="POST", path_prefix=_DATA_PRODUCTS_PATH, since=before_apply
    )
    assert len(creates) == 1
    create = creates[0]
    assert str(create.url.path) == _DATA_PRODUCTS_PATH
    assert create.headers["content-type"].startswith("application/json")
    assert json.loads(create.content) == EXPECTED_CREATE_BODY

    # It is the only mutation the whole apply cycle made.
    assert [request.method for request in qlik_mutations(router, since=before_apply)] == ["POST"]

    # D3: the owner email was resolved through the users API, and only through it -- no
    # Qlik user was created to make the reference resolve.
    owner_lookups = qlik_requests(router, path_prefix="/api/v1/users", since=before_apply)
    assert len(owner_lookups) == 1
    assert owner_lookups[0].url.params["filter"] == f"email eq '{OWNER_EMAIL}'"

    # D2: no dataset was invented. The source's two tables have no Qlik counterpart in
    # this empty space, so `datasetIds` is absent from the body rather than fabricated.
    assert "datasetIds" not in json.loads(create.content)

    # D7: the product Qlik now holds is not activated, and no activation was attempted.
    product = tenants.qlik.product_named("retail")
    assert product["activated"] is False
    assert product["spaceId"] == SPACE_ID
    assert product["keyContacts"] == [{"userId": OWNER_USER_ID, "role": "owner"}]

    # The run report agrees with the wire.
    assert "created=1" in applied.stdout
    assert "committed=True" in applied.stdout
    assert "watermark_advanced=True" in applied.stdout


def test_a_second_cycle_over_unchanged_source_data_writes_nothing(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
) -> None:
    """Claim 3 -- the product's central claim. Re-running against unchanged Unity Catalog
    data reaches Qlik with zero write requests, and reads nothing back either.

    Proved against recorded requests, and paired with the opposite case in
    :func:`test_a_real_source_change_produces_a_minimal_json_patch` so that "nothing was
    written" means "the source did not change", not "the engine is inert".
    """
    config_path = write_pilot_config(tmp_path)
    root = _root_args(state_db_url, review_path)

    first = runner.invoke(cli, [*root, *_run_args(config_path)], obj=cli_deps)
    assert first.exit_code == EXIT_OK, first.output
    assert len(qlik_mutations(router)) == 1

    before_second = calls_so_far(router)
    second = runner.invoke(cli, [*root, *_run_args(config_path)], obj=cli_deps)
    assert second.exit_code == EXIT_OK, second.output

    assert qlik_mutations(router, since=before_second) == []
    # The only Qlik traffic at all is the pre-cycle health check: an OAuth token and one
    # GET on the configured space. Nothing about the data product is even read.
    second_cycle_qlik = [
        (request.method, str(request.url.path))
        for request in qlik_requests(router, since=before_second)
    ]
    assert second_cycle_qlik == [
        ("POST", "/oauth/token"),
        ("GET", f"/api/v1/spaces/{SPACE_ID}"),
    ]

    # The source connector short-circuited before the engine ever saw a candidate: the
    # checksum snapshot in the committed watermark matched what Unity Catalog now holds.
    assert "created=0 written=0" in second.stdout
    assert "(read=0)" in second.stdout
    assert "writes=0, errors=0" in second.stdout
    # Qlik still holds exactly the one product the first cycle created.
    assert len(tenants.qlik.products) == 1


def test_a_real_source_change_produces_a_minimal_json_patch(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
    plan_path: Path,
) -> None:
    """The update half of claim 2, and the control for claim 3.

    One edited ``comment`` in Unity Catalog produces exactly one ``replace`` operation on
    ``/description`` -- not a re-send of every field -- guarded by the ETag the create
    returned, and preceded by the connector's own pre-read.
    """
    config_path = write_pilot_config(tmp_path)
    root = _root_args(state_db_url, review_path)

    first = runner.invoke(cli, [*root, *_run_args(config_path)], obj=cli_deps)
    assert first.exit_code == EXIT_OK, first.output
    created_etag = next(iter(tenants.qlik.products.values())).etag

    tenants.databricks.set_schema_comment(
        "sales.retail", "Curated retail sales tables, refreshed hourly."
    )

    # The plan first: it must name the one field that moved, and nothing else.
    planned = runner.invoke(
        cli,
        [*root, *_dry_run_args(config_path, plan_path, create_missing=False)],
        obj=cli_deps,
    )
    assert planned.exit_code == EXIT_OK, planned.output
    plan_record = json.loads(plan_path.read_text())["runs"][0]["records"][0]
    assert plan_record["outcome"] == "written"
    assert plan_record["changed_fields"] == ["description"]
    # `custom_attributes` has no Qlik counterpart, and the plan says so rather than
    # letting it disappear.
    assert [item["field"] for item in plan_record["dropped"]] == ["custom_attributes"]

    before_apply = calls_so_far(router)
    applied = runner.invoke(
        cli, [*root, *_run_args(config_path, create_missing=False)], obj=cli_deps
    )
    assert applied.exit_code == EXIT_OK, applied.output

    patches = qlik_requests(router, method="PATCH", since=before_apply)
    assert len(patches) == 1
    patch = patches[0]
    assert str(patch.url.path) == f"{_DATA_PRODUCTS_PATH}/{next(iter(tenants.qlik.products))}"
    assert patch.headers["if-match"] == created_etag
    assert json.loads(patch.content) == [
        {
            "op": "replace",
            "path": "/description",
            "value": "Curated retail sales tables, refreshed hourly.",
        }
    ]
    # No create, and exactly one mutation.
    assert [request.method for request in qlik_mutations(router, since=before_apply)] == ["PATCH"]

    # The connector's own idempotency pre-read (one Tier-1 GET) precedes the PATCH.
    reads = qlik_requests(
        router, method="GET", path_prefix=f"{_DATA_PRODUCTS_PATH}/", since=before_apply
    )
    assert len(reads) == 1

    assert tenants.qlik.product_named("retail")["description"] == (
        "Curated retail sales tables, refreshed hourly."
    )
    # Everything the create wrote is still there -- a replace on one path, not a rewrite.
    assert tenants.qlik.product_named("retail")["tags"] == ["domain=retail", "pii=false"]


def test_an_owner_with_no_qlik_user_is_dropped_and_reported_never_invented(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
) -> None:
    """D3, the failing half: an owner email that matches no Qlik user is omitted from the
    payload and reported -- never turned into a fabricated user."""
    tenants.qlik.users_by_email.clear()
    config_path = write_pilot_config(tmp_path)

    result = runner.invoke(
        cli,
        [*_root_args(state_db_url, review_path), *_run_args(config_path)],
        obj=cli_deps,
    )
    assert result.exit_code == EXIT_OK, result.output

    body = request_bodies(qlik_requests(router, method="POST", path_prefix=_DATA_PRODUCTS_PATH))[0]
    assert "keyContacts" not in body
    # Everything else still landed.
    assert body["name"] == "retail"
    assert body["description"] == "Curated retail sales tables, refreshed nightly."

    assert "target could not resolve: ['owners']" in result.stdout
    # No user was created to make the reference resolve: the users API was read only.
    assert [
        request.method for request in qlik_requests(router, path_prefix="/api/v1/users")
    ] == ["GET"]


@pytest.mark.parametrize("cycles", [3])
def test_repeating_the_whole_operator_workflow_converges(
    runner: CliRunner,
    tmp_path: Path,
    tenants: PilotTenants,
    router: respx.MockRouter,
    cli_deps: CliDeps,
    state_db_url: str,
    review_path: Path,
    plan_path: Path,
    cycles: int,
) -> None:
    """dry-run, run, then dry-run/run again and again: exactly one write, ever.

    The scheduler (T2.6) will call this cycle on a cadence, so "one create, then
    silence" has to hold across repetition, not just across one repeat.
    """
    config_path = write_pilot_config(tmp_path)
    root = _root_args(state_db_url, review_path)

    for _ in range(cycles):
        planned = runner.invoke(cli, [*root, *_dry_run_args(config_path, plan_path)], obj=cli_deps)
        assert planned.exit_code == EXIT_OK, planned.output
        applied = runner.invoke(cli, [*root, *_run_args(config_path)], obj=cli_deps)
        assert applied.exit_code == EXIT_OK, applied.output

    mutations = qlik_mutations(router)
    assert [request.method for request in mutations] == ["POST"]
    assert len(tenants.qlik.products) == 1
    # The dry-run after the create plans nothing, because there is nothing to do.
    final_plan = json.loads(plan_path.read_text())["runs"][0]
    assert final_plan["counts"]["created"] == 0
    assert final_plan["counts"]["written"] == 0
