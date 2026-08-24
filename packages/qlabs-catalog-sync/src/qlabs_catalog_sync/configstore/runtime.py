"""Stored configuration -> live objects the engine can actually run.

The neutral home for one question: *given a row the console wrote, what does the engine
need in order to sync with it?* An :class:`~qlabs_catalog_sync.configstore.models.
EndpointRow` becomes a live, ``setup()``-ed :class:`~qlabs_catalog_sync_sdk.contract.
Connector`; a :class:`~qlabs_catalog_sync.configstore.models.SyncPairRow` becomes a
:class:`~qlabs_catalog_sync.config.SyncPairConfig` plus the compiled
:class:`~qlabs_catalog_sync.selection.rules.SelectionRuleSet` its cycles decide scope
against.

Why this module exists
-----------------------

``cli/wiring.py``'s :func:`~qlabs_catalog_sync.cli.wiring.build_connector_pool` builds
connectors from an :class:`~qlabs_catalog_sync.config.EngineConfig` — the static, YAML-
loaded configuration a process started with — plus a resolved-credentials dict. Decision
C6 says an operator registers an endpoint *from the browser*, and decision C1 says that
endpoint takes effect without a restart: such an endpoint exists only as a row in the
configuration store and is not in that ``EngineConfig`` at all. Two WP12 tasks hit that
wall independently — T12.9 (the scheduler's reconcile: *"'add a brand-new endpoint in the
console and start syncing with it' needs a store-backed connector build that does not
exist yet"*) and T12.6, which solved it privately inside ``api/routes/run_control.py``.

This module is that build, in one place, so there is exactly one implementation of it —
the same reason :func:`~qlabs_catalog_sync.configstore.secrets.secret_field_names` was
made public rather than left copied into two callers that could drift apart.

**Dependency direction.** This module imports neither ``api/`` nor ``cli/``. Both of them
consume it — ``api/routes/run_control.py`` builds a loop per request, and the caller that
owns a :data:`~qlabs_catalog_sync.scheduler.RunnerFactory` (``cli/serve_command.py``)
builds one per scheduled pair — so the arrow has to point one way or a route module and
the CLI layer end up importing each other through it.

Credentials
------------

A live credential is handled here (that is what ``setup()`` needs) and is never returned,
stored, logged, or put into an exception message. Resolution goes through
:func:`~qlabs_catalog_sync.configstore.secrets.resolve_connector_kwargs`, which only ever
hands back a value inside a pydantic ``SecretStr``/``SecretBytes``; the resolved kwargs
are passed straight into ``ConfigModel.for_endpoint`` and dropped. Every reason string
:class:`EndpointSetupError` carries is value-free by construction:

* a :class:`~qlabs_catalog_sync.config.SecretNotFoundError` message names only the
  endpoint, the key and the backend variable it looked for (T10.2 pins that);
* a pydantic ``ValidationError`` is reduced to its field *locations* and error *types* —
  its rendered message is deliberately **not** used, because pydantic includes the
  offending input value in it and this is the one place where that input can be a
  credential;
* a :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError`'s ``message`` is documented
  safe to surface (see that module's own docstring), and is the same text
  ``run_control.py`` already put in its 422 before this module existed.

What deliberately still propagates
------------------------------------

:class:`EndpointSetupError` covers "this row will not become a live connector". Two
failures are *not* that, already have their own stable handling, and are left alone:

* :class:`~qlabs_catalog_sync.discovery.ConnectorLookupError` and its subclasses — the
  endpoint names a connector that is not installed, or is installed and broken. A
  registration problem to fix, not a setup failure; ``api/errors.py`` already maps both.
* :class:`~qlabs_catalog_sync.configstore.secrets.SecretRefFormatError` — the stored
  ``secret_ref`` string is malformed or names an unsupported scheme. Detected before any
  backend is touched, and already mapped by ``api/errors.py`` to a dedicated
  ``secret_ref_invalid`` 422 that says more than a generic setup failure would.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import ValidationError

from qlabs_catalog_sync.config import SecretBackend, SecretNotFoundError, SyncPairConfig
from qlabs_catalog_sync.configstore.models import (
    EndpointRow,
    SelectionOverrideRow,
    SelectionRuleRow,
    SyncPairRow,
)
from qlabs_catalog_sync.configstore.secrets import SecretRef, resolve_connector_kwargs
from qlabs_catalog_sync.configstore.types import (
    MatcherKind,
    RuleScope,
    SelectionDecision,
)
from qlabs_catalog_sync.observability import get_logger
from qlabs_catalog_sync.selection.rules import SelectionRuleSet
from qlabs_catalog_sync_sdk.config import ConnectorConfig, ConnectorContext, MetricsHandle
from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.exceptions import ConnectorError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from qlabs_catalog_sync.configstore.service import ConfigService
    from qlabs_catalog_sync.discovery import ConnectorRegistry

__all__ = [
    "INERT_CATALOG_SCHEMA_PATTERN",
    "EndpointSetupError",
    "StoreConnectorPool",
    "build_connector_for_endpoint",
    "close_quietly",
    "derived_catalog_schema_patterns",
    "endpoint_fingerprint",
    "selection_rows_for_pair",
    "selection_rule_set_for_pair",
    "sync_pair_config_for_row",
]

_LOG = get_logger("qlabs.catalog_sync.configstore.runtime")

#: The placeholder :attr:`~qlabs_catalog_sync.config.SyncPairConfig.catalog_schema_patterns`
#: value used for a store-configured pair with no object-scope include-glob rule to project
#: back into D1's flat list. See :func:`derived_catalog_schema_patterns`.
#:
#: ``scheduler.INERT_CATALOG_SCHEMA_PATTERN`` is the same string, and
#: ``tests/configstore/test_runtime.py`` fails if the two ever stop being the same string —
#: see that module for why there are two names for it today.
INERT_CATALOG_SCHEMA_PATTERN: Final[str] = "__rules__.__rules__"


class EndpointSetupError(Exception):
    """A stored endpoint row could not be turned into a live, ``setup()``-ed connector.

    Deliberately a plain :class:`Exception` — not an ``APIError``, not a ``CliError``. This
    module is below both layers and has no business deciding what an HTTP status code or a
    process exit code should be; the caller translates.

    :param endpoint: the endpoint's stored name (``EndpointRow.name``).
    :param reason: a **value-free** explanation. Every construction site in this module
        builds it from information that cannot contain a credential — see the module
        docstring's "Credentials" section for the enumeration.
    """

    def __init__(self, endpoint: str, reason: str) -> None:
        super().__init__(f"endpoint {endpoint!r} could not be set up: {reason}")
        self.endpoint = endpoint
        self.reason = reason


# ==========================================================================================
# Endpoint row -> live connector
# ==========================================================================================


def _validation_reason(exc: ValidationError) -> str:
    """A value-free summary of ``exc``: field locations and error types only.

    ``str(exc)`` is not used on purpose. Pydantic renders the offending ``input_value``
    into its message, and the input here can be a resolved credential — the one place in
    this module where a value is in scope at all. Locations and error tags say everything
    an operator needs ("``space_id``: missing") without the ability to echo one.
    """
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ())) or "<model>"
        parts.append(f"{location} ({error.get('type', 'invalid')})")
    joined = ", ".join(parts) if parts else "no field-level detail available"
    return f"invalid connector settings: {joined}"


async def build_connector_for_endpoint(
    row: EndpointRow,
    registry: ConnectorRegistry,
    *,
    metrics: MetricsHandle | None = None,
    backend_factory: Callable[[SecretRef], SecretBackend] | None = None,
) -> Connector:
    """Build, ``setup()`` and return a live connector for ``row``.

    The store-backed twin of ``cli/wiring.py``'s
    :func:`~qlabs_catalog_sync.cli.wiring.build_connector_pool` inner loop, and the exact
    construction path ``api/routes/endpoints.py``'s healthcheck already uses:
    ``registry.get_connector(row.connector)`` -> resolve kwargs through
    :mod:`~qlabs_catalog_sync.configstore.secrets` -> ``ConfigModel.for_endpoint`` ->
    ``ConnectorContext.build`` -> ``setup()``.

    ``row.secret_ref`` is optional in the schema (T10.1: an endpoint can be registered and
    its non-secret settings edited before a reference is bound). With one, the reference's
    own ``locator`` is the endpoint key handed to ``for_endpoint`` — decoupled from
    ``row.name`` on purpose, so renaming an endpoint or pointing two endpoints at one
    shared credential never touches an environment variable (see
    ``configstore/secrets.py``'s module docstring). Without one, ``row.settings`` is passed
    through as-is under ``row.name``, which is the only sensible reading of "no secrets
    bound yet" and is what a connector declaring no secret fields at all wants anyway.

    Raises :class:`EndpointSetupError` — value-free — when the secret reference does not
    resolve, the resulting settings do not validate, or the connector raises a
    :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError` from ``setup()``. A
    connector that is not installed or is broken still raises
    :class:`~qlabs_catalog_sync.discovery.ConnectorLookupError`, and a malformed
    ``secret_ref`` still raises
    :class:`~qlabs_catalog_sync.configstore.secrets.SecretRefFormatError`; see the module
    docstring for why those two are left alone. Anything else a connector's ``setup()``
    raises — a bug in the connector rather than a configuration problem — propagates
    unchanged, exactly as it does today, but the half-built connector is closed first so a
    failed build cannot leak a session.
    """
    connector_cls = registry.get_connector(row.connector)
    # Connector.ConfigModel is typed as `type[BaseSettings]` on the SDK's ABC; every real
    # connector's ConfigModel is a ConnectorConfig subclass, and only that subclass
    # declares `for_endpoint` (same cast as cli/wiring.py's own).
    config_model_cls = cast(type[ConnectorConfig], connector_cls.ConfigModel)

    kwargs: dict[str, Any]
    if row.secret_ref is not None:
        ref = SecretRef.parse(row.secret_ref)
        try:
            backend = backend_factory(ref) if backend_factory is not None else None
            kwargs = resolve_connector_kwargs(
                ref, config_model_cls, settings=row.settings, backend=backend
            )
        except SecretNotFoundError as exc:
            # SecretNotFoundError's message names the endpoint, the key and the variable
            # it looked for -- never a value (T10.2 pins this).
            raise EndpointSetupError(row.name, str(exc)) from exc
        locator = ref.locator
    else:
        kwargs = dict(row.settings)
        locator = row.name

    try:
        connector_config = config_model_cls.for_endpoint(locator, **kwargs)
    except ValidationError as exc:
        raise EndpointSetupError(row.name, _validation_reason(exc)) from exc

    connector = connector_cls()
    ctx = ConnectorContext.build(config=connector_config, endpoint=row.name, metrics=metrics)
    try:
        await connector.setup(ctx)
    except ConnectorError as exc:
        await close_quietly(connector)
        raise EndpointSetupError(row.name, exc.message) from exc
    except BaseException:
        await close_quietly(connector)
        raise
    return connector


async def close_quietly(connector: Connector) -> None:
    """``await connector.close()``, swallowing whatever it raises.

    Mirrors ``cli/wiring.py``'s :meth:`~qlabs_catalog_sync.cli.wiring.ConnectorPool.close`
    contract at the level of one connector: a failing close is never allowed to hide the
    outcome the caller actually cares about (the error being reported, or the other
    connectors still to be closed).
    """
    with contextlib.suppress(Exception):
        await connector.close()


def endpoint_fingerprint(row: EndpointRow) -> str:
    """A stable digest of everything about ``row`` that changes the connector it builds.

    Exactly three stored fields decide what
    :func:`build_connector_for_endpoint` produces — ``connector`` (which class),
    ``secret_ref`` (which credential) and ``settings`` (everything else it is configured
    with) — plus ``name``, which is the endpoint key the context is built with. Nothing
    else is included: ``role`` is a labelling concern, ``enabled`` is handled separately
    (a disabled endpoint is refused outright rather than rebuilt), and
    ``created_at``/``updated_at`` move on writes that changed nothing this cache cares
    about, which would churn a connector for free.

    The same shape ``scheduler.PairPlan.fingerprint`` uses — a digest over plain stored
    scalars, not over the objects built from them.
    """
    payload = {
        "name": row.name,
        "connector": row.connector,
        "secret_ref": row.secret_ref,
        "settings": row.settings,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class _PooledConnector:
    """One live connector plus the endpoint fingerprint it was built from."""

    connector: Connector
    fingerprint: str


class StoreConnectorPool:
    """Live connectors built lazily from stored endpoint rows, reused until they go stale.

    ``cli/wiring.py``'s :class:`~qlabs_catalog_sync.cli.wiring.ConnectorPool` is built once,
    eagerly, from a static :class:`~qlabs_catalog_sync.config.EngineConfig` and is then a
    plain dict lookup. This one cannot be: under C1/C6 the set of endpoints, and each
    endpoint's own configuration, change while the process runs. So it builds on first use
    and re-reads the row on every :meth:`get`.

    **Reuse is the point.** ``setup()`` is auth plus client construction; it must run once
    per endpoint, not once per cycle. A :meth:`get` for an unchanged endpoint is one indexed
    single-row read and a dict hit.

    **Staleness is decided by the row, not by a notification.** Every :meth:`get` compares
    :func:`endpoint_fingerprint` against the fingerprint the cached connector was built
    from. If an operator fixes a credential reference, edits settings or repoints the
    endpoint at a different connector, the next :meth:`get` builds a new connector and
    **closes the one it replaces** — a long-running service that kept the old one would
    both leak its HTTP session and, worse, keep syncing with the credential the operator
    just replaced.

    Closing the replacement's predecessor has one consequence worth stating plainly: a
    caller that captured a connector from an earlier :meth:`get` and holds it across a
    configuration change is holding a closed object. Callers should ask the pool per cycle
    rather than caching a reference of their own. (The scheduler's own swap semantics line
    up with this: an endpoint edit changes the fingerprint of *every* pair that references
    it, so every runner built on the old connector is rebuilt in the same reconcile pass.
    The exposure is a cycle already in flight at that instant, which C1 documents as
    keeping the configuration it started with.)

    **Disabled endpoints are never built.** ``endpoints.enabled`` defaults to ``False``
    because C6's registration is a multi-step flow ending in an explicit enable. A
    :meth:`get` for a disabled endpoint raises :class:`EndpointSetupError` and drops (and
    closes) any connector cached from when it was enabled — disabling an endpoint releases
    its session rather than parking it indefinitely.

    **Concurrency.** :meth:`get` holds a per-endpoint :class:`asyncio.Lock` across the row
    read and the build, so two concurrent callers asking for the same not-yet-built
    endpoint produce exactly **one** ``setup()`` and both receive the **same** connector;
    the second waits rather than racing. Building twice and discarding one would be safe
    but wasteful; handing out a half-``setup()`` connector would be neither, and the lock
    makes that unrepresentable. The lock is per endpoint, so a slow ``setup()`` for one
    endpoint never blocks a :meth:`get` for another.

    :param config_service: the configuration store to read endpoint rows from.
    :param registry: the connector registry to resolve ``EndpointRow.connector`` through.
    :param metrics: optional metrics handle, forwarded into every connector's context
        (mirrors :class:`~qlabs_catalog_sync.sync.loop.SyncLoop`'s own optional ``metrics``).
    """

    def __init__(
        self,
        config_service: ConfigService,
        registry: ConnectorRegistry,
        *,
        metrics: MetricsHandle | None = None,
    ) -> None:
        self._config_service = config_service
        self._registry = registry
        self._metrics = metrics
        self._entries: dict[str, _PooledConnector] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, endpoint_name: str) -> asyncio.Lock:
        """This endpoint's build lock, created on demand.

        ``setdefault`` on a plain dict with no ``await`` between the miss and the insert is
        atomic under asyncio's single-threaded cooperative scheduling — the same reasoning
        ``run_control.py``'s in-flight set relies on — so two concurrent callers can never
        end up with two different locks for one endpoint.
        """
        return self._locks.setdefault(endpoint_name, asyncio.Lock())

    async def get(self, endpoint_name: str) -> Connector:
        """A live, ``setup()``-ed connector for ``endpoint_name``, built on first use.

        Raises :class:`EndpointSetupError` if no endpoint is configured under that name, if
        it is disabled, or if it will not build (see
        :func:`build_connector_for_endpoint` for exactly which failures become this and
        which propagate).
        """
        async with self._lock_for(endpoint_name):
            row = await self._config_service.get_endpoint(endpoint_name)
            if row is None:
                await self._discard(endpoint_name)
                raise EndpointSetupError(endpoint_name, "no endpoint is configured under that name")
            if not row.enabled:
                await self._discard(endpoint_name)
                raise EndpointSetupError(
                    endpoint_name, "endpoint is disabled; enable it before syncing with it"
                )

            fingerprint = endpoint_fingerprint(row)
            cached = self._entries.get(endpoint_name)
            if cached is not None and cached.fingerprint == fingerprint:
                return cached.connector

            try:
                connector = await build_connector_for_endpoint(
                    row,
                    self._registry,
                    metrics=self._metrics,
                    backend_factory=self._config_service.secret_backend_for,
                )
            except Exception:
                # The stored configuration has moved on, so whatever is cached was built
                # from something that is no longer configured. Serving it would mean an
                # operator's fix silently not taking effect; dropping it means the next
                # get() tries again from scratch.
                await self._discard(endpoint_name)
                raise

            self._entries[endpoint_name] = _PooledConnector(
                connector=connector, fingerprint=fingerprint
            )
            if cached is not None:
                _LOG.info(
                    "endpoint connector rebuilt from changed configuration",
                    endpoint=endpoint_name,
                    connector=row.connector,
                )
                await close_quietly(cached.connector)
            return connector

    async def _discard(self, endpoint_name: str) -> None:
        """Drop and close whatever is cached for ``endpoint_name``, if anything."""
        cached = self._entries.pop(endpoint_name, None)
        if cached is not None:
            await close_quietly(cached.connector)

    def cached_endpoints(self) -> tuple[str, ...]:
        """Endpoint names with a live connector in this pool, in sorted order.

        Diagnostic only — nothing about the pool's behaviour depends on it. Useful for a
        test or an operator asking "what is this process actually holding open".
        """
        return tuple(sorted(self._entries))

    async def close(self) -> None:
        """Close every connector this pool holds and forget them all.

        Best-effort, one connector at a time: a failing close must not hide the rest (the
        same contract as ``cli/wiring.py``'s ``ConnectorPool.close``). The pool is reusable
        afterwards — a later :meth:`get` simply rebuilds.
        """
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            await close_quietly(entry.connector)


# ==========================================================================================
# Pair row -> SyncPairConfig + selection rules
# ==========================================================================================


def derived_catalog_schema_patterns(rule_rows: Sequence[SelectionRuleRow]) -> list[str]:
    """Project a stored rule set back onto D1's flat ``catalog.schema`` pattern list.

    :class:`~qlabs_catalog_sync.config.SyncPairConfig` predates C3 and still requires a
    non-empty ``catalog_schema_patterns``, while a store-configured pair expresses scope as
    an ordered rule set instead. Nothing in the sync path reads the field once
    ``selection_rules`` is supplied explicitly — :func:`~qlabs_catalog_sync.sync.loop
    .rule_set_for_pair` is its only reader, and passing ``selection_rules`` is precisely
    what bypasses it — so this is a *label*, kept as honest as the shapes allow rather than
    invented.

    The projection is the inverse of :func:`~qlabs_catalog_sync.selection.rules
    .object_rules_from_catalog_schema_patterns`: the object-scope, glob, **include** rules
    in ordinal order. It is lossy by construction (exclude rules, tag and owner matchers,
    and per-object overrides have no representation in a flat include list), which is
    exactly why it is not used to decide anything.

    With no such rule the pair's rule set includes nothing by glob at all
    (:data:`~qlabs_catalog_sync.selection.rules.DEFAULT_DECISION` excludes what no rule
    matched), so the placeholder is :data:`INERT_CATALOG_SCHEMA_PATTERN` — a value that
    matches no real Unity Catalog name — rather than ``"*.*"``: if some future reader ever
    does consult this field, it fails closed, the same direction ``sync/loop.py``'s
    "Selection fails closed" section argues for.
    """
    patterns: list[str] = []
    for row in sorted(rule_rows, key=lambda item: item.ordinal):
        if row.scope is not RuleScope.OBJECT:
            continue
        if row.matcher_kind is not MatcherKind.GLOB:
            continue
        if row.decision is not SelectionDecision.INCLUDE:
            continue
        if row.pattern not in patterns:
            patterns.append(row.pattern)
    return patterns or [INERT_CATALOG_SCHEMA_PATTERN]


def sync_pair_config_for_row(
    row: SyncPairRow, rule_rows: Sequence[SelectionRuleRow] = ()
) -> SyncPairConfig:
    """The :class:`~qlabs_catalog_sync.config.SyncPairConfig` for one stored pair.

    Field-for-field from the row — ``configstore/models.py`` mirrors ``SyncPairConfig``
    deliberately — except ``catalog_schema_patterns``, which the row does not have at all
    (C3 moved selection into the ``selection_rules``/``selection_overrides`` tables) and
    which :func:`derived_catalog_schema_patterns` projects from ``rule_rows``.

    ``rule_rows`` defaults to empty, which yields :data:`INERT_CATALOG_SCHEMA_PATTERN`. Pass
    the pair's real rules (from :func:`selection_rows_for_pair`) whenever they are already
    in hand: the projected label is then the closest honest description of the pair's scope
    the old shape can carry. Either way the field is inert — every caller building a cycle
    from a stored pair also passes ``selection_rules=`` explicitly, and
    :class:`~qlabs_catalog_sync.sync.loop.SyncLoop` only falls back to
    ``catalog_schema_patterns`` when that argument is omitted.
    """
    return SyncPairConfig(
        name=row.name,
        source=row.source,
        target=row.target,
        catalog_schema_patterns=derived_catalog_schema_patterns(rule_rows),
        target_space=row.target_space,
        entity_types=list(row.entity_types),
        cadence_seconds=row.cadence_seconds,
        manual_edit_policy=row.manual_edit_policy,
        activation_opt_in=row.activation_opt_in,
    )


async def selection_rows_for_pair(
    config_service: ConfigService, pair_id: uuid.UUID
) -> tuple[list[SelectionRuleRow], list[SelectionOverrideRow]]:
    """Every stored selection rule and override for one pair, across every scope (C3).

    Returned as rows rather than as a compiled rule set because two different things are
    built from them — the rule set itself and
    :func:`derived_catalog_schema_patterns`' projection — and reading the store twice to
    get both would be silly.
    """
    rule_rows: list[SelectionRuleRow] = []
    override_rows: list[SelectionOverrideRow] = []
    for scope in RuleScope:
        rule_rows.extend(await config_service.list_selection_rules(pair_id, scope))
        override_rows.extend(await config_service.list_selection_overrides(pair_id, scope))
    return rule_rows, override_rows


async def selection_rule_set_for_pair(
    config_service: ConfigService, pair_id: uuid.UUID
) -> SelectionRuleSet:
    """This pair's full, ordered, compiled selection rule set (C3), across every scope."""
    rule_rows, override_rows = await selection_rows_for_pair(config_service, pair_id)
    return SelectionRuleSet.from_rows(rule_rows, override_rows)
