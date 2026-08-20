"""``run`` and ``dry-run`` -- the two ways to execute the sync loop from the CLI.

WP2 / T2.8. Both commands share :func:`execute_cycles`: load config, resolve pairs and
entity types, build connectors, then call
:meth:`~qlabs_catalog_sync.sync.loop.SyncLoop.run_cycle` once per pair/entity-type
combination. The only difference between them is the ``dry_run`` flag passed through to
every :class:`~qlabs_catalog_sync.sync.loop.SyncLoop` -- which is also the whole safety
story: ``dry_run=True`` makes the loop itself skip every ``create``/``update`` call (see
``sync/loop.py``), so ``dry-run`` performs zero mutations by construction, not because
this module remembers not to call something.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import click

from qlabs_catalog_sync.observability import HealthRegistry, PrometheusMetrics, get_logger
from qlabs_catalog_sync.sync.loop import SyncRunReport
from qlabs_catalog_sync_sdk.models import EntityType

from .config_loading import load_engine_config, resolve_credentials
from .deps import RuntimeContext
from .exit_status import classify_exit
from .render import render_report_text, render_summary_text
from .wiring import (
    build_connector_pool,
    build_identity_resolver,
    build_state_store,
    build_sync_loop,
    select_entity_types,
    select_pairs,
)

__all__ = ["dry_run", "run"]

_LOG = get_logger("qlabs.catalog_sync.cli.sync")

_ENTITY_TYPE_CHOICE = click.Choice([entity_type.value for entity_type in EntityType])

_CONFIG_OPTION = click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Engine config file (YAML or JSON) -- endpoints and sync pairs.",
)
_PAIR_OPTION = click.option(
    "--pair",
    "pair_names",
    multiple=True,
    help="Restrict to this sync pair (repeatable). Default: every configured pair.",
)
_ENTITY_TYPE_OPTION = click.option(
    "--entity-type",
    "entity_type_values",
    multiple=True,
    type=_ENTITY_TYPE_CHOICE,
    help="Restrict to this entity type (repeatable). Default: every type the pair configures.",
)
_CREATE_MISSING_OPTION = click.option(
    "--create-missing/--no-create-missing",
    default=False,
    show_default=True,
    help=(
        "Create a target object when a source object has no confirmed identity binding. "
        "Off by default -- see `identity-confirm bootstrap` and `identity-confirm confirm`."
    ),
)


async def execute_cycles(
    *,
    config_path: Path,
    runtime: RuntimeContext,
    pair_names: Sequence[str],
    entity_type_values: Sequence[str],
    create_missing: bool,
    dry_run: bool,
) -> list[SyncRunReport]:
    """Run one cycle per selected pair/entity-type combination and return every report."""
    engine_config = load_engine_config(config_path)
    entity_types = [EntityType(value) for value in entity_type_values]
    pairs = select_pairs(engine_config, pair_names)
    credentials = resolve_credentials(engine_config)
    needed_endpoints = {pair.source for pair in pairs} | {pair.target for pair in pairs}

    metrics = PrometheusMetrics()
    health = HealthRegistry()
    store = await build_state_store(runtime.state_db)
    resolver = build_identity_resolver(store, runtime.review_path)
    pool = await build_connector_pool(
        config=engine_config,
        credentials=credentials,
        endpoint_keys=needed_endpoints,
        registry=runtime.connector_registry(),
        metrics=metrics,
    )
    try:
        reports: list[SyncRunReport] = []
        for pair in pairs:
            for entity_type in select_entity_types(pair, entity_types):
                loop = build_sync_loop(
                    pair=pair,
                    pool=pool,
                    store=store,
                    resolver=resolver,
                    metrics=metrics,
                    health=health,
                    dry_run=dry_run,
                    create_missing=create_missing,
                )
                reports.append(await loop.run_cycle(entity_type))
        return reports
    finally:
        await pool.close()
        await store.aclose()


@click.command()
@_CONFIG_OPTION
@_PAIR_OPTION
@_ENTITY_TYPE_OPTION
@_CREATE_MISSING_OPTION
@click.pass_obj
def run(
    runtime: RuntimeContext,
    config_path: Path,
    pair_names: tuple[str, ...],
    entity_type_values: tuple[str, ...],
    create_missing: bool,
) -> None:
    """Run one sync cycle for every selected pair, applying writes to Qlik.

    Exit codes: 0 ok, 1 ran but some records failed, 2 config invalid, 3 an endpoint was
    unreachable.
    """
    reports = asyncio.run(
        execute_cycles(
            config_path=config_path,
            runtime=runtime,
            pair_names=pair_names,
            entity_type_values=entity_type_values,
            create_missing=create_missing,
            dry_run=False,
        )
    )
    for report in reports:
        click.echo(render_report_text(report))
    click.echo(render_summary_text(reports))
    exit_code = classify_exit(reports)
    if exit_code != 0:
        raise SystemExit(exit_code)


@click.command("dry-run")
@_CONFIG_OPTION
@_PAIR_OPTION
@_ENTITY_TYPE_OPTION
@_CREATE_MISSING_OPTION
@click.option(
    "--plan-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("dry-run-plan.json"),
    show_default=True,
    help="Where to write the machine-readable JSON plan.",
)
@click.pass_obj
def dry_run(
    runtime: RuntimeContext,
    config_path: Path,
    pair_names: tuple[str, ...],
    entity_type_values: tuple[str, ...],
    create_missing: bool,
    plan_file: Path,
) -> None:
    """Compute the full planned write set and apply nothing.

    Performs zero mutations against every target: `SyncLoop` skips every create/update
    call when run in dry-run mode, so nothing reaches Qlik regardless of what the plan
    contains. Writes the plan both as a human-readable summary on stdout and as a
    machine-readable JSON file (`--plan-file`) -- every intended create/update with its
    field-level diff, what could not be carried across and why, and what would be
    reported as an orphan.

    Exit codes: 0 ok, 1 the plan found records that would fail or that are held back
    with work outstanding, 2 config invalid, 3 an endpoint was unreachable.
    """
    reports = asyncio.run(
        execute_cycles(
            config_path=config_path,
            runtime=runtime,
            pair_names=pair_names,
            entity_type_values=entity_type_values,
            create_missing=create_missing,
            dry_run=True,
        )
    )

    plan = {
        "kind": "qlabs-catalog-sync/dry-run-plan",
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "config_file": str(config_path),
        "runs": [report.to_json() for report in reports],
    }
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for report in reports:
        click.echo(render_report_text(report))
    click.echo(render_summary_text(reports))
    click.echo(f"\nplan written to {plan_file}  (zero mutations were applied)")

    exit_code = classify_exit(reports)
    if exit_code != 0:
        raise SystemExit(exit_code)
