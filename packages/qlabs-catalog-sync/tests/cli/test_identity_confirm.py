"""`identity-confirm`: bootstrap, list, confirm (never blindly), reject, apply."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from cli_helpers import write_engine_config
from click.testing import CliRunner

from qlabs_catalog_sync.cli import cli
from qlabs_catalog_sync.cli.deps import CliDeps
from qlabs_catalog_sync.cli.errors import EXIT_INCOMPLETE, EXIT_OK
from qlabs_catalog_sync.identity import CatalogObject, IdentityResolver, NaturalKey, ParentPathRule
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType, IdentityRef
from qlabs_catalog_sync_sdk.testing import FakeConnector


def _seed_ambiguous_proposal(state_db_url: str, review_path: Path) -> str:
    """Two Qlik candidates share one Databricks source's natural key -- a genuine
    ambiguity, built directly through the library `identity-confirm` wraps, exactly the
    shape a real `identity-confirm bootstrap` run would have produced."""

    async def _do() -> str:
        upgrade_to_head(state_db_url)
        store = StateStore.from_url(state_db_url)
        try:
            resolver = IdentityResolver(store, review_path=review_path)
            source = CatalogObject(
                identity=IdentityRef(
                    endpoint="databricks",
                    entity_type=EntityType.DATA_PRODUCT,
                    native_key="sales.orders",
                    tenant_id="acme",
                ),
                natural_key=NaturalKey(name="orders", entity_type=EntityType.DATA_PRODUCT),
            )
            candidate_a = CatalogObject(
                identity=IdentityRef(
                    endpoint="qlik",
                    entity_type=EntityType.DATA_PRODUCT,
                    native_key="qlik-orders-a",
                    tenant_id="acme",
                ),
                natural_key=NaturalKey(name="orders", entity_type=EntityType.DATA_PRODUCT),
            )
            candidate_b = CatalogObject(
                identity=IdentityRef(
                    endpoint="qlik",
                    entity_type=EntityType.DATA_PRODUCT,
                    native_key="qlik-orders-b",
                    tenant_id="acme",
                ),
                natural_key=NaturalKey(name="orders", entity_type=EntityType.DATA_PRODUCT),
            )
            await resolver.bootstrap(
                source_objects=[source],
                target_candidates=[candidate_a, candidate_b],
                target_endpoint="qlik",
                target_tenant_id="acme",
                parent_path_rule=ParentPathRule.IGNORE,
            )
            return source.key
        finally:
            await store.aclose()

    return asyncio.run(_do())


def test_list_shows_the_proposal_and_confirm_refuses_to_guess_an_ambiguous_match(
    runner: CliRunner, state_db_url: str, review_path: Path
) -> None:
    proposal_id = _seed_ambiguous_proposal(state_db_url, review_path)

    listed = runner.invoke(
        cli,
        ["--state-db", state_db_url, "--review-file", str(review_path), "identity-confirm", "list"],
    )
    assert listed.exit_code == EXIT_OK, listed.output
    assert proposal_id in listed.stdout
    assert "ambiguous" in listed.stdout

    # No --candidate: this command must not pick one for the human.
    blind = runner.invoke(
        cli,
        [
            "--state-db",
            state_db_url,
            "--review-file",
            str(review_path),
            "identity-confirm",
            "confirm",
            proposal_id,
        ],
    )
    assert blind.exit_code == EXIT_INCOMPLETE, blind.output
    assert "ambiguous" in blind.output.lower() or "candidate" in blind.output.lower()

    # Nothing was bound by the blind attempt: a fresh listing still shows it pending.
    still_pending = runner.invoke(
        cli,
        ["--state-db", state_db_url, "--review-file", str(review_path), "identity-confirm", "list"],
    )
    assert proposal_id in still_pending.stdout

    # With an explicit --candidate, confirm succeeds.
    confirmed = runner.invoke(
        cli,
        [
            "--state-db",
            state_db_url,
            "--review-file",
            str(review_path),
            "identity-confirm",
            "confirm",
            proposal_id,
            "--candidate",
            "qlik-orders-a",
        ],
    )
    assert confirmed.exit_code == EXIT_OK, confirmed.output
    assert "bound" in confirmed.stdout.lower()

    # Now bound: `list --pending` (the default) no longer shows it.
    after = runner.invoke(
        cli,
        ["--state-db", state_db_url, "--review-file", str(review_path), "identity-confirm", "list"],
    )
    assert proposal_id not in after.stdout


def test_reject_records_a_refusal_without_binding_anything(
    runner: CliRunner, state_db_url: str, review_path: Path
) -> None:
    proposal_id = _seed_ambiguous_proposal(state_db_url, review_path)

    result = runner.invoke(
        cli,
        [
            "--state-db",
            state_db_url,
            "--review-file",
            str(review_path),
            "identity-confirm",
            "reject",
            proposal_id,
            "--reason",
            "wrong match",
        ],
    )
    assert result.exit_code == EXIT_OK, result.output
    assert "rejected" in result.stdout.lower()


def test_apply_binds_every_confirm_decision_in_the_review_file(
    runner: CliRunner, state_db_url: str, review_path: Path
) -> None:
    proposal_id = _seed_ambiguous_proposal(state_db_url, review_path)
    document = json.loads(review_path.read_text())
    for proposal in document["proposals"]:
        if proposal["proposal_id"] == proposal_id:
            proposal["decision"] = "confirm"
            proposal["chosen_native_key"] = "qlik-orders-b"
    review_path.write_text(json.dumps(document))

    result = runner.invoke(
        cli,
        [
            "--state-db",
            state_db_url,
            "--review-file",
            str(review_path),
            "identity-confirm",
            "apply",
        ],
    )
    assert result.exit_code == EXIT_OK, result.output
    assert "bound" in result.stdout.lower()


def test_bootstrap_proposes_a_match_from_live_connectors_and_binds_nothing(
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
    target_connector.seed(DataProduct(name="orders"), native_key="qlik-orders-1")

    result = runner.invoke(
        cli,
        [
            "--state-db",
            state_db_url,
            "--review-file",
            str(review_path),
            "identity-confirm",
            "bootstrap",
            "--config",
            str(config_path),
            "--pair",
            "db-to-qlik",
        ],
        obj=cli_deps,
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "proposed=1" in result.stdout
    assert target_connector.call_count("create") == 0
    assert target_connector.call_count("update") == 0

    proposals = runner.invoke(
        cli,
        ["--state-db", state_db_url, "--review-file", str(review_path), "identity-confirm", "list"],
    )
    assert "qlik-orders-1" in proposals.stdout
