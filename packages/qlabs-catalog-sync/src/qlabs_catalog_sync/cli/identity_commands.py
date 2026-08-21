"""``identity-confirm`` -- the CLI wrapper T7.1 was written for.

WP2 / T2.8. ``identity.py``'s own docstring: "Expose confirm and list operations as a
callable API the CLI wraps," and the RM-01 agent guide lists T7.1 as "bootstrap matching
... with an explicit confirmation step exposed as a CLI subcommand." This module is that
subcommand -- a :class:`click.Group` with five children:

* ``bootstrap`` -- read every object of the pair's entity types at both endpoints,
  propose matches by natural key, and write the review file. Binds nothing.
* ``list`` -- show proposals from the review file.
* ``confirm`` -- bind one proposal after a human has looked at it. **Refuses** to bind
  an ambiguous proposal without an explicit ``--candidate``: there is deliberately no
  "confirm everything" that would quietly pick one, because an ambiguous match is a
  structural fact (two candidates share the natural key equally), not something this
  CLI is entitled to break the tie on.
* ``reject`` -- record that a proposal must not be bound, without binding anything.
  Bootstrap will not propose it again while its candidates are unchanged.
* ``apply`` -- bind every ``confirm`` decision already recorded in the review file (the
  workflow for someone who edited the file by hand rather than running ``confirm`` once
  per id).

Every subcommand needs a state store and the review file path -- both root-group
options (``--state-db``, ``--review-file``), shared with ``run``/``dry-run`` on purpose:
`SyncLoop` itself reads bindings through the same review-file-backed
`IdentityResolver` while it runs a cycle, so a sync run and an `identity-confirm`
session must agree on one file, never default to two different ones. Only ``bootstrap``
additionally needs ``--config`` and a live source/target connector, because it is the
one subcommand that reads a catalog rather than only the stored map and the review file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from qlabs_catalog_sync.identity import (
    ApplyReport,
    BootstrapReport,
    CatalogObject,
    ConfirmationResult,
    MatchProposal,
    ParentPathRule,
    ProposalStatus,
)
from qlabs_catalog_sync.observability import PrometheusMetrics, get_logger
from qlabs_catalog_sync_sdk.exceptions import ConflictError, NotFound
from qlabs_catalog_sync_sdk.models import EntityType

from .config_loading import load_engine_config, resolve_credentials
from .deps import RuntimeContext
from .errors import EXIT_CONFIG_ERROR, EXIT_INCOMPLETE, CliError
from .identity_bootstrap import enumerate_all, to_catalog_objects
from .render import render_bootstrap_text, render_proposals_text
from .wiring import build_connector_pool, build_identity_resolver, build_state_store, select_pairs

__all__ = ["identity_confirm"]

_LOG = get_logger("qlabs.catalog_sync.cli.identity")

_ENTITY_TYPE_CHOICE = click.Choice([entity_type.value for entity_type in EntityType])
_STATUS_CHOICE = click.Choice([status.value for status in ProposalStatus])


@click.group("identity-confirm")
def identity_confirm() -> None:
    """Bootstrap, review and confirm identity bindings. Nothing binds without confirm/apply."""


# --------------------------------------------------------------------------------------
# bootstrap
# --------------------------------------------------------------------------------------


async def _run_bootstrap(
    *,
    runtime: RuntimeContext,
    config_path: Path,
    pair_name: str,
    entity_type_values: tuple[str, ...],
    rule: ParentPathRule,
) -> BootstrapReport:
    engine_config = load_engine_config(config_path)
    pair = select_pairs(engine_config, [pair_name])[0]
    entity_types = [EntityType(value) for value in entity_type_values] or list(pair.entity_types)
    allowed = set(pair.entity_types)
    entity_types = [entity_type for entity_type in entity_types if entity_type in allowed]
    if not entity_types:
        raise CliError(
            f"sync pair {pair.name!r} configures no matching entity types",
            exit_code=EXIT_CONFIG_ERROR,
        )

    credentials = resolve_credentials(engine_config)
    metrics = PrometheusMetrics()
    store = await build_state_store(runtime.state_db)
    resolver = build_identity_resolver(store, runtime.review_path)
    pool = await build_connector_pool(
        config=engine_config,
        credentials=credentials,
        endpoint_keys=[pair.source, pair.target],
        registry=runtime.connector_registry(),
        metrics=metrics,
    )
    try:
        source = pool.get(pair.source)
        target = pool.get(pair.target)
        source_objects: list[CatalogObject] = []
        target_objects: list[CatalogObject] = []
        for entity_type in entity_types:
            source_objects += to_catalog_objects(
                await enumerate_all(source, entity_type), entity_type, source.name
            )
            target_objects += to_catalog_objects(
                await enumerate_all(target, entity_type), entity_type, target.name
            )

        if not target_objects:
            raise CliError(
                f"no existing {target.name!r} objects were found to bootstrap identity "
                "against -- nothing to propose a match to",
                exit_code=EXIT_CONFIG_ERROR,
            )
        tenants = {obj.identity.tenant_id for obj in target_objects}
        if len(tenants) > 1:
            raise CliError(
                f"target objects span multiple tenants ({sorted(tenants)!r}); bootstrap "
                "needs exactly one",
                exit_code=EXIT_CONFIG_ERROR,
            )

        return await resolver.bootstrap(
            source_objects=source_objects,
            target_candidates=target_objects,
            target_endpoint=target.name,
            target_tenant_id=next(iter(tenants)),
            parent_path_rule=rule,
        )
    finally:
        await pool.close()
        await store.aclose()


@identity_confirm.command()
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Engine config file (YAML or JSON) -- endpoints and sync pairs.",
)
@click.option("--pair", "pair_name", required=True, help="The sync pair to bootstrap identity for.")
@click.option(
    "--entity-type",
    "entity_type_values",
    multiple=True,
    type=_ENTITY_TYPE_CHOICE,
    help="Restrict to this entity type (repeatable). Default: every type the pair configures.",
)
@click.option(
    "--parent-path-rule",
    type=click.Choice(["exact", "ignore"]),
    default="ignore",
    show_default=True,
    help=(
        "How the natural key's parent path participates in matching. 'ignore' is the "
        "right default for a source-to-Qlik pair: candidates are scoped to one Qlik "
        "space, and a Qlik native key carries no comparable containment to a Unity "
        "Catalog path (see identity_bootstrap.py)."
    ),
)
@click.pass_obj
def bootstrap(
    runtime: RuntimeContext,
    config_path: Path,
    pair_name: str,
    entity_type_values: tuple[str, ...],
    parent_path_rule: str,
) -> None:
    """Propose identity bindings by natural-key match. Binds nothing.

    Reads every object of the pair's entity types at both endpoints, matches source
    objects with no existing binding against unbound target candidates by name + type +
    parent path, and writes the proposals to the review file. A target object already
    bound to some other neutral id is never offered as a candidate. Nothing in this
    command performs a database write to the identity map -- only `confirm` and `apply` do.
    """
    report = asyncio.run(
        _run_bootstrap(
            runtime=runtime,
            config_path=config_path,
            pair_name=pair_name,
            entity_type_values=entity_type_values,
            rule=ParentPathRule(parent_path_rule),
        )
    )
    click.echo(render_bootstrap_text(report))
    if report.needs_human:
        click.echo(
            f"\n{len(report.needs_human)} proposal(s) need review: identity-confirm list --pending"
        )


# --------------------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------------------


@identity_confirm.command("list")
@click.option(
    "--status", type=_STATUS_CHOICE, default=None, help="Only proposals with this status."
)
@click.option(
    "--pending/--all",
    "pending_only",
    default=True,
    show_default=True,
    help="Only proposals still waiting on a human decision (proposed/ambiguous, undecided).",
)
@click.pass_obj
def list_proposals(runtime: RuntimeContext, status: str | None, pending_only: bool) -> None:
    """List identity bootstrap proposals from the review file. Binds nothing."""

    async def _list() -> tuple[MatchProposal, ...]:
        store = await build_state_store(runtime.state_db)
        try:
            resolver = build_identity_resolver(store, runtime.review_path)
            status_enum = ProposalStatus(status) if status is not None else None
            return await resolver.list_proposals(status=status_enum, needs_human=pending_only)
        finally:
            await store.aclose()

    proposals = asyncio.run(_list())
    click.echo(render_proposals_text(proposals))


# --------------------------------------------------------------------------------------
# confirm
# --------------------------------------------------------------------------------------


@identity_confirm.command()
@click.argument("proposal_id")
@click.option(
    "--candidate",
    "candidate_native_key",
    default=None,
    help="Native key to bind. Required for an ambiguous proposal -- never guessed.",
)
@click.pass_obj
def confirm(runtime: RuntimeContext, proposal_id: str, candidate_native_key: str | None) -> None:
    """Bind one proposal after a human has reviewed it.

    Refuses an ambiguous proposal with no `--candidate`: two or more target objects
    share the natural key equally, so there is no default this command is entitled to
    pick, and it does not.
    """

    async def _confirm() -> ConfirmationResult:
        store = await build_state_store(runtime.state_db)
        try:
            resolver = build_identity_resolver(store, runtime.review_path)
            return await resolver.confirm(proposal_id, candidate_native_key=candidate_native_key)
        finally:
            await store.aclose()

    try:
        result = asyncio.run(_confirm())
    except NotFound as exc:
        raise CliError(exc.message, exit_code=EXIT_CONFIG_ERROR) from exc
    except ConflictError as exc:
        raise CliError(exc.message, exit_code=EXIT_INCOMPLETE) from exc
    click.echo(f"{result.outcome.value}: {result.detail}")


# --------------------------------------------------------------------------------------
# reject
# --------------------------------------------------------------------------------------


@identity_confirm.command()
@click.argument("proposal_id")
@click.option("--reason", default=None, help="Why this proposal is refused, recorded on the entry.")
@click.pass_obj
def reject(runtime: RuntimeContext, proposal_id: str, reason: str | None) -> None:
    """Record that a proposal must not be bound. Binds nothing."""

    async def _reject() -> ConfirmationResult:
        store = await build_state_store(runtime.state_db)
        try:
            resolver = build_identity_resolver(store, runtime.review_path)
            return await resolver.reject(proposal_id, reason=reason)
        finally:
            await store.aclose()

    try:
        result = asyncio.run(_reject())
    except NotFound as exc:
        raise CliError(exc.message, exit_code=EXIT_CONFIG_ERROR) from exc
    except ConflictError as exc:
        raise CliError(exc.message, exit_code=EXIT_INCOMPLETE) from exc
    click.echo(f"{result.outcome.value}: {result.detail}")


# --------------------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------------------


@identity_confirm.command()
@click.pass_obj
def apply(runtime: RuntimeContext) -> None:
    """Bind every `confirm` decision already recorded in the review file.

    For a human who edited the review file directly (set `"decision": "confirm"`,
    and `"chosen_native_key"` for an ambiguous entry) rather than running `confirm`
    once per proposal id. One entry that no longer matches reality is reported as
    failed and does not block the rest.
    """

    async def _apply() -> ApplyReport:
        store = await build_state_store(runtime.state_db)
        try:
            resolver = build_identity_resolver(store, runtime.review_path)
            return await resolver.apply_review_file()
        finally:
            await store.aclose()

    report = asyncio.run(_apply())
    for result in report.results:
        click.echo(f"{result.proposal_id}: {result.outcome.value}: {result.detail}")
    if not report.results:
        click.echo("no confirmed decisions to apply")
    if not report.ok:
        raise SystemExit(EXIT_INCOMPLETE)
