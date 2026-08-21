"""Config -> connectors -> state store -> :class:`SyncLoop`, shared by every subcommand.

WP2 / T2.8. This module is the one place that turns an :class:`EngineConfig` into live
connector instances and a migrated state store; ``run``, ``dry-run`` and
``identity-confirm bootstrap`` all call into it rather than each re-deriving the wiring.

Where the scheduler goes
-------------------------

T2.6's scheduler is not implemented yet and this module does not import it. The wiring
here is deliberately split so that landing it is additive:

* :func:`build_connector_pool` builds and ``setup()``s one connector per *endpoint* the
  selected pairs use (a connector is reused across every pair/entity-type combination
  that shares an endpoint, never rebuilt per cycle).
* :func:`build_sync_loop` builds one :class:`~qlabs_catalog_sync.sync.loop.SyncLoop` for
  one pair, over an already-built :class:`ConnectorPool`.
* ``run`` (``cli/sync_commands.py``) drives both once, in a single pass over every
  selected pair and entity type, then closes the pool.

T2.6's scheduler will call :func:`build_connector_pool` and :func:`build_sync_loop`
*exactly the same way* -- build the pool once at process startup, build one
``SyncLoop`` per pair, then instead of ``run``'s one-shot ``for`` loop, hand each pair's
loop to an APScheduler ``AsyncIOScheduler`` job on that pair's own ``cadence_seconds``
(``max_instances=1``, per the T2.6 task) that calls ``run_cycle`` repeatedly for the
life of the process. Nothing about config loading, credential resolution or connector
construction changes when that lands; only what calls ``run_cycle``, and how often, does.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from qlabs_catalog_sync.config import EngineConfig, SyncPairConfig
from qlabs_catalog_sync.discovery import ConnectorLookupError, ConnectorRegistry
from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.observability import HealthRegistry
from qlabs_catalog_sync.selection.rules import SelectionRuleSet
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import SyncLoop, WritePolicy
from qlabs_catalog_sync_sdk.config import (
    ConnectorConfig,
    ConnectorContext,
    MetricsHandle,
    SystemClock,
)
from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.exceptions import ConnectorError
from qlabs_catalog_sync_sdk.models import EntityType

from .errors import EXIT_CONFIG_ERROR, EXIT_ENDPOINT_UNREACHABLE, CliError

__all__ = [
    "ConnectorPool",
    "build_connector_pool",
    "build_state_store",
    "build_sync_loop",
    "select_entity_types",
    "select_pairs",
]


# --------------------------------------------------------------------------------------
# Pair / entity-type selection
# --------------------------------------------------------------------------------------


def select_pairs(config: EngineConfig, names: Sequence[str]) -> list[SyncPairConfig]:
    """The configured pairs to act on: every pair, or exactly the named ones.

    Raises :class:`CliError` for a name that does not exist and for an entirely empty
    config -- both are things the operator can fix in the config or the command line,
    not something a cycle should discover by silently doing nothing.
    """
    if not config.pairs:
        raise CliError("no sync pairs are configured", exit_code=EXIT_CONFIG_ERROR)
    if not names:
        return list(config.pairs)
    by_name = {pair.name: pair for pair in config.pairs}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise CliError(
            f"no such sync pair(s): {', '.join(sorted(set(missing)))}; configured pairs: "
            f"{sorted(by_name)}",
            exit_code=EXIT_CONFIG_ERROR,
        )
    return [by_name[name] for name in names]


def select_entity_types(pair: SyncPairConfig, requested: Sequence[EntityType]) -> list[EntityType]:
    """The entity types to sync for ``pair``: every type it configures, or the overlap
    with ``requested`` when the caller narrowed it via ``--entity-type``."""
    if not requested:
        return list(pair.entity_types)
    allowed = set(pair.entity_types)
    return [entity_type for entity_type in requested if entity_type in allowed]


# --------------------------------------------------------------------------------------
# State store
# --------------------------------------------------------------------------------------


async def build_state_store(state_db_url: str, *, migrate: bool = True) -> StateStore:
    """A :class:`StateStore` over ``state_db_url``, migrated to head first.

    Migrating here, on every command that touches the store (``run``, ``dry-run``,
    every ``identity-confirm`` subcommand), is a deliberate decision: nobody has to
    remember a separate "run migrations first" step before the first cycle, and Alembic
    upgrading an already-current schema to head is a no-op, so this costs nothing on
    every later invocation. Pass ``migrate=False`` only for a store already known
    current (used by the CLI's own tests, which migrate once per fixture).
    """
    if migrate:
        try:
            await asyncio.to_thread(upgrade_to_head, state_db_url)
        except Exception as exc:  # noqa: BLE001 - any Alembic failure must be reported cleanly
            raise CliError(
                f"failed to migrate the state store at {state_db_url!r}: "
                f"{type(exc).__name__}: {exc}",
                exit_code=EXIT_CONFIG_ERROR,
            ) from exc
    return StateStore.from_url(state_db_url)


def build_identity_resolver(store: StateStore, review_path: Path) -> IdentityResolver:
    """An :class:`IdentityResolver` over ``store``, using ``review_path`` for proposals."""
    return IdentityResolver(store, review_path=review_path)


# --------------------------------------------------------------------------------------
# Connectors
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ConnectorPool:
    """Live, ``setup()``-ed connectors keyed by their configured endpoint key.

    Built once per CLI invocation and reused across every pair/entity-type combination
    that shares an endpoint -- a connector's ``setup()`` (auth, client construction) runs
    exactly once per endpoint, never once per cycle.
    """

    connectors: dict[str, Connector] = field(default_factory=dict)

    def get(self, endpoint_key: str) -> Connector:
        return self.connectors[endpoint_key]

    async def close(self) -> None:
        """Close every connector. Best-effort: one failing close must not hide the rest."""
        for connector in self.connectors.values():
            with contextlib.suppress(Exception):
                await connector.close()


async def build_connector_pool(
    *,
    config: EngineConfig,
    credentials: dict[str, dict[str, Any]],
    endpoint_keys: Iterable[str],
    registry: ConnectorRegistry,
    metrics: MetricsHandle,
) -> ConnectorPool:
    """Build and ``setup()`` one connector per key in ``endpoint_keys``.

    Only the endpoints the selected pairs actually use are built -- not every endpoint
    in ``config`` -- so a ``--pair`` filter that excludes an unreachable endpoint never
    has to reach it. Every failure mode is turned into a :class:`CliError` with a
    deliberate exit code rather than left as a raw exception: an unregistered or broken
    connector is a config problem (:data:`~qlabs_catalog_sync.cli.errors.
    EXIT_CONFIG_ERROR`); invalid connector settings/secrets likewise; a connector that
    raises during ``setup()`` (auth rejected, host unreachable) is
    :data:`~qlabs_catalog_sync.cli.errors.EXIT_ENDPOINT_UNREACHABLE`.
    """
    pool = ConnectorPool()
    clock = SystemClock()
    for endpoint_key in sorted(set(endpoint_keys)):
        endpoint_config = config.endpoints[endpoint_key]
        try:
            connector_cls = registry.get_connector(endpoint_config.connector)
        except ConnectorLookupError as exc:
            raise CliError(
                f"endpoint {endpoint_key!r}: {exc}", exit_code=EXIT_CONFIG_ERROR
            ) from exc

        # Connector.ConfigModel is typed as `type[BaseSettings]` on the SDK's ABC (any
        # pydantic-settings model satisfies the contract in principle); every real
        # connector's ConfigModel is in fact a ConnectorConfig subclass, and only that
        # subclass declares `for_endpoint`.
        config_model_cls = cast(type[ConnectorConfig], connector_cls.ConfigModel)
        try:
            connector_config = config_model_cls.for_endpoint(
                endpoint_key, **credentials.get(endpoint_key, {})
            )
        except ValidationError as exc:
            raise CliError(
                f"endpoint {endpoint_key!r} ({endpoint_config.connector!r}): invalid "
                f"connector settings: {exc}",
                exit_code=EXIT_CONFIG_ERROR,
            ) from exc

        connector = connector_cls()
        context = ConnectorContext.build(
            config=connector_config, endpoint=endpoint_key, metrics=metrics, clock=clock
        )
        try:
            await connector.setup(context)
        except ConnectorError as exc:
            raise CliError(
                f"endpoint {endpoint_key!r} ({endpoint_config.connector!r}) is "
                f"unreachable: {exc.message}",
                exit_code=EXIT_ENDPOINT_UNREACHABLE,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - a connector bug still means "unreachable"
            raise CliError(
                f"endpoint {endpoint_key!r} ({endpoint_config.connector!r}) failed to "
                f"start: {type(exc).__name__}: {exc}",
                exit_code=EXIT_ENDPOINT_UNREACHABLE,
            ) from exc

        pool.connectors[endpoint_key] = connector
    return pool


# --------------------------------------------------------------------------------------
# The loop itself
# --------------------------------------------------------------------------------------


def build_sync_loop(
    *,
    pair: SyncPairConfig,
    pool: ConnectorPool,
    store: StateStore,
    resolver: IdentityResolver,
    metrics: MetricsHandle,
    health: HealthRegistry,
    dry_run: bool,
    create_missing: bool,
    write_policy: WritePolicy | None = None,
    selection_rules: SelectionRuleSet | None = None,
) -> SyncLoop:
    """One :class:`SyncLoop` for ``pair``, over the connectors already in ``pool``.

    See the module docstring for exactly how T2.6's scheduler is meant to reuse this.

    ``selection_rules`` is passed straight through to
    :class:`~qlabs_catalog_sync.sync.loop.SyncLoop`, which falls back to
    ``rule_set_for_pair(pair)`` -- D1's flat ``catalog_schema_patterns`` -- only when it is
    omitted. That fallback is right for a YAML-configured pair and wrong for a
    console-configured one: C3 moved selection into the ``selection_rules``/
    ``selection_overrides`` tables, so a database-backed pair's real rule set (built with
    :func:`~qlabs_catalog_sync.configstore.runtime.selection_rule_set_for_pair`) has to
    reach the cycle somehow, and T11.3 built exactly this seam on ``SyncLoop.__init__``
    for it. Without the parameter here, a caller wiring a store-backed pair could not use
    this function at all and would have to construct ``SyncLoop`` by hand.
    """
    return SyncLoop(
        pair=pair,
        source=pool.get(pair.source),
        target=pool.get(pair.target),
        store=store,
        resolver=resolver,
        selection_rules=selection_rules,
        metrics=metrics,
        health=health,
        write_policy=write_policy,
        create_missing=create_missing,
        dry_run=dry_run,
    )
