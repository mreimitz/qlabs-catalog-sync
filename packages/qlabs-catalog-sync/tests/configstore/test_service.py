"""``configstore.service.ConfigService``: CRUD, audit and generation in one transaction.

Exercises the real service against the real migrated SQLite database the ``engine``
fixture (``tests/configstore/conftest.py``) provides -- no mocked session, no fake
store. Two fake connectors (``config_service_helpers.FakeDatabricksConnector`` /
``FakeQlikConnector``) stand in for the real ``databricks``/``qlik`` connectors so
settings validation has a real ``ConfigModel`` (with real secret-typed fields) to
validate against, without depending on either real connector package.

Sections, in order: endpoints, sync pairs, selection rules (+ reorder), selection
overrides, then a dedicated "one transaction, genuinely" section proving the DoD's real
claim -- that a failure anywhere in a write, including after the audit row is appended,
leaves no trace at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from config_service_helpers import ACTOR, LATER, NOW, make_registry
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from qlabs_catalog_sync.configstore import audit, service
from qlabs_catalog_sync.configstore.models import (
    ConfigChangeRow,
    EndpointRow,
    SelectionOverrideRow,
    SelectionRuleRow,
    SyncPairRow,
)
from qlabs_catalog_sync.configstore.secrets import SecretRefFormatError
from qlabs_catalog_sync.configstore.service import (
    ConfigService,
    EndpointAlreadyExistsError,
    EndpointInUseError,
    EndpointNotFoundError,
    EndpointSettingsValidationError,
    InlineSecretRejectedError,
    SelectionOverrideAlreadyExistsError,
    SelectionOverrideNotFoundError,
    SelectionRuleNotFoundError,
    SelectionRuleOrdinalConflictError,
    SelectionRuleReorderMismatchError,
    SyncPairAlreadyExistsError,
    SyncPairEndpointError,
    SyncPairNotFoundError,
)
from qlabs_catalog_sync.configstore.types import (
    EndpointRole,
    MatcherKind,
    RuleScope,
    SelectionDecision,
)
from qlabs_catalog_sync.discovery import ConnectorNotRegisteredError
from qlabs_catalog_sync_sdk.models import EntityType

#: Stands in for a live credential -- distinctive enough that its accidental presence
#: anywhere (an audit row, a stored settings blob) can only mean a real leak. Mirrors
#: T10.2's own ``test_secrets.py`` sentinel convention.
_SECRET_SENTINEL = "sk-t10-3-do-not-leak-2f9b6c3a"


@pytest.fixture
def registry() -> object:
    return make_registry()


@pytest.fixture
def svc(engine: Engine, registry: object) -> Iterator[ConfigService]:
    built = ConfigService(engine, registry)  # type: ignore[arg-type]
    yield built


def _generation(engine: Engine) -> int:
    with Session(engine) as session:
        return audit.current_generation(session)


def _change_summaries(engine: Engine) -> list[tuple[str, str, str, str | None]]:
    """``[(entity_kind, entity_id, action, field), ...]`` ordered by ``changed_at``."""
    with Session(engine) as session:
        rows = session.scalars(select(ConfigChangeRow).order_by(ConfigChangeRow.changed_at)).all()
        return [(row.entity_kind.value, row.entity_id, row.action.value, row.field) for row in rows]


def _endpoint_exists(engine: Engine, name: str) -> bool:
    with Session(engine) as session:
        return session.get(EndpointRow, name) is not None


def _sync_pair_exists(engine: Engine, pair_id: uuid.UUID) -> bool:
    with Session(engine) as session:
        return session.get(SyncPairRow, pair_id) is not None


def _rule_ordinals(engine: Engine, pair_id: uuid.UUID, scope: RuleScope) -> list[tuple[str, int]]:
    with Session(engine) as session:
        rows = session.scalars(
            select(SelectionRuleRow)
            .where(SelectionRuleRow.pair_id == pair_id, SelectionRuleRow.scope == scope)
            .order_by(SelectionRuleRow.ordinal)
        ).all()
        return [(str(row.id), row.ordinal) for row in rows]


def _no_stored_value_contains(engine: Engine, needle: str) -> bool:
    """True when ``needle`` appears nowhere in any ``config_changes`` value or any
    ``endpoints.settings`` blob -- the check the inline-secret tests below hinge on."""
    with Session(engine) as session:
        haystacks: list[str] = []
        for change in session.scalars(select(ConfigChangeRow)).all():
            haystacks.append(repr(change.old_value))
            haystacks.append(repr(change.new_value))
        for endpoint in session.scalars(select(EndpointRow)).all():
            haystacks.append(repr(endpoint.settings))
            haystacks.append(repr(endpoint.secret_ref))
        return not any(needle in haystack for haystack in haystacks)


async def _seed_endpoints(svc: ConfigService) -> tuple[EndpointRow, EndpointRow]:
    source = await svc.create_endpoint(
        name="databricks_prod",
        connector="databricks",
        role=EndpointRole.SOURCE,
        settings={"host": "acme.cloud.databricks.com"},
        actor=ACTOR,
        now=NOW,
    )
    target = await svc.create_endpoint(
        name="qlik_acme",
        connector="qlik",
        role=EndpointRole.TARGET,
        settings={"space_id": "personal"},
        secret_ref="env:QLIK_ACME",
        actor=ACTOR,
        now=NOW,
    )
    return source, target


# ========================================================================================
# Endpoints
# ========================================================================================


async def test_create_get_list_endpoint_round_trip(svc: ConfigService, engine: Engine) -> None:
    created = await svc.create_endpoint(
        name="databricks_prod",
        connector="databricks",
        role=EndpointRole.SOURCE,
        settings={"host": "acme.cloud.databricks.com"},
        actor=ACTOR,
        now=NOW,
    )
    assert created.name == "databricks_prod"
    assert created.settings == {"host": "acme.cloud.databricks.com"}
    assert created.enabled is False

    fetched = await svc.get_endpoint("databricks_prod")
    assert fetched is not None
    assert fetched.connector == "databricks"

    listed = await svc.list_endpoints()
    assert [row.name for row in listed] == ["databricks_prod"]

    assert _generation(engine) == 1
    assert _change_summaries(engine) == [("endpoint", "databricks_prod", "create", None)]


async def test_get_endpoint_returns_none_when_absent(svc: ConfigService) -> None:
    assert await svc.get_endpoint("nope") is None


async def test_create_endpoint_with_secret_field_absent_from_settings_succeeds(
    svc: ConfigService,
) -> None:
    """The core "validate without demanding secrets" case: ``FakeQlikConfig.api_key``
    is a *required* ``SecretStr`` field, deliberately never supplied in ``settings`` --
    it is ``secret_ref``'s job, resolved later, at connector setup, not here."""
    row = await svc.create_endpoint(
        name="qlik_acme",
        connector="qlik",
        role=EndpointRole.TARGET,
        settings={"space_id": "acme"},
        secret_ref="env:QLIK_ACME",
        actor=ACTOR,
        now=NOW,
    )
    assert row.settings == {"space_id": "acme"}


async def test_create_endpoint_unknown_connector_names_available_connectors(
    svc: ConfigService, engine: Engine
) -> None:
    with pytest.raises(ConnectorNotRegisteredError) as excinfo:
        await svc.create_endpoint(
            name="mystery",
            connector="does-not-exist",
            role=EndpointRole.SOURCE,
            actor=ACTOR,
            now=NOW,
        )
    message = str(excinfo.value)
    assert "databricks" in message
    assert "qlik" in message
    assert _generation(engine) == 0
    assert not _endpoint_exists(engine, "mystery")


async def test_create_endpoint_rejects_malformed_secret_ref(
    svc: ConfigService, engine: Engine
) -> None:
    with pytest.raises(SecretRefFormatError):
        await svc.create_endpoint(
            name="qlik_acme",
            connector="qlik",
            role=EndpointRole.TARGET,
            settings={"space_id": "acme"},
            secret_ref="not-a-valid-ref",
            actor=ACTOR,
            now=NOW,
        )
    assert _generation(engine) == 0
    assert not _endpoint_exists(engine, "qlik_acme")


async def test_create_endpoint_rejects_wrong_type_in_settings(
    svc: ConfigService, engine: Engine
) -> None:
    with pytest.raises(EndpointSettingsValidationError) as excinfo:
        await svc.create_endpoint(
            name="databricks_prod",
            connector="databricks",
            role=EndpointRole.SOURCE,
            settings={"host": 123},  # FakeDatabricksConfig.host is `str`
            actor=ACTOR,
            now=NOW,
        )
    assert "host" in str(excinfo.value)
    assert _generation(engine) == 0
    assert not _endpoint_exists(engine, "databricks_prod")


async def test_create_endpoint_rejects_unknown_settings_key(svc: ConfigService) -> None:
    with pytest.raises(EndpointSettingsValidationError):
        await svc.create_endpoint(
            name="databricks_prod",
            connector="databricks",
            role=EndpointRole.SOURCE,
            settings={"host": "acme.cloud.databricks.com", "bogus_field": "x"},
            actor=ACTOR,
            now=NOW,
        )


async def test_create_endpoint_rejects_inline_secret_in_settings(
    svc: ConfigService, engine: Engine
) -> None:
    """The dishonest case a happy path would miss entirely: a plaintext credential
    smuggled into ``settings`` under the connector's own secret field name must be
    rejected before it ever reaches a row, an audit entry, or a generation bump."""
    with pytest.raises(InlineSecretRejectedError) as excinfo:
        await svc.create_endpoint(
            name="qlik_acme",
            connector="qlik",
            role=EndpointRole.TARGET,
            settings={"space_id": "acme", "api_key": _SECRET_SENTINEL},
            actor=ACTOR,
            now=NOW,
        )
    assert "api_key" in str(excinfo.value)
    assert _generation(engine) == 0
    assert not _endpoint_exists(engine, "qlik_acme")
    assert _no_stored_value_contains(engine, _SECRET_SENTINEL)


async def test_update_endpoint_partial_update_diffs_and_bumps_generation_once(
    svc: ConfigService, engine: Engine
) -> None:
    await svc.create_endpoint(
        name="databricks_prod",
        connector="databricks",
        role=EndpointRole.SOURCE,
        settings={"host": "acme.cloud.databricks.com"},
        actor=ACTOR,
        now=NOW,
    )
    updated = await svc.update_endpoint(
        "databricks_prod", enabled=True, actor=ACTOR, now=LATER
    )
    assert updated.enabled is True
    assert _generation(engine) == 2
    assert _change_summaries(engine) == [
        ("endpoint", "databricks_prod", "create", None),
        ("endpoint", "databricks_prod", "update", "enabled"),
    ]


async def test_update_endpoint_no_op_does_not_bump_generation(
    svc: ConfigService, engine: Engine
) -> None:
    await svc.create_endpoint(
        name="databricks_prod",
        connector="databricks",
        role=EndpointRole.SOURCE,
        settings={"host": "acme.cloud.databricks.com"},
        actor=ACTOR,
        now=NOW,
    )
    await svc.update_endpoint(
        "databricks_prod",
        settings={"host": "acme.cloud.databricks.com"},  # identical value
        actor=ACTOR,
        now=LATER,
    )
    assert _generation(engine) == 1  # only the create bumped it
    assert len(_change_summaries(engine)) == 1


async def test_update_endpoint_can_explicitly_clear_secret_ref(
    svc: ConfigService, engine: Engine
) -> None:
    await svc.create_endpoint(
        name="qlik_acme",
        connector="qlik",
        role=EndpointRole.TARGET,
        settings={"space_id": "acme"},
        secret_ref="env:QLIK_ACME",
        actor=ACTOR,
        now=NOW,
    )
    updated = await svc.update_endpoint(
        "qlik_acme", secret_ref=None, actor=ACTOR, now=LATER
    )
    assert updated.secret_ref is None
    assert _change_summaries(engine)[-1] == ("endpoint", "qlik_acme", "update", "secret_ref")


async def test_update_endpoint_rejects_inline_secret(svc: ConfigService, engine: Engine) -> None:
    await svc.create_endpoint(
        name="qlik_acme",
        connector="qlik",
        role=EndpointRole.TARGET,
        settings={"space_id": "acme"},
        secret_ref="env:QLIK_ACME",
        actor=ACTOR,
        now=NOW,
    )
    with pytest.raises(InlineSecretRejectedError):
        await svc.update_endpoint(
            "qlik_acme",
            settings={"space_id": "acme", "api_key": _SECRET_SENTINEL},
            actor=ACTOR,
            now=LATER,
        )
    assert _generation(engine) == 1  # the failed update did not bump it further
    assert _no_stored_value_contains(engine, _SECRET_SENTINEL)


async def test_update_endpoint_not_found_raises(svc: ConfigService) -> None:
    with pytest.raises(EndpointNotFoundError):
        await svc.update_endpoint("nope", enabled=True, actor=ACTOR, now=NOW)


async def test_create_endpoint_duplicate_name_raises(svc: ConfigService, engine: Engine) -> None:
    await svc.create_endpoint(
        name="qlik_acme",
        connector="qlik",
        role=EndpointRole.TARGET,
        settings={"space_id": "acme"},
        actor=ACTOR,
        now=NOW,
    )
    with pytest.raises(EndpointAlreadyExistsError):
        await svc.create_endpoint(
            name="qlik_acme",
            connector="qlik",
            role=EndpointRole.TARGET,
            settings={"space_id": "other"},
            actor=ACTOR,
            now=LATER,
        )
    assert _generation(engine) == 1


async def test_delete_endpoint_removes_row_and_audits(svc: ConfigService, engine: Engine) -> None:
    await svc.create_endpoint(
        name="databricks_prod",
        connector="databricks",
        role=EndpointRole.SOURCE,
        settings={"host": "acme.cloud.databricks.com"},
        actor=ACTOR,
        now=NOW,
    )
    await svc.delete_endpoint("databricks_prod", actor=ACTOR, now=LATER)

    assert await svc.get_endpoint("databricks_prod") is None
    assert _generation(engine) == 2
    assert _change_summaries(engine)[-1] == ("endpoint", "databricks_prod", "delete", None)


async def test_delete_endpoint_not_found_raises(svc: ConfigService) -> None:
    with pytest.raises(EndpointNotFoundError):
        await svc.delete_endpoint("nope", actor=ACTOR, now=NOW)


async def test_delete_endpoint_blocked_by_sync_pair_raises_typed_error(
    svc: ConfigService, engine: Engine
) -> None:
    await _seed_endpoints(svc)
    await svc.create_sync_pair(
        name="databricks-to-qlik",
        source="databricks_prod",
        target="qlik_acme",
        target_space="personal",
        entity_types=[EntityType.DATA_PRODUCT],
        actor=ACTOR,
        now=NOW,
    )
    generation_before = _generation(engine)

    with pytest.raises(EndpointInUseError) as excinfo:
        await svc.delete_endpoint("qlik_acme", actor=ACTOR, now=LATER)
    assert "databricks-to-qlik" in str(excinfo.value)
    assert excinfo.value.pairs == ("databricks-to-qlik",)

    assert _endpoint_exists(engine, "qlik_acme")  # not deleted
    assert _generation(engine) == generation_before  # the blocked delete did not write


# ========================================================================================
# Sync pairs
# ========================================================================================


async def test_create_sync_pair_round_trip(svc: ConfigService, engine: Engine) -> None:
    await _seed_endpoints(svc)
    row = await svc.create_sync_pair(
        name="databricks-to-qlik",
        source="databricks_prod",
        target="qlik_acme",
        target_space="personal",
        entity_types=[EntityType.DATA_PRODUCT, EntityType.DATASET],
        cadence_seconds=1800,
        activation_opt_in=True,
        actor=ACTOR,
        now=NOW,
    )
    assert row.entity_types == [EntityType.DATA_PRODUCT, EntityType.DATASET]
    assert row.cadence_seconds == 1800
    assert row.activation_opt_in is True

    fetched = await svc.get_sync_pair(row.id)
    assert fetched is not None
    assert fetched.name == "databricks-to-qlik"

    listed = await svc.list_sync_pairs()
    assert [pair.name for pair in listed] == ["databricks-to-qlik"]
    assert _change_summaries(engine)[-1] == (
        "sync_pair",
        str(row.id),
        "create",
        None,
    )


async def test_create_sync_pair_missing_source_endpoint_raises(
    svc: ConfigService, engine: Engine
) -> None:
    await svc.create_endpoint(
        name="qlik_acme",
        connector="qlik",
        role=EndpointRole.TARGET,
        settings={"space_id": "acme"},
        actor=ACTOR,
        now=NOW,
    )
    with pytest.raises(SyncPairEndpointError, match="source"):
        await svc.create_sync_pair(
            name="pair-1",
            source="does-not-exist",
            target="qlik_acme",
            target_space="personal",
            actor=ACTOR,
            now=NOW,
        )
    assert _generation(engine) == 1  # only the endpoint create


async def test_create_sync_pair_missing_target_endpoint_raises(svc: ConfigService) -> None:
    await svc.create_endpoint(
        name="databricks_prod",
        connector="databricks",
        role=EndpointRole.SOURCE,
        settings={"host": "acme.cloud.databricks.com"},
        actor=ACTOR,
        now=NOW,
    )
    with pytest.raises(SyncPairEndpointError, match="target"):
        await svc.create_sync_pair(
            name="pair-1",
            source="databricks_prod",
            target="does-not-exist",
            target_space="personal",
            actor=ACTOR,
            now=NOW,
        )


async def test_create_sync_pair_rejects_qlik_as_source(svc: ConfigService, engine: Engine) -> None:
    """The v1 upstream-only direction guardrail, enforced at write time -- see the
    module docstring's rationale (an operator should not discover a backwards pair
    only when the engine next restarts)."""
    await svc.create_endpoint(
        name="qlik_a",
        connector="qlik",
        role=EndpointRole.SOURCE,
        settings={"space_id": "a"},
        actor=ACTOR,
        now=NOW,
    )
    await svc.create_endpoint(
        name="qlik_b",
        connector="qlik",
        role=EndpointRole.TARGET,
        settings={"space_id": "b"},
        actor=ACTOR,
        now=NOW,
    )
    with pytest.raises(SyncPairEndpointError, match="never be a sync source"):
        await svc.create_sync_pair(
            name="backwards-pair",
            source="qlik_a",
            target="qlik_b",
            target_space="b",
            actor=ACTOR,
            now=NOW,
        )
    assert _generation(engine) == 2  # only the two endpoint creates
    assert not _sync_pair_exists_by_name(engine, "backwards-pair")


def _sync_pair_exists_by_name(engine: Engine, name: str) -> bool:
    with Session(engine) as session:
        return (
            session.execute(select(SyncPairRow.id).where(SyncPairRow.name == name)).first()
            is not None
        )


async def test_create_sync_pair_rejects_non_qlik_target(svc: ConfigService) -> None:
    await svc.create_endpoint(
        name="databricks_prod",
        connector="databricks",
        role=EndpointRole.SOURCE,
        settings={"host": "acme.cloud.databricks.com"},
        actor=ACTOR,
        now=NOW,
    )
    await svc.create_endpoint(
        name="databricks_other",
        connector="databricks",
        role=EndpointRole.TARGET,
        settings={"host": "other.cloud.databricks.com"},
        actor=ACTOR,
        now=NOW,
    )
    with pytest.raises(SyncPairEndpointError, match="only write target"):
        await svc.create_sync_pair(
            name="wrong-target",
            source="databricks_prod",
            target="databricks_other",
            target_space="personal",
            actor=ACTOR,
            now=NOW,
        )


async def test_create_sync_pair_duplicate_name_raises(svc: ConfigService) -> None:
    await _seed_endpoints(svc)
    await svc.create_sync_pair(
        name="pair-1",
        source="databricks_prod",
        target="qlik_acme",
        target_space="personal",
        actor=ACTOR,
        now=NOW,
    )
    with pytest.raises(SyncPairAlreadyExistsError):
        await svc.create_sync_pair(
            name="pair-1",
            source="databricks_prod",
            target="qlik_acme",
            target_space="personal",
            actor=ACTOR,
            now=LATER,
        )


async def test_update_sync_pair_diffs_multiple_fields_in_one_generation_bump(
    svc: ConfigService, engine: Engine
) -> None:
    await _seed_endpoints(svc)
    pair = await svc.create_sync_pair(
        name="pair-1",
        source="databricks_prod",
        target="qlik_acme",
        target_space="personal",
        entity_types=[EntityType.DATA_PRODUCT],
        cadence_seconds=900,
        actor=ACTOR,
        now=NOW,
    )
    generation_before = _generation(engine)

    updated = await svc.update_sync_pair(
        pair.id,
        cadence_seconds=1800,
        entity_types=[EntityType.DATA_PRODUCT, EntityType.DATASET],
        actor=ACTOR,
        now=LATER,
    )
    assert updated.cadence_seconds == 1800
    assert updated.entity_types == [EntityType.DATA_PRODUCT, EntityType.DATASET]
    assert _generation(engine) == generation_before + 1  # one bump for both fields

    fields_changed = {
        summary[3] for summary in _change_summaries(engine) if summary[2] == "update"
    }
    assert fields_changed == {"cadence_seconds", "entity_types"}


async def test_update_sync_pair_rechecks_direction_when_source_changes(
    svc: ConfigService,
) -> None:
    await _seed_endpoints(svc)
    await svc.create_endpoint(
        name="qlik_other",
        connector="qlik",
        role=EndpointRole.SOURCE,
        settings={"space_id": "other"},
        actor=ACTOR,
        now=NOW,
    )
    pair = await svc.create_sync_pair(
        name="pair-1",
        source="databricks_prod",
        target="qlik_acme",
        target_space="personal",
        actor=ACTOR,
        now=NOW,
    )
    with pytest.raises(SyncPairEndpointError, match="never be a sync source"):
        await svc.update_sync_pair(pair.id, source="qlik_other", actor=ACTOR, now=LATER)


async def test_update_sync_pair_no_op_does_not_bump_generation(
    svc: ConfigService, engine: Engine
) -> None:
    await _seed_endpoints(svc)
    pair = await svc.create_sync_pair(
        name="pair-1",
        source="databricks_prod",
        target="qlik_acme",
        target_space="personal",
        cadence_seconds=900,
        actor=ACTOR,
        now=NOW,
    )
    generation_before = _generation(engine)
    await svc.update_sync_pair(pair.id, cadence_seconds=900, actor=ACTOR, now=LATER)
    assert _generation(engine) == generation_before


async def test_update_sync_pair_not_found_raises(svc: ConfigService) -> None:
    with pytest.raises(SyncPairNotFoundError):
        await svc.update_sync_pair(uuid.uuid4(), enabled=True, actor=ACTOR, now=NOW)


async def test_delete_sync_pair_cascades_rules_and_overrides_with_one_audit_row(
    svc: ConfigService, engine: Engine
) -> None:
    await _seed_endpoints(svc)
    pair = await svc.create_sync_pair(
        name="pair-1",
        source="databricks_prod",
        target="qlik_acme",
        target_space="personal",
        actor=ACTOR,
        now=NOW,
    )
    await svc.create_selection_rule(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        decision=SelectionDecision.INCLUDE,
        matcher_kind=MatcherKind.GLOB,
        pattern="sales.*",
        actor=ACTOR,
        now=NOW,
    )
    await svc.create_selection_override(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        object_id="analytics.prod_staging",
        decision=SelectionDecision.INCLUDE,
        actor=ACTOR,
        now=NOW,
    )
    generation_before = _generation(engine)

    await svc.delete_sync_pair(pair.id, actor=ACTOR, now=LATER)

    assert await svc.get_sync_pair(pair.id) is None
    with Session(engine) as session:
        assert session.scalars(
            select(SelectionRuleRow).where(SelectionRuleRow.pair_id == pair.id)
        ).all() == []
        assert session.scalars(
            select(SelectionOverrideRow).where(SelectionOverrideRow.pair_id == pair.id)
        ).all() == []

    assert _generation(engine) == generation_before + 1  # exactly one bump
    assert _change_summaries(engine)[-1] == ("sync_pair", str(pair.id), "delete", None)


async def test_delete_sync_pair_not_found_raises(svc: ConfigService) -> None:
    with pytest.raises(SyncPairNotFoundError):
        await svc.delete_sync_pair(uuid.uuid4(), actor=ACTOR, now=NOW)


# ========================================================================================
# Selection rules (+ reorder)
# ========================================================================================


async def _make_pair(svc: ConfigService) -> SyncPairRow:
    await _seed_endpoints(svc)
    return await svc.create_sync_pair(
        name="pair-1",
        source="databricks_prod",
        target="qlik_acme",
        target_space="personal",
        actor=ACTOR,
        now=NOW,
    )


async def test_create_selection_rule_defaults_ordinal_to_append(svc: ConfigService) -> None:
    pair = await _make_pair(svc)
    first = await svc.create_selection_rule(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        decision=SelectionDecision.INCLUDE,
        matcher_kind=MatcherKind.GLOB,
        pattern="a.*",
        actor=ACTOR,
        now=NOW,
    )
    second = await svc.create_selection_rule(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        decision=SelectionDecision.EXCLUDE,
        matcher_kind=MatcherKind.GLOB,
        pattern="b.*",
        actor=ACTOR,
        now=NOW,
    )
    assert first.ordinal == 0
    assert second.ordinal == 1

    rules = await svc.list_selection_rules(pair.id, RuleScope.OBJECT)
    assert [rule.pattern for rule in rules] == ["a.*", "b.*"]


async def test_create_selection_rule_ordinal_collision_raises_typed_error(
    svc: ConfigService, engine: Engine
) -> None:
    pair = await _make_pair(svc)
    await svc.create_selection_rule(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        decision=SelectionDecision.INCLUDE,
        matcher_kind=MatcherKind.GLOB,
        pattern="a.*",
        ordinal=0,
        actor=ACTOR,
        now=NOW,
    )
    generation_before = _generation(engine)

    with pytest.raises(SelectionRuleOrdinalConflictError):
        await svc.create_selection_rule(
            pair_id=pair.id,
            scope=RuleScope.OBJECT,
            decision=SelectionDecision.EXCLUDE,
            matcher_kind=MatcherKind.GLOB,
            pattern="b.*",
            ordinal=0,
            actor=ACTOR,
            now=LATER,
        )
    assert _generation(engine) == generation_before  # the failed create did not write
    rules = await svc.list_selection_rules(pair.id, RuleScope.OBJECT)
    assert len(rules) == 1


async def test_create_selection_rule_missing_pair_raises(svc: ConfigService) -> None:
    with pytest.raises(SyncPairNotFoundError):
        await svc.create_selection_rule(
            pair_id=uuid.uuid4(),
            scope=RuleScope.OBJECT,
            decision=SelectionDecision.INCLUDE,
            matcher_kind=MatcherKind.GLOB,
            pattern="a.*",
            actor=ACTOR,
            now=NOW,
        )


async def test_update_selection_rule_changes_decision_and_pattern(
    svc: ConfigService, engine: Engine
) -> None:
    pair = await _make_pair(svc)
    rule = await svc.create_selection_rule(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        decision=SelectionDecision.INCLUDE,
        matcher_kind=MatcherKind.GLOB,
        pattern="a.*",
        actor=ACTOR,
        now=NOW,
    )
    updated = await svc.update_selection_rule(
        rule.id, decision=SelectionDecision.EXCLUDE, pattern="a.staging", actor=ACTOR, now=LATER
    )
    assert updated.decision == SelectionDecision.EXCLUDE
    assert updated.pattern == "a.staging"
    assert updated.ordinal == 0  # unaffected by an update that never mentions ordinal


async def test_update_selection_rule_not_found_raises(svc: ConfigService) -> None:
    with pytest.raises(SelectionRuleNotFoundError):
        await svc.update_selection_rule(
            uuid.uuid4(), decision=SelectionDecision.EXCLUDE, actor=ACTOR, now=NOW
        )


async def test_delete_selection_rule_removes_and_audits(
    svc: ConfigService, engine: Engine
) -> None:
    pair = await _make_pair(svc)
    rule = await svc.create_selection_rule(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        decision=SelectionDecision.INCLUDE,
        matcher_kind=MatcherKind.GLOB,
        pattern="a.*",
        actor=ACTOR,
        now=NOW,
    )
    await svc.delete_selection_rule(rule.id, actor=ACTOR, now=LATER)
    assert await svc.get_selection_rule(rule.id) is None
    assert _change_summaries(engine)[-1] == ("selection_rule", str(rule.id), "delete", None)


async def test_delete_selection_rule_not_found_raises(svc: ConfigService) -> None:
    with pytest.raises(SelectionRuleNotFoundError):
        await svc.delete_selection_rule(uuid.uuid4(), actor=ACTOR, now=NOW)


async def test_reorder_moves_last_rule_to_first_and_stays_contiguous(
    svc: ConfigService, engine: Engine
) -> None:
    pair = await _make_pair(svc)
    rules = [
        await svc.create_selection_rule(
            pair_id=pair.id,
            scope=RuleScope.OBJECT,
            decision=SelectionDecision.INCLUDE,
            matcher_kind=MatcherKind.GLOB,
            pattern=pattern,
            actor=ACTOR,
            now=NOW,
        )
        for pattern in ("a.*", "b.*", "c.*", "d.*")
    ]
    generation_before = _generation(engine)

    new_order = [rules[3].id, rules[0].id, rules[1].id, rules[2].id]  # last -> first
    reordered = await svc.reorder_selection_rules(
        pair.id, RuleScope.OBJECT, new_order, actor=ACTOR, now=LATER
    )

    assert [rule.id for rule in reordered] == new_order
    assert [rule.ordinal for rule in reordered] == [0, 1, 2, 3]
    assert _rule_ordinals(engine, pair.id, RuleScope.OBJECT) == [
        (str(rule_id), ordinal) for rule_id, ordinal in zip(new_order, [0, 1, 2, 3], strict=True)
    ]
    assert _generation(engine) == generation_before + 1  # exactly one bump
    assert _change_summaries(engine)[-1] == (
        "selection_rule",
        f"{pair.id!s}:{RuleScope.OBJECT.value}",
        "update",
        "order:object",
    )


async def test_reorder_handles_a_non_contiguous_starting_ordinal_range(
    svc: ConfigService, engine: Engine
) -> None:
    """Stresses the two-phase offset computation for real: starting ordinals {0, 5}
    are not contiguous, so the temporary range must still avoid colliding with either
    the pre-move values or the final [0, 1) range."""
    pair = await _make_pair(svc)
    first = await svc.create_selection_rule(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        decision=SelectionDecision.INCLUDE,
        matcher_kind=MatcherKind.GLOB,
        pattern="a.*",
        ordinal=0,
        actor=ACTOR,
        now=NOW,
    )
    second = await svc.create_selection_rule(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        decision=SelectionDecision.INCLUDE,
        matcher_kind=MatcherKind.GLOB,
        pattern="b.*",
        ordinal=5,
        actor=ACTOR,
        now=NOW,
    )
    reordered = await svc.reorder_selection_rules(
        pair.id, RuleScope.OBJECT, [second.id, first.id], actor=ACTOR, now=LATER
    )
    assert [rule.ordinal for rule in reordered] == [0, 1]
    assert [rule.id for rule in reordered] == [second.id, first.id]


async def test_reorder_mismatched_ids_raises_and_leaves_order_intact(
    svc: ConfigService, engine: Engine
) -> None:
    pair = await _make_pair(svc)
    rules = [
        await svc.create_selection_rule(
            pair_id=pair.id,
            scope=RuleScope.OBJECT,
            decision=SelectionDecision.INCLUDE,
            matcher_kind=MatcherKind.GLOB,
            pattern=pattern,
            actor=ACTOR,
            now=NOW,
        )
        for pattern in ("a.*", "b.*")
    ]
    original_order = _rule_ordinals(engine, pair.id, RuleScope.OBJECT)
    generation_before = _generation(engine)

    with pytest.raises(SelectionRuleReorderMismatchError):
        await svc.reorder_selection_rules(
            pair.id, RuleScope.OBJECT, [rules[0].id, uuid.uuid4()], actor=ACTOR, now=LATER
        )

    assert _rule_ordinals(engine, pair.id, RuleScope.OBJECT) == original_order
    assert _generation(engine) == generation_before


async def test_reorder_noop_when_already_in_requested_order(
    svc: ConfigService, engine: Engine
) -> None:
    pair = await _make_pair(svc)
    rules = [
        await svc.create_selection_rule(
            pair_id=pair.id,
            scope=RuleScope.OBJECT,
            decision=SelectionDecision.INCLUDE,
            matcher_kind=MatcherKind.GLOB,
            pattern=pattern,
            actor=ACTOR,
            now=NOW,
        )
        for pattern in ("a.*", "b.*")
    ]
    generation_before = _generation(engine)
    await svc.reorder_selection_rules(
        pair.id, RuleScope.OBJECT, [rules[0].id, rules[1].id], actor=ACTOR, now=LATER
    )
    assert _generation(engine) == generation_before


async def test_reorder_failure_after_audit_leaves_original_order_intact(
    svc: ConfigService, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The atomicity proof for reorder specifically: force a failure *after* the
    reorder's audit row would have been appended (both ordinal-rewrite passes already
    flushed to the open transaction) and confirm the rollback restores the exact
    original ordinals -- not just "some" order, the original one."""
    pair = await _make_pair(svc)
    rules = [
        await svc.create_selection_rule(
            pair_id=pair.id,
            scope=RuleScope.OBJECT,
            decision=SelectionDecision.INCLUDE,
            matcher_kind=MatcherKind.GLOB,
            pattern=pattern,
            actor=ACTOR,
            now=NOW,
        )
        for pattern in ("a.*", "b.*", "c.*")
    ]
    original_order = _rule_ordinals(engine, pair.id, RuleScope.OBJECT)
    generation_before = _generation(engine)
    changes_before = len(_change_summaries(engine))

    real_record_reorder = audit.record_reorder

    def _boom(*args: object, **kwargs: object) -> int:
        real_record_reorder(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("injected failure after the audit row was appended")

    monkeypatch.setattr(service.audit, "record_reorder", _boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        await svc.reorder_selection_rules(
            pair.id,
            RuleScope.OBJECT,
            [rules[2].id, rules[0].id, rules[1].id],
            actor=ACTOR,
            now=LATER,
        )

    assert _rule_ordinals(engine, pair.id, RuleScope.OBJECT) == original_order
    assert _generation(engine) == generation_before
    assert len(_change_summaries(engine)) == changes_before  # no new audit row


# ========================================================================================
# Selection overrides
# ========================================================================================


async def test_create_selection_override_round_trip(svc: ConfigService, engine: Engine) -> None:
    pair = await _make_pair(svc)
    row = await svc.create_selection_override(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        object_id="analytics.prod_staging",
        decision=SelectionDecision.INCLUDE,
        reason="keep despite the exclude rule",
        actor=ACTOR,
        now=NOW,
    )
    assert row.reason == "keep despite the exclude rule"

    fetched = await svc.get_selection_override(row.id)
    assert fetched is not None
    assert fetched.object_id == "analytics.prod_staging"

    listed = await svc.list_selection_overrides(pair.id, RuleScope.OBJECT)
    assert [override.object_id for override in listed] == ["analytics.prod_staging"]
    assert _change_summaries(engine)[-1] == ("selection_override", str(row.id), "create", None)


async def test_create_selection_override_duplicate_raises(svc: ConfigService) -> None:
    pair = await _make_pair(svc)
    await svc.create_selection_override(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        object_id="analytics.prod_staging",
        decision=SelectionDecision.INCLUDE,
        actor=ACTOR,
        now=NOW,
    )
    with pytest.raises(SelectionOverrideAlreadyExistsError):
        await svc.create_selection_override(
            pair_id=pair.id,
            scope=RuleScope.OBJECT,
            object_id="analytics.prod_staging",
            decision=SelectionDecision.EXCLUDE,
            actor=ACTOR,
            now=LATER,
        )


async def test_create_selection_override_missing_pair_raises(svc: ConfigService) -> None:
    with pytest.raises(SyncPairNotFoundError):
        await svc.create_selection_override(
            pair_id=uuid.uuid4(),
            scope=RuleScope.OBJECT,
            object_id="analytics.prod_staging",
            decision=SelectionDecision.INCLUDE,
            actor=ACTOR,
            now=NOW,
        )


async def test_update_selection_override_decision_and_clears_reason(
    svc: ConfigService,
) -> None:
    pair = await _make_pair(svc)
    override = await svc.create_selection_override(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        object_id="analytics.prod_staging",
        decision=SelectionDecision.INCLUDE,
        reason="temporary",
        actor=ACTOR,
        now=NOW,
    )
    updated = await svc.update_selection_override(
        override.id,
        decision=SelectionDecision.EXCLUDE,
        reason=None,
        actor=ACTOR,
        now=LATER,
    )
    assert updated.decision == SelectionDecision.EXCLUDE
    assert updated.reason is None


async def test_update_selection_override_not_found_raises(svc: ConfigService) -> None:
    with pytest.raises(SelectionOverrideNotFoundError):
        await svc.update_selection_override(
            uuid.uuid4(), decision=SelectionDecision.EXCLUDE, actor=ACTOR, now=NOW
        )


async def test_delete_selection_override_removes_and_audits(
    svc: ConfigService, engine: Engine
) -> None:
    pair = await _make_pair(svc)
    override = await svc.create_selection_override(
        pair_id=pair.id,
        scope=RuleScope.OBJECT,
        object_id="analytics.prod_staging",
        decision=SelectionDecision.INCLUDE,
        actor=ACTOR,
        now=NOW,
    )
    await svc.delete_selection_override(override.id, actor=ACTOR, now=LATER)
    assert await svc.get_selection_override(override.id) is None
    assert _change_summaries(engine)[-1] == (
        "selection_override",
        str(override.id),
        "delete",
        None,
    )


async def test_delete_selection_override_not_found_raises(svc: ConfigService) -> None:
    with pytest.raises(SelectionOverrideNotFoundError):
        await svc.delete_selection_override(uuid.uuid4(), actor=ACTOR, now=NOW)


# ========================================================================================
# One transaction, genuinely -- the DoD's real claim
# ========================================================================================


async def test_create_endpoint_failure_after_audit_append_leaves_no_trace(
    svc: ConfigService, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injects a failure *after* ``configstore.audit.record_create`` has really run
    (so the audit row and the generation bump are genuinely flushed into the open,
    uncommitted transaction) and confirms the whole thing rolls back: no endpoint row,
    no audit row, generation still 0. This is the test that fails if a future change
    ever committed the audit append and the generation bump in separate transactions."""
    real_record_create = audit.record_create

    def _boom(*args: object, **kwargs: object) -> int:
        real_record_create(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("injected failure after the audit row was appended")

    monkeypatch.setattr(service.audit, "record_create", _boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        await svc.create_endpoint(
            name="qlik_acme",
            connector="qlik",
            role=EndpointRole.TARGET,
            settings={"space_id": "acme"},
            actor=ACTOR,
            now=NOW,
        )

    assert not _endpoint_exists(engine, "qlik_acme")
    assert _generation(engine) == 0
    assert _change_summaries(engine) == []


async def test_update_endpoint_failure_after_audit_append_leaves_prior_state_intact(
    svc: ConfigService, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await svc.create_endpoint(
        name="databricks_prod",
        connector="databricks",
        role=EndpointRole.SOURCE,
        settings={"host": "acme.cloud.databricks.com"},
        actor=ACTOR,
        now=NOW,
    )
    generation_after_create = _generation(engine)

    real_record_update = audit.record_update

    def _boom(*args: object, **kwargs: object) -> int:
        real_record_update(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("injected failure after the audit row was appended")

    monkeypatch.setattr(service.audit, "record_update", _boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        await svc.update_endpoint("databricks_prod", enabled=True, actor=ACTOR, now=LATER)

    fetched = await svc.get_endpoint("databricks_prod")
    assert fetched is not None
    assert fetched.enabled is False  # the failed update never actually landed
    assert _generation(engine) == generation_after_create  # not bumped a second time
    assert len(_change_summaries(engine)) == 1  # only the original create


async def test_delete_sync_pair_failure_after_audit_append_leaves_pair_intact(
    svc: ConfigService, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair = await _make_pair(svc)
    generation_before = _generation(engine)

    real_record_delete = audit.record_delete

    def _boom(*args: object, **kwargs: object) -> int:
        real_record_delete(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("injected failure after the audit row was appended")

    monkeypatch.setattr(service.audit, "record_delete", _boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        await svc.delete_sync_pair(pair.id, actor=ACTOR, now=LATER)

    assert _sync_pair_exists(engine, pair.id)  # not actually deleted
    assert _generation(engine) == generation_before


async def test_generation_tracks_exactly_the_successful_writes(
    svc: ConfigService, engine: Engine
) -> None:
    """An end-to-end mix of successful and failed writes: the generation counter must
    equal the count of writes that actually succeeded, no more and no less."""
    await svc.create_endpoint(
        name="databricks_prod",
        connector="databricks",
        role=EndpointRole.SOURCE,
        settings={"host": "acme.cloud.databricks.com"},
        actor=ACTOR,
        now=NOW,
    )  # 1
    with pytest.raises(EndpointAlreadyExistsError):
        await svc.create_endpoint(
            name="databricks_prod",
            connector="databricks",
            role=EndpointRole.SOURCE,
            settings={"host": "other.cloud.databricks.com"},
            actor=ACTOR,
            now=NOW,
        )  # fails, no bump
    await svc.create_endpoint(
        name="qlik_acme",
        connector="qlik",
        role=EndpointRole.TARGET,
        settings={"space_id": "acme"},
        actor=ACTOR,
        now=NOW,
    )  # 2
    with pytest.raises(ConnectorNotRegisteredError):
        await svc.create_endpoint(
            name="mystery", connector="nope", role=EndpointRole.SOURCE, actor=ACTOR, now=NOW
        )  # fails, no bump
    pair = await svc.create_sync_pair(
        name="pair-1",
        source="databricks_prod",
        target="qlik_acme",
        target_space="acme",
        actor=ACTOR,
        now=NOW,
    )  # 3
    with pytest.raises(SyncPairNotFoundError):
        await svc.update_sync_pair(uuid.uuid4(), enabled=True, actor=ACTOR, now=NOW)  # no bump
    await svc.update_sync_pair(pair.id, enabled=True, actor=ACTOR, now=LATER)  # 4

    assert _generation(engine) == 4
