"""Stored configuration -> live objects (``configstore/runtime.py``).

Behaviour, not mocks: real :class:`~qlabs_catalog_sync.configstore.models.EndpointRow`s
written through a real :class:`~qlabs_catalog_sync.configstore.service.ConfigService` into
the real, migrated SQLite database ``tests/configstore/conftest.py`` already builds, and
real :class:`~qlabs_catalog_sync_sdk.contract.Connector` implementations resolved through a
real :class:`~qlabs_catalog_sync.discovery.ConnectorRegistry`. The connectors below are
genuine ``Connector`` subclasses with working ``setup``/``close`` -- they record what was
done to them, they do not stand in for something that would behave differently.

The three things this suite exists to hold down:

* **A stale connector is never served.** ``test_a_credential_fix_in_the_store_reaches_the
  _next_get`` is the one that matters: it is the scenario the pool exists for (an operator
  repoints an endpoint at a working credential and the long-running service must stop using
  the broken one), and it fails if :class:`~qlabs_catalog_sync.configstore.runtime.
  StoreConnectorPool` ever serves a cached connector past a configuration change.
* **No credential reaches a message or a log record.** A distinctive sentinel stands in for
  a live credential throughout, and the log assertions run the engine's *real* structlog
  processor chain (``qlabs_catalog_sync.observability.REDACTION_TEST_PROCESSORS``), the same
  technique ``tests/configstore/test_secrets.py`` established.
* **``sync_pair_config_for_row`` and ``scheduler.ConfigStorePairSource`` do not diverge.**
  They were two implementations of one translation when this module was written (
  ``run_control.py``'s placeholder ``["*.*"]`` failed *open*; ``scheduler.py``'s projection
  fails *closed*). The pair-config section at the bottom compares what each produces for the
  same stored rows and fails if they ever stop agreeing -- including on the inert
  placeholder string itself, which still has two names today (see
  ``test_the_inert_pattern_is_one_string_under_two_names``).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
import structlog.testing
from pydantic import SecretStr
from sqlalchemy import Engine

from qlabs_catalog_sync import scheduler as scheduler_module
from qlabs_catalog_sync.configstore.models import EndpointRow
from qlabs_catalog_sync.configstore.runtime import (
    INERT_CATALOG_SCHEMA_PATTERN,
    EndpointSetupError,
    StoreConnectorPool,
    build_connector_for_endpoint,
    derived_catalog_schema_patterns,
    endpoint_fingerprint,
    selection_rows_for_pair,
    selection_rule_set_for_pair,
    sync_pair_config_for_row,
)
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.configstore.types import (
    EndpointRole,
    MatcherKind,
    RuleScope,
    SelectionDecision,
)
from qlabs_catalog_sync.discovery import ConnectorNotRegisteredError, ConnectorRegistry
from qlabs_catalog_sync.observability import REDACTION_TEST_PROCESSORS, get_logger
from qlabs_catalog_sync.scheduler import ConfigStorePairSource
from qlabs_catalog_sync_sdk.config import ConnectorConfig, ConnectorContext
from qlabs_catalog_sync_sdk.contract import (
    CapabilityManifestBase,
    Connector,
    HealthState,
    HealthStatus,
    IdentityRef,
    ListChangedResult,
    Watermark,
)
from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_catalog_sync_sdk.models import EntityType, NeutralEntity
from qlabs_catalog_sync_sdk.testing import databricks_shaped_manifest, qlik_shaped_manifest

#: Stands in for a live credential. Distinctive enough that finding it in a message, a
#: reason or a log record can only mean a real leak -- the same device
#: ``tests/configstore/test_secrets.py`` uses.
SENTINEL = "sk-wire-store-connectors-do-not-leak-4c1f9a2e"

#: A second, distinct credential value: what the operator repoints a broken endpoint at.
FIXED_SENTINEL = "sk-wire-store-connectors-the-fixed-one-77b0d3e5"

ACTOR = "test-operator"
NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 20, 13, 0, 0, tzinfo=UTC)

SOURCE_ENDPOINT = "databricks_prod"
TARGET_ENDPOINT = "qlik_acme"


# ==========================================================================================
# Real connectors: they record what happened to them, they do not fake it
# ==========================================================================================


class SourceConfig(ConnectorConfig):
    """A read-only source connector's config: plain fields only, no secrets."""

    host: str
    port: int = 443


class TargetConfig(ConnectorConfig):
    """The sole write connector's config: one plain field, one required secret."""

    space_id: str
    api_key: SecretStr


#: Every connector instance any test in this module constructed, in construction order.
#: The pool builds instances itself (``connector_cls()``), so this is how a test gets a
#: handle on one it never constructed -- notably the half-built one a failing ``setup()``
#: must still close.
BUILT: list[RecordingConnector] = []


class RecordingConnector(Connector):
    """A real connector that records its own lifecycle.

    Not a mock: ``setup`` and ``close`` really run and really change this object's state,
    which is what the pool's reuse/invalidation assertions read back. ``setup`` yields to
    the event loop before finishing so that "two concurrent ``get()``s" is a genuine
    interleaving rather than a straight-line call that could never have raced.
    """

    manifest_factory: ClassVar[Any] = staticmethod(databricks_shaped_manifest)
    setup_error: ClassVar[Exception | None] = None
    close_error: ClassVar[Exception | None] = None

    def __init__(self) -> None:
        super().__init__()
        self.setup_calls = 0
        self.close_calls = 0
        self.context: ConnectorContext[Any] | None = None
        BUILT.append(self)

    def capabilities(self) -> CapabilityManifestBase:
        manifest: CapabilityManifestBase = type(self).manifest_factory()
        return manifest

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        await asyncio.sleep(0)
        self.setup_calls += 1
        self.context = ctx
        await asyncio.sleep(0)
        if type(self).setup_error is not None:
            raise type(self).setup_error

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(state=HealthState.GREEN, detail="fake")

    async def close(self) -> None:
        self.close_calls += 1
        if type(self).close_error is not None:
            raise type(self).close_error

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        raise NotImplementedError

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        raise NotImplementedError


class DatabricksLikeConnector(RecordingConnector):
    name = "databricks"
    ConfigModel = SourceConfig


class CollibraLikeConnector(RecordingConnector):
    """A second source-shaped connector, so "the operator repointed the endpoint at a
    different connector" is a real change of class rather than a relabelling."""

    name = "collibra"
    ConfigModel = SourceConfig


class QlikLikeConnector(RecordingConnector):
    """The sole write target. ``WRITE_CONNECTOR_NAME`` is ``"qlik"``, and
    ``ConfigService`` enforces the v1 direction guardrail on every pair it stores, so a
    pair's target endpoint has to name exactly this connector."""

    name = "qlik"
    ConfigModel = TargetConfig
    manifest_factory: ClassVar[Any] = staticmethod(qlik_shaped_manifest)


class UnreachableConnector(RecordingConnector):
    """A connector whose ``setup()`` fails the way a real one does when the tenant
    rejects its credentials."""

    name = "unreachable"
    ConfigModel = SourceConfig
    setup_error: ClassVar[Exception | None] = AuthError("tenant rejected the credentials")


class UncloseableConnector(RecordingConnector):
    """A connector whose ``close()`` raises. Best-effort closing has to survive it."""

    name = "uncloseable"
    ConfigModel = SourceConfig
    close_error: ClassVar[Exception | None] = RuntimeError("close blew up")


# ==========================================================================================
# Fixtures
# ==========================================================================================


@pytest.fixture(autouse=True)
def _reset_built_instances() -> None:
    BUILT.clear()


@pytest.fixture
def registry() -> ConnectorRegistry:
    return ConnectorRegistry(
        {
            "databricks": DatabricksLikeConnector,
            "collibra": CollibraLikeConnector,
            "qlik": QlikLikeConnector,
            "unreachable": UnreachableConnector,
            "uncloseable": UncloseableConnector,
        },
        {},
    )


@pytest.fixture
def config_service(engine: Engine, registry: ConnectorRegistry) -> ConfigService:
    """A real service over the migrated temp database ``conftest.py`` built."""
    return ConfigService(engine, registry)


@pytest.fixture
def pool(config_service: ConfigService, registry: ConnectorRegistry) -> StoreConnectorPool:
    return StoreConnectorPool(config_service, registry)


@pytest.fixture
def bound_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """``env:QLIK_ACME`` resolves; ``env:QLIK_FIXED`` resolves to a different value."""
    monkeypatch.setenv("QLIK_ACME__API_KEY", SENTINEL)
    monkeypatch.setenv("QLIK_FIXED__API_KEY", FIXED_SENTINEL)


async def _create_target_endpoint(
    config_service: ConfigService,
    *,
    name: str = TARGET_ENDPOINT,
    secret_ref: str | None = "env:QLIK_ACME",
    settings: dict[str, object] | None = None,
    enabled: bool = True,
) -> EndpointRow:
    return await config_service.create_endpoint(
        name=name,
        connector="qlik",
        role=EndpointRole.TARGET,
        settings=settings if settings is not None else {"space_id": "Sales Space"},
        secret_ref=secret_ref,
        enabled=enabled,
        actor=ACTOR,
        now=NOW,
    )


async def _create_source_endpoint(
    config_service: ConfigService,
    *,
    name: str = SOURCE_ENDPOINT,
    connector: str = "databricks",
    settings: dict[str, object] | None = None,
    enabled: bool = True,
) -> EndpointRow:
    return await config_service.create_endpoint(
        name=name,
        connector=connector,
        role=EndpointRole.SOURCE,
        settings=settings if settings is not None else {"host": "adb-1.azuredatabricks.net"},
        secret_ref=None,
        enabled=enabled,
        actor=ACTOR,
        now=NOW,
    )


# ==========================================================================================
# build_connector_for_endpoint: a stored row becomes a live, set-up connector
# ==========================================================================================


async def test_a_stored_row_becomes_a_live_setup_connector(
    config_service: ConfigService, registry: ConnectorRegistry, bound_credential: None
) -> None:
    """The C6 case end to end: an endpoint that exists only as a row, with a secret
    reference resolving through the environment, becomes a connector that has really been
    ``setup()``-ed and really carries the resolved credential."""
    row = await _create_target_endpoint(config_service)

    connector = await build_connector_for_endpoint(row, registry)

    assert isinstance(connector, QlikLikeConnector)
    assert connector.setup_calls == 1
    ctx = connector.context
    assert ctx is not None
    assert ctx.endpoint == TARGET_ENDPOINT
    # The live credential really arrived, resolved from env:QLIK_ACME -- not a placeholder.
    assert isinstance(ctx.config, TargetConfig)
    assert ctx.config.api_key.get_secret_value() == SENTINEL
    assert ctx.config.space_id == "Sales Space"


async def test_an_endpoint_with_no_secret_ref_uses_its_own_name_and_settings(
    config_service: ConfigService, registry: ConnectorRegistry
) -> None:
    """``secret_ref`` is nullable (T10.1): a connector declaring no secret field is fully
    configured by ``settings`` alone."""
    row = await _create_source_endpoint(config_service)

    connector = await build_connector_for_endpoint(row, registry)

    assert connector.setup_calls == 1
    ctx = connector.context
    assert ctx is not None
    assert ctx.endpoint == SOURCE_ENDPOINT
    assert isinstance(ctx.config, SourceConfig)
    assert ctx.config.host == "adb-1.azuredatabricks.net"


async def test_a_connector_that_is_not_registered_still_raises_the_lookup_error(
    config_service: ConfigService, registry: ConnectorRegistry
) -> None:
    """A registration problem is not a setup failure: ``api/errors.py`` already maps
    ``ConnectorLookupError``, and wrapping it would lose that."""
    row = await _create_source_endpoint(config_service)
    row.connector = "nope-not-installed"

    with pytest.raises(ConnectorNotRegisteredError):
        await build_connector_for_endpoint(row, registry)


async def test_a_setup_failure_raises_endpoint_setup_error_and_closes_the_half_built_connector(
    config_service: ConfigService, registry: ConnectorRegistry
) -> None:
    row = await _create_source_endpoint(config_service, connector="unreachable")

    with pytest.raises(EndpointSetupError) as excinfo:
        await build_connector_for_endpoint(row, registry)

    assert excinfo.value.endpoint == SOURCE_ENDPOINT
    assert "tenant rejected the credentials" in excinfo.value.reason
    # The instance the failed build constructed is closed, not leaked.
    (built,) = BUILT
    assert built.close_calls == 1


# ==========================================================================================
# No credential reaches a message, a reason, or a log record
# ==========================================================================================


async def test_an_unresolvable_secret_ref_names_the_endpoint_and_not_the_value(
    config_service: ConfigService, registry: ConnectorRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The required secret field is missing from the environment while a *sibling*
    variable under the same prefix holds the sentinel -- so "the sentinel is absent from
    the message" is a real assertion about what the failure path echoes, not a vacuous one
    about a value that was never in scope."""
    monkeypatch.delenv("QLIK_ACME__API_KEY", raising=False)
    monkeypatch.setenv("QLIK_ACME__SOMETHING_ELSE", SENTINEL)
    row = await _create_target_endpoint(config_service)

    with structlog.testing.capture_logs(processors=REDACTION_TEST_PROCESSORS) as entries:
        with pytest.raises(EndpointSetupError) as excinfo:
            await build_connector_for_endpoint(row, registry)
        get_logger().error(
            "endpoint setup failed",
            endpoint=excinfo.value.endpoint,
            reason=excinfo.value.reason,
            error=excinfo.value,
        )

    exc = excinfo.value
    assert exc.endpoint == TARGET_ENDPOINT
    assert TARGET_ENDPOINT in str(exc)
    assert SENTINEL not in str(exc)
    assert SENTINEL not in exc.reason
    # It still says what is actually wrong: the variable it looked for.
    assert "QLIK_ACME__API_KEY" in exc.reason
    assert SENTINEL not in str(entries)


async def test_invalid_settings_are_reported_by_field_never_by_value(
    registry: ConnectorRegistry,
) -> None:
    """``ValidationError`` is the one failure whose rendered message embeds the offending
    *input*, and that input can be a credential -- so the reason is built from field
    locations and error types only.

    The row here is constructed directly rather than through ``ConfigService``: the service
    validates ``settings`` on write and would refuse this one. That is exactly why the
    guarantee has to live in ``build_connector_for_endpoint`` too -- it takes a row, from
    wherever a row comes from.
    """
    row = EndpointRow(
        name="badly_configured",
        connector="databricks",
        role=EndpointRole.SOURCE,
        secret_ref=None,
        settings={"host": "adb-1.azuredatabricks.net", "port": SENTINEL},
        enabled=True,
        created_at=NOW,
        updated_at=NOW,
    )

    with structlog.testing.capture_logs(processors=REDACTION_TEST_PROCESSORS) as entries:
        with pytest.raises(EndpointSetupError) as excinfo:
            await build_connector_for_endpoint(row, registry)
        get_logger().error("endpoint setup failed", reason=excinfo.value.reason)

    assert SENTINEL not in excinfo.value.reason
    assert SENTINEL not in str(excinfo.value)
    assert "port" in excinfo.value.reason
    assert SENTINEL not in str(entries)


async def test_a_successful_build_logs_nothing_carrying_the_credential(
    config_service: ConfigService, pool: StoreConnectorPool, bound_credential: None
) -> None:
    """Everything this module logs on the happy path (including the rebuild line) captured
    through the engine's real processor chain."""
    await _create_target_endpoint(config_service)

    with structlog.testing.capture_logs(processors=REDACTION_TEST_PROCESSORS) as entries:
        await pool.get(TARGET_ENDPOINT)
        await config_service.update_endpoint(
            TARGET_ENDPOINT,
            settings={"space_id": "Another Space"},
            actor=ACTOR,
            now=LATER,
        )
        await pool.get(TARGET_ENDPOINT)

    assert SENTINEL not in str(entries)


# ==========================================================================================
# StoreConnectorPool: build once, reuse, invalidate on a real change
# ==========================================================================================


async def test_the_pool_builds_once_and_reuses(
    config_service: ConfigService, pool: StoreConnectorPool, bound_credential: None
) -> None:
    """``setup()`` is auth plus client construction: once per endpoint, not once per
    cycle."""
    await _create_target_endpoint(config_service)

    first = await pool.get(TARGET_ENDPOINT)
    second = await pool.get(TARGET_ENDPOINT)

    assert first is second
    assert isinstance(first, RecordingConnector)
    assert first.setup_calls == 1
    assert len(BUILT) == 1
    assert pool.cached_endpoints() == (TARGET_ENDPOINT,)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("settings", {"host": "adb-2.azuredatabricks.net"}),
        ("secret_ref", "env:SOMETHING_NEW"),
        ("connector", "collibra"),
    ],
)
async def test_changing_a_semantic_field_rebuilds_and_closes_the_replaced_connector(
    config_service: ConfigService,
    pool: StoreConnectorPool,
    field: str,
    value: object,
) -> None:
    """The three stored fields that decide what connector a row builds. Each one, changed,
    must produce a *different* live connector and must leave the previous one closed --
    a long-running service that kept it would leak its session and keep using
    configuration the operator has already replaced."""
    await _create_source_endpoint(config_service)

    first = await pool.get(SOURCE_ENDPOINT)
    await config_service.update_endpoint(
        SOURCE_ENDPOINT, actor=ACTOR, now=LATER, **{field: value}
    )
    second = await pool.get(SOURCE_ENDPOINT)

    assert second is not first
    assert isinstance(first, RecordingConnector)
    assert isinstance(second, RecordingConnector)
    assert first.close_calls == 1, "the replaced connector must be closed, not leaked"
    assert second.close_calls == 0
    assert second.setup_calls == 1


async def test_a_credential_fix_in_the_store_reaches_the_next_get(
    config_service: ConfigService, pool: StoreConnectorPool, bound_credential: None
) -> None:
    """The scenario this pool exists for. An endpoint is bound to one credential; an
    operator repoints it at another in the console; the very next ``get()`` must hand back
    a connector holding the *new* value. This test fails if a stale connector is ever
    served after a credential fix."""
    await _create_target_endpoint(config_service, secret_ref="env:QLIK_ACME")

    before = await pool.get(TARGET_ENDPOINT)
    assert isinstance(before, RecordingConnector)
    assert before.context is not None
    assert isinstance(before.context.config, TargetConfig)
    assert before.context.config.api_key.get_secret_value() == SENTINEL

    await config_service.update_endpoint(
        TARGET_ENDPOINT, secret_ref="env:QLIK_FIXED", actor=ACTOR, now=LATER
    )

    after = await pool.get(TARGET_ENDPOINT)
    assert after is not before
    assert isinstance(after, RecordingConnector)
    assert after.context is not None
    assert isinstance(after.context.config, TargetConfig)
    assert after.context.config.api_key.get_secret_value() == FIXED_SENTINEL
    assert before.close_calls == 1


async def test_an_unrelated_write_does_not_churn_the_connector(
    config_service: ConfigService, pool: StoreConnectorPool
) -> None:
    """A configuration write that changed nothing this endpoint's connector depends on
    (here: another endpoint entirely) must not re-``setup()`` it -- that is the whole
    point of fingerprinting the semantic fields rather than watching the generation."""
    await _create_source_endpoint(config_service)

    first = await pool.get(SOURCE_ENDPOINT)
    await _create_source_endpoint(config_service, name="unrelated")
    second = await pool.get(SOURCE_ENDPOINT)

    assert second is first
    assert isinstance(first, RecordingConnector)
    assert first.setup_calls == 1
    assert first.close_calls == 0


async def test_endpoint_fingerprint_ignores_timestamps_and_role(
    config_service: ConfigService,
) -> None:
    row = await _create_source_endpoint(config_service)
    before = endpoint_fingerprint(row)

    row.updated_at = LATER
    row.role = EndpointRole.TARGET
    assert endpoint_fingerprint(row) == before

    row.settings = {"host": "adb-2.azuredatabricks.net"}
    assert endpoint_fingerprint(row) != before


# ==========================================================================================
# Disabled, missing, and closing
# ==========================================================================================


async def test_a_disabled_endpoint_is_refused_and_never_built(
    config_service: ConfigService, pool: StoreConnectorPool, bound_credential: None
) -> None:
    """``endpoints.enabled`` defaults to ``False`` because C6's registration ends with an
    explicit enable. A half-registered endpoint must not be reached at all."""
    await _create_target_endpoint(config_service, enabled=False)

    with pytest.raises(EndpointSetupError) as excinfo:
        await pool.get(TARGET_ENDPOINT)

    assert excinfo.value.endpoint == TARGET_ENDPOINT
    assert "disabled" in excinfo.value.reason
    assert BUILT == [], "a disabled endpoint must not be constructed, let alone set up"


async def test_disabling_an_endpoint_drops_and_closes_its_connector(
    config_service: ConfigService, pool: StoreConnectorPool
) -> None:
    await _create_source_endpoint(config_service)
    connector = await pool.get(SOURCE_ENDPOINT)
    assert isinstance(connector, RecordingConnector)

    await config_service.update_endpoint(SOURCE_ENDPOINT, enabled=False, actor=ACTOR, now=LATER)

    with pytest.raises(EndpointSetupError):
        await pool.get(SOURCE_ENDPOINT)
    assert connector.close_calls == 1
    assert pool.cached_endpoints() == ()


async def test_an_unconfigured_endpoint_is_refused_by_name(pool: StoreConnectorPool) -> None:
    with pytest.raises(EndpointSetupError) as excinfo:
        await pool.get("never-registered")

    assert excinfo.value.endpoint == "never-registered"
    assert "no endpoint is configured" in excinfo.value.reason


async def test_a_failed_rebuild_does_not_leave_the_stale_connector_in_the_pool(
    config_service: ConfigService, pool: StoreConnectorPool
) -> None:
    """If the rebuild fails, the cached connector was still built from configuration that
    no longer exists. Serving it would mean an operator's edit silently not taking
    effect."""
    await _create_source_endpoint(config_service)
    stale = await pool.get(SOURCE_ENDPOINT)
    assert isinstance(stale, RecordingConnector)

    await config_service.update_endpoint(
        SOURCE_ENDPOINT, connector="unreachable", actor=ACTOR, now=LATER
    )

    with pytest.raises(EndpointSetupError):
        await pool.get(SOURCE_ENDPOINT)
    assert stale.close_calls == 1
    assert pool.cached_endpoints() == ()


async def test_close_closes_every_connector_even_when_one_raises(
    config_service: ConfigService, pool: StoreConnectorPool
) -> None:
    """Best-effort, the same contract as ``cli/wiring.py``'s ``ConnectorPool.close``."""
    await _create_source_endpoint(config_service, name="ok")
    await _create_source_endpoint(config_service, name="breaks", connector="uncloseable")
    good = await pool.get("ok")
    bad = await pool.get("breaks")
    assert isinstance(good, RecordingConnector)
    assert isinstance(bad, RecordingConnector)

    await pool.close()

    assert good.close_calls == 1
    assert bad.close_calls == 1
    assert pool.cached_endpoints() == ()


# ==========================================================================================
# Concurrency: one build, one setup, one connector
# ==========================================================================================


async def test_concurrent_gets_for_one_endpoint_build_exactly_once(
    config_service: ConfigService, pool: StoreConnectorPool, bound_credential: None
) -> None:
    """Documented behaviour: a per-endpoint lock spans the row read and the build, so the
    second caller waits and receives the *same* connector rather than racing. Building
    twice and discarding one would be safe but wasteful; handing out a half-``setup()``
    connector would be neither. ``RecordingConnector.setup`` yields to the loop twice, so
    an unguarded implementation really would interleave here."""
    await _create_target_endpoint(config_service)

    first, second, third = await asyncio.gather(
        pool.get(TARGET_ENDPOINT), pool.get(TARGET_ENDPOINT), pool.get(TARGET_ENDPOINT)
    )

    assert first is second is third
    assert isinstance(first, RecordingConnector)
    assert first.setup_calls == 1
    assert len(BUILT) == 1


async def test_concurrent_gets_for_different_endpoints_do_not_serialize_wrongly(
    config_service: ConfigService, pool: StoreConnectorPool
) -> None:
    """The lock is per endpoint: two different endpoints each get built exactly once, and
    neither is blocked out by the other."""
    await _create_source_endpoint(config_service, name="one")
    await _create_source_endpoint(config_service, name="two")

    first, second = await asyncio.gather(pool.get("one"), pool.get("two"))

    assert first is not second
    assert pool.cached_endpoints() == ("one", "two")
    assert len(BUILT) == 2


# ==========================================================================================
# Pair rows: one translation, shared with the scheduler
# ==========================================================================================


async def _create_pair_with_rules(
    config_service: ConfigService, *, patterns: tuple[str, ...] = ("sales.*", "finance.*")
) -> uuid.UUID:
    await _create_source_endpoint(config_service)
    await _create_target_endpoint(config_service)
    pair = await config_service.create_sync_pair(
        name="databricks-to-qlik",
        source=SOURCE_ENDPOINT,
        target=TARGET_ENDPOINT,
        target_space="Sales Space",
        entity_types=[EntityType.DATA_PRODUCT],
        cadence_seconds=600,
        activation_opt_in=True,
        enabled=True,
        actor=ACTOR,
        now=NOW,
    )
    for ordinal, pattern in enumerate(patterns):
        await config_service.create_selection_rule(
            pair_id=pair.id,
            scope=RuleScope.OBJECT,
            decision=SelectionDecision.INCLUDE,
            matcher_kind=MatcherKind.GLOB,
            pattern=pattern,
            ordinal=ordinal,
            actor=ACTOR,
            now=NOW,
        )
    return pair.id


async def test_sync_pair_config_agrees_with_the_scheduler_pair_source(
    config_service: ConfigService, bound_credential: None
) -> None:
    """The anti-drift test. ``scheduler.ConfigStorePairSource`` and this module both turn a
    stored pair into a ``SyncPairConfig``; they must produce the same object for the same
    rows, or the console's dry run and the scheduler's cycle are planning against two
    different pairs."""
    pair_id = await _create_pair_with_rules(config_service)

    snapshot = await ConfigStorePairSource(config_service).load()
    assert snapshot.failures == ()
    (plan,) = snapshot.plans

    row = await config_service.get_sync_pair(pair_id)
    assert row is not None
    rule_rows, _ = await selection_rows_for_pair(config_service, pair_id)

    assert sync_pair_config_for_row(row, rule_rows) == plan.pair


async def test_sync_pair_config_agrees_with_the_scheduler_when_a_pair_has_no_rules(
    config_service: ConfigService, bound_credential: None
) -> None:
    """The case the two implementations actually disagreed on before this module existed:
    with nothing to project, ``run_control.py`` used ``["*.*"]`` (fails open) and
    ``scheduler.py`` used the inert pattern (fails closed)."""
    pair_id = await _create_pair_with_rules(config_service, patterns=())

    snapshot = await ConfigStorePairSource(config_service).load()
    (plan,) = snapshot.plans
    row = await config_service.get_sync_pair(pair_id)
    assert row is not None

    assert sync_pair_config_for_row(row) == plan.pair
    assert plan.pair.catalog_schema_patterns == [INERT_CATALOG_SCHEMA_PATTERN]


def test_the_inert_pattern_is_one_string_under_two_names() -> None:
    """``scheduler.INERT_CATALOG_SCHEMA_PATTERN`` still exists as its own constant until
    the scheduler imports this module's. Until it does, this is what stops the two
    drifting."""
    assert INERT_CATALOG_SCHEMA_PATTERN == scheduler_module.INERT_CATALOG_SCHEMA_PATTERN


async def test_derived_patterns_keep_only_object_scope_include_globs_in_ordinal_order(
    config_service: ConfigService,
) -> None:
    """The projection is deliberately lossy -- it is a label, not a decision -- and this
    pins exactly which rules survive it."""
    pair_id = await _create_pair_with_rules(config_service, patterns=("zulu.*", "alpha.*"))
    await config_service.create_selection_rule(
        pair_id=pair_id,
        scope=RuleScope.OBJECT,
        decision=SelectionDecision.EXCLUDE,
        matcher_kind=MatcherKind.GLOB,
        pattern="zulu.secret",
        ordinal=2,
        actor=ACTOR,
        now=LATER,
    )
    rule_rows, _ = await selection_rows_for_pair(config_service, pair_id)

    assert derived_catalog_schema_patterns(rule_rows) == ["zulu.*", "alpha.*"]


async def test_selection_rule_set_for_pair_is_the_scheduler_rule_set(
    config_service: ConfigService, bound_credential: None
) -> None:
    """Same rows, same compiled rules, same order -- compared by the stored rules the
    compiled ones carry, since a ``SelectionRuleSet`` holds compiled matchers whose
    equality is not something to assert on (``scheduler.py`` says so itself)."""
    pair_id = await _create_pair_with_rules(config_service)

    snapshot = await ConfigStorePairSource(config_service).load()
    (plan,) = snapshot.plans
    mine = await selection_rule_set_for_pair(config_service, pair_id)

    for scope in RuleScope:
        assert [compiled.rule for compiled in mine.rules_for(scope)] == [
            compiled.rule for compiled in plan.selection_rules.rules_for(scope)
        ]


async def test_selection_rows_for_pair_reads_every_scope(
    config_service: ConfigService,
) -> None:
    pair_id = await _create_pair_with_rules(config_service)
    await config_service.create_selection_override(
        pair_id=pair_id,
        scope=RuleScope.OBJECT,
        object_id="sales.orders",
        decision=SelectionDecision.EXCLUDE,
        reason="pinned off by hand",
        actor=ACTOR,
        now=LATER,
    )

    rule_rows, override_rows = await selection_rows_for_pair(config_service, pair_id)

    assert sorted(row.pattern for row in rule_rows) == ["finance.*", "sales.*"]
    assert [row.object_id for row in override_rows] == ["sales.orders"]
