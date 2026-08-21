"""``configstore.bootstrap``: seed-on-first-start from an ``EngineConfig`` (T10.4, C1).

Exercises the real importer against the real migrated SQLite database the ``engine``
fixture (``tests/configstore/conftest.py``) provides, through a real ``ConfigService``
-- no mocked session, no hand-built rows. ``config_service_helpers`` supplies two fake
connectors (``databricks``, a read-only source with no secret fields; ``qlik``, the
sole write target with one required and one optional secret field) that stand in for
the real connector packages, exactly as ``test_service.py`` already does.

Sections, in order: the happy path and the DoD's three claims verbatim (comes up the
same as before, a second start imports nothing, a console edit survives a restart);
selection equivalence against ``SyncPairConfig.matches``; the "database wins" cases
(an edit, and a deliberate deletion, both survive a re-run); the secrets bridge (both
directions of the convention check); partial failure; and a dedicated section proving
this module cannot have bypassed ``ConfigService`` -- the dishonest cases a happy path
would never catch.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from config_service_helpers import ACTOR, LATER, NOW, make_registry
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from qlabs_catalog_sync.config import EndpointConfig, EngineConfig, SyncPairConfig
from qlabs_catalog_sync.configstore.bootstrap import (
    BOOTSTRAP_ACTOR,
    BootstrapPartialFailureError,
    BootstrapReport,
    bootstrap_from_environment,
)
from qlabs_catalog_sync.configstore.models import ConfigChangeRow, EndpointRow
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.configstore.types import ChangeEntityKind, EndpointRole, RuleScope
from qlabs_catalog_sync.selection.evaluator import evaluate
from qlabs_catalog_sync.selection.rules import SelectionCandidate, SelectionRuleSet
from qlabs_catalog_sync_sdk.models import EntityType

#: A distinctive stand-in for a live credential value -- its accidental presence
#: anywhere durable (a settings blob, a secret_ref, an audit row) can only mean a real
#: leak. Mirrors the sentinel convention ``test_service.py`` / ``test_secrets.py`` use.
_SECRET_SENTINEL = "sk-t10-4-do-not-leak-9c1f7b2a"

_DEFAULT_PATTERNS: tuple[str, ...] = ("prod.sales_*", "prod.finance", "staging.reporting")

#: A third fixed instant, after ``LATER``, standing in for "the process restarted".
RESTART: datetime = datetime(2026, 8, 22, tzinfo=UTC)


@pytest.fixture
def registry() -> object:
    return make_registry()


@pytest.fixture
def svc(engine: Engine, registry: object) -> ConfigService:
    return ConfigService(engine, registry)  # type: ignore[arg-type]


def _sample_engine_config(
    *,
    patterns: Sequence[str] = _DEFAULT_PATTERNS,
    databricks_secrets: dict[str, str] | None = None,
    qlik_secrets: dict[str, str] | None = None,
    databricks_connector: str = "databricks",
    extra_endpoints: dict[str, EndpointConfig] | None = None,
    pair_name: str = "databricks_prod_to_qlik_acme",
    cadence_seconds: int = 900,
    include_pair: bool = True,
) -> EngineConfig:
    """A realistic two-endpoint, one-pair config -- the same shape (and, by default,
    the same secrets-mapping convention) as ``tests/config/test_engine_config.py``'s
    own ``REALISTIC_CONFIG``, so this suite imports exactly what T2.3 already
    considers a normal deployment.
    """
    endpoints: dict[str, EndpointConfig] = {
        "databricks_prod": EndpointConfig(
            connector=databricks_connector,
            settings={"host": "https://dbx.example.com"},
            secrets=databricks_secrets if databricks_secrets is not None else {"token": "token"},
        ),
        "qlik_acme": EndpointConfig(
            connector="qlik",
            settings={"space_id": "personal"},
            secrets=qlik_secrets if qlik_secrets is not None else {"api_key": "api_key"},
        ),
    }
    if extra_endpoints:
        endpoints.update(extra_endpoints)
    pairs = (
        [
            SyncPairConfig(
                name=pair_name,
                source="databricks_prod",
                target="qlik_acme",
                catalog_schema_patterns=list(patterns),
                target_space="personal",
                entity_types=[EntityType.DATA_PRODUCT, EntityType.DATASET],
                cadence_seconds=cadence_seconds,
            )
        ]
        if include_pair
        else []
    )
    return EngineConfig(endpoints=endpoints, pairs=pairs)


def _change_rows(session: Session) -> list[ConfigChangeRow]:
    return list(session.scalars(select(ConfigChangeRow)).all())


# ----------------------------------------------------------------------------------
# The DoD, verbatim: "An environment-only deployment comes up with the same behavior
# as before; a second start imports nothing; an edit made through the service
# survives a restart unchanged."
# ----------------------------------------------------------------------------------


class TestDefinitionOfDone:
    async def test_environment_only_deployment_comes_up_the_same_as_before(
        self, svc: ConfigService, session: Session
    ) -> None:
        config = _sample_engine_config()

        report = await bootstrap_from_environment(svc, config, now=NOW)

        assert report.seeded is True
        assert report.endpoints_created == ("databricks_prod", "qlik_acme")
        assert report.pairs_created == ("databricks_prod_to_qlik_acme",)
        assert report.rules_created == len(_DEFAULT_PATTERNS)
        assert report.failures == ()
        assert report.secret_ref_skips == ()

        databricks = await svc.get_endpoint("databricks_prod")
        qlik = await svc.get_endpoint("qlik_acme")
        assert databricks is not None and qlik is not None
        # An environment-only deployment never had an "enabled" concept: what was
        # declared was active. The DoD's "same behavior as before" means the import
        # must not leave a freshly-seeded endpoint/pair inert behind the schema's
        # own enabled=False default.
        assert databricks.enabled is True
        assert databricks.role is EndpointRole.SOURCE
        assert databricks.connector == "databricks"
        assert databricks.settings == {"host": "https://dbx.example.com"}
        assert databricks.secret_ref == "env:databricks_prod"
        assert qlik.enabled is True
        assert qlik.role is EndpointRole.TARGET
        assert qlik.secret_ref == "env:qlik_acme"

        pairs = await svc.list_sync_pairs()
        assert len(pairs) == 1
        pair = pairs[0]
        assert pair.enabled is True
        assert pair.source == "databricks_prod"
        assert pair.target == "qlik_acme"
        assert pair.cadence_seconds == 900
        assert pair.jitter_seconds is None  # no per-pair override existed before RM-06
        assert pair.entity_types == [EntityType.DATA_PRODUCT, EntityType.DATASET]

        rules = await svc.list_selection_rules(pair.id, RuleScope.OBJECT)
        assert [rule.pattern for rule in rules] == list(_DEFAULT_PATTERNS)
        assert [rule.ordinal for rule in rules] == [0, 1, 2]

    async def test_second_start_imports_nothing(self, svc: ConfigService, session: Session) -> None:
        config = _sample_engine_config()
        first = await bootstrap_from_environment(svc, config, now=NOW)
        assert first.seeded is True
        generation_after_first = await svc.current_generation()
        changes_after_first = _change_rows(session)

        second = await bootstrap_from_environment(svc, config, now=LATER)

        assert second == BootstrapReport(seeded=False)
        assert await svc.current_generation() == generation_after_first
        assert _change_rows(session) == changes_after_first
        # Still exactly what the first run created -- nothing duplicated.
        assert len(await svc.list_endpoints()) == 2
        assert len(await svc.list_sync_pairs()) == 1

    async def test_an_edit_through_the_service_survives_a_restart_unchanged(
        self, svc: ConfigService
    ) -> None:
        config = _sample_engine_config()
        await bootstrap_from_environment(svc, config, now=NOW)
        pairs = await svc.list_sync_pairs()
        pair_id = pairs[0].id

        edited = await svc.update_sync_pair(pair_id, cadence_seconds=1234, actor=ACTOR, now=LATER)
        assert edited.cadence_seconds == 1234

        # "Restart": re-run the importer with the same environment config.
        report = await bootstrap_from_environment(svc, config, now=RESTART)

        assert report.seeded is False
        after_restart = await svc.get_sync_pair(pair_id)
        assert after_restart is not None
        assert after_restart.cadence_seconds == 1234  # not reverted to the env's 900


# ----------------------------------------------------------------------------------
# Selection equivalence (C3): the imported rule set decides identically to
# SyncPairConfig.matches over a table of inputs, including the tricky ones.
# ----------------------------------------------------------------------------------


class TestSelectionEquivalence:
    async def test_imported_rules_decide_identically_to_syncpairconfig_matches(
        self, svc: ConfigService
    ) -> None:
        patterns = ("prod.sales_*", "prod.finance", "staging.reporting")
        config = _sample_engine_config(patterns=patterns)
        pair_config = config.pairs[0]

        await bootstrap_from_environment(svc, config, now=NOW)
        pairs = await svc.list_sync_pairs()
        rule_rows = await svc.list_selection_rules(pairs[0].id, RuleScope.OBJECT)
        rule_set = SelectionRuleSet.from_rows(rule_rows)

        cases: list[tuple[str, str, bool]] = [
            ("prod", "sales_eu", True),  # matches prod.sales_*
            ("prod", "sales_us", True),
            ("prod", "sales", False),  # "sales_*" needs the literal underscore
            ("prod", "finance", True),  # exact second pattern
            ("prod", "hr", False),  # no pattern covers it
            ("staging", "reporting", True),  # exact third pattern
            ("staging", "reporting_v2", False),  # no wildcard on this pattern
            ("Prod", "sales_eu", False),  # case-sensitive catalog
            ("dev", "sales_eu", False),  # wrong catalog entirely
        ]
        for catalog, schema, expected in cases:
            # Sanity on the source of truth this is supposed to reproduce.
            assert pair_config.matches(catalog, schema) is expected, (catalog, schema)

            candidate = SelectionCandidate(
                scope=RuleScope.OBJECT,
                object_id=f"{catalog}.{schema}",
                qualified_name=f"{catalog}.{schema}",
            )
            result = evaluate(rule_set, candidate)
            assert result.included is expected, (
                f"{catalog}.{schema}: expected {expected}, got {result.included} "
                f"({result.explain()})"
            )

        # An empty rule set (no patterns declared) selects nothing -- D1 equivalence
        # holds at the boundary too, not just for a populated list.
        empty_candidate = SelectionCandidate(
            scope=RuleScope.OBJECT, object_id="anything.here", qualified_name="anything.here"
        )
        assert evaluate(SelectionRuleSet.build(), empty_candidate).included is False


# ----------------------------------------------------------------------------------
# "The database wins": a console edit, and a deliberate deletion, both survive a
# re-run of the importer untouched.
# ----------------------------------------------------------------------------------


class TestDatabaseWins:
    async def test_console_edit_of_cadence_is_not_reverted(self, svc: ConfigService) -> None:
        config = _sample_engine_config(cadence_seconds=900)
        await bootstrap_from_environment(svc, config, now=NOW)
        pair_id = (await svc.list_sync_pairs())[0].id

        await svc.update_sync_pair(pair_id, cadence_seconds=42, actor=ACTOR, now=LATER)
        await bootstrap_from_environment(svc, config, now=RESTART)

        pair = await svc.get_sync_pair(pair_id)
        assert pair is not None
        assert pair.cadence_seconds == 42

    async def test_console_deletion_of_a_selection_rule_is_not_resurrected(
        self, svc: ConfigService
    ) -> None:
        config = _sample_engine_config(patterns=_DEFAULT_PATTERNS)
        await bootstrap_from_environment(svc, config, now=NOW)
        pair_id = (await svc.list_sync_pairs())[0].id
        rules = await svc.list_selection_rules(pair_id, RuleScope.OBJECT)
        assert len(rules) == len(_DEFAULT_PATTERNS)
        deleted_rule_id = rules[0].id

        await svc.delete_selection_rule(deleted_rule_id, actor=ACTOR, now=LATER)
        await bootstrap_from_environment(svc, config, now=RESTART)

        remaining = await svc.list_selection_rules(pair_id, RuleScope.OBJECT)
        assert len(remaining) == len(_DEFAULT_PATTERNS) - 1
        assert deleted_rule_id not in {rule.id for rule in remaining}

    async def test_deliberately_deleted_endpoint_is_not_resurrected(
        self, svc: ConfigService
    ) -> None:
        # An endpoint the pair does not reference, so it can be deleted without
        # tripping the sync_pairs FK -- isolates "was this endpoint resurrected?"
        # from the pair/rule machinery exercised by the other tests in this class.
        config = _sample_engine_config(
            extra_endpoints={
                "databricks_unused": EndpointConfig(
                    connector="databricks", settings={"host": "https://unused.example.com"}
                )
            }
        )
        await bootstrap_from_environment(svc, config, now=NOW)
        assert await svc.get_endpoint("databricks_unused") is not None

        await svc.delete_endpoint("databricks_unused", actor=ACTOR, now=LATER)
        assert await svc.get_endpoint("databricks_unused") is None

        report = await bootstrap_from_environment(svc, config, now=RESTART)

        assert report.seeded is False
        assert await svc.get_endpoint("databricks_unused") is None
        # The endpoints the operator did *not* touch are still exactly as imported.
        assert await svc.get_endpoint("databricks_prod") is not None
        assert await svc.get_endpoint("qlik_acme") is not None


# ----------------------------------------------------------------------------------
# The secrets bridge: per-field EndpointConfig.secrets -> one EndpointRow.secret_ref.
# ----------------------------------------------------------------------------------


class TestSecretsBridge:
    async def test_convention_following_secrets_produce_a_working_secret_ref(
        self, svc: ConfigService
    ) -> None:
        config = _sample_engine_config(
            databricks_secrets={"token": "token"},
            qlik_secrets={"api_key": "api_key", "refresh_token": "refresh_token"},
        )

        report = await bootstrap_from_environment(svc, config, now=NOW)

        assert report.secret_ref_skips == ()
        databricks = await svc.get_endpoint("databricks_prod")
        qlik = await svc.get_endpoint("qlik_acme")
        assert databricks is not None and databricks.secret_ref == "env:databricks_prod"
        assert qlik is not None and qlik.secret_ref == "env:qlik_acme"

    async def test_non_convention_secrets_are_refused_not_guessed(self, svc: ConfigService) -> None:
        # A per-field key naming an unrelated environment variable -- structurally
        # legal on EndpointConfig.secrets (T2.3 places no constraint on the value),
        # but not representable as one secret_ref (configstore.secrets always derives
        # the key from the connector's own field name).
        config = _sample_engine_config(
            qlik_secrets={"api_key": "SOME_VAULT_PATH", "refresh_token": "refresh_token"}
        )

        with pytest.raises(BootstrapPartialFailureError) as exc_info:
            await bootstrap_from_environment(svc, config, now=NOW)

        report = exc_info.value.report
        assert len(report.secret_ref_skips) == 1
        skip = report.secret_ref_skips[0]
        assert skip.endpoint == "qlik_acme"
        assert skip.field == "api_key"
        assert skip.declared_key == "SOME_VAULT_PATH"
        assert skip.expected_key == "API_KEY"
        assert "does not match the single-reference convention" in skip.explain()

        # The endpoint still imports -- everything about it *except* the credential
        # reference is exactly what the environment declared -- but with no
        # secret_ref rather than one that would resolve to nothing or the wrong
        # value.
        assert "qlik_acme" in report.endpoints_created
        qlik = await svc.get_endpoint("qlik_acme")
        assert qlik is not None
        assert qlik.secret_ref is None
        assert qlik.settings == {"space_id": "personal"}

    async def test_endpoint_with_no_declared_secrets_gets_no_secret_ref_and_no_skip(
        self, svc: ConfigService
    ) -> None:
        config = _sample_engine_config(databricks_secrets={}, qlik_secrets={})

        report = await bootstrap_from_environment(svc, config, now=NOW)

        assert report.secret_ref_skips == ()
        databricks = await svc.get_endpoint("databricks_prod")
        assert databricks is not None
        assert databricks.secret_ref is None


# ----------------------------------------------------------------------------------
# Partial failure: best-effort persistence, loud reporting (see the module docstring
# for why this module chose best-effort over an all-or-nothing transaction).
# ----------------------------------------------------------------------------------


class TestPartialFailure:
    async def test_one_bad_endpoint_does_not_stop_the_rest_but_is_reported(
        self, svc: ConfigService
    ) -> None:
        config = _sample_engine_config(
            extra_endpoints={
                "broken": EndpointConfig(connector="nonexistent_connector", settings={})
            }
        )

        with pytest.raises(BootstrapPartialFailureError) as exc_info:
            await bootstrap_from_environment(svc, config, now=NOW)

        report = exc_info.value.report
        assert len(report.failures) == 1
        failure = report.failures[0]
        assert failure.entity_kind is ChangeEntityKind.ENDPOINT
        assert failure.key == "broken"
        assert "nonexistent_connector" in failure.reason
        assert "broken" in str(exc_info.value)

        # Everything importable was still imported.
        assert "databricks_prod" in report.endpoints_created
        assert "qlik_acme" in report.endpoints_created
        assert report.pairs_created == ("databricks_prod_to_qlik_acme",)
        assert await svc.get_endpoint("broken") is None
        assert await svc.get_endpoint("databricks_prod") is not None
        assert len(await svc.list_sync_pairs()) == 1

        # The next restart does not retry the broken endpoint: from the first
        # (partial) write onward, the database is authoritative, and closing the gap
        # is the console's job. See the module docstring's "partial failure" section.
        second = await bootstrap_from_environment(svc, config, now=LATER)
        assert second.seeded is False
        assert await svc.get_endpoint("broken") is None

    async def test_a_pair_whose_endpoint_failed_also_fails_and_is_reported(
        self, svc: ConfigService
    ) -> None:
        config = _sample_engine_config(databricks_connector="nonexistent_connector")

        with pytest.raises(BootstrapPartialFailureError) as exc_info:
            await bootstrap_from_environment(svc, config, now=NOW)

        report = exc_info.value.report
        kinds = {failure.entity_kind for failure in report.failures}
        assert ChangeEntityKind.ENDPOINT in kinds
        assert ChangeEntityKind.SYNC_PAIR in kinds
        assert report.pairs_created == ()
        assert await svc.list_sync_pairs() == []


# ----------------------------------------------------------------------------------
# The dishonest cases: assertions that fail if the import were subtly wrong in a way
# a happy path would miss.
# ----------------------------------------------------------------------------------


class TestWritesGoThroughTheService:
    async def test_every_created_row_has_a_matching_bootstrap_actor_audit_row(
        self, svc: ConfigService, session: Session
    ) -> None:
        """Fails if bootstrap ever wrote an EndpointRow/SyncPairRow/SelectionRuleRow
        directly instead of through ConfigService: a direct write would leave the
        audit log empty and the generation at 0 even though rows exist."""
        config = _sample_engine_config(patterns=_DEFAULT_PATTERNS)

        report = await bootstrap_from_environment(svc, config, now=NOW)

        expected_writes = (
            len(report.endpoints_created) + len(report.pairs_created) + report.rules_created
        )
        assert expected_writes == 2 + 1 + len(_DEFAULT_PATTERNS)

        changes = _change_rows(session)
        assert len(changes) == expected_writes
        assert all(change.action.value == "create" for change in changes)
        assert all(change.actor == BOOTSTRAP_ACTOR for change in changes)
        assert await svc.current_generation() == expected_writes

        endpoint_ids = {
            change.entity_id
            for change in changes
            if change.entity_kind is ChangeEntityKind.ENDPOINT
        }
        assert endpoint_ids == {"databricks_prod", "qlik_acme"}

        pair_ids = {
            change.entity_id
            for change in changes
            if change.entity_kind is ChangeEntityKind.SYNC_PAIR
        }
        pairs = await svc.list_sync_pairs()
        assert pair_ids == {str(pair.id) for pair in pairs}

        rule_change_count = sum(
            1 for change in changes if change.entity_kind is ChangeEntityKind.SELECTION_RULE
        )
        assert rule_change_count == len(_DEFAULT_PATTERNS)

    async def test_second_run_performs_zero_writes_even_though_values_are_identical(
        self, svc: ConfigService, session: Session
    ) -> None:
        """Fails if a second run wrote the same rows again (value-identical writes
        are still writes): the generation and the audit-row count must be byte-for-
        byte identical, not merely "the data still matches"."""
        config = _sample_engine_config()
        await bootstrap_from_environment(svc, config, now=NOW)
        generation_before = await svc.current_generation()
        change_ids_before = {change.id for change in _change_rows(session)}
        assert generation_before > 0

        report = await bootstrap_from_environment(svc, config, now=LATER)

        assert report.seeded is False
        assert await svc.current_generation() == generation_before
        change_ids_after = {change.id for change in _change_rows(session)}
        assert change_ids_after == change_ids_before  # not just same count -- same rows

    async def test_no_endpoint_ever_carries_a_credential_value(
        self,
        svc: ConfigService,
        session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fails if bootstrap ever resolved a secret (it must not -- it only ever
        derives a reference) and something resolved leaked into a persisted column."""
        monkeypatch.setenv("DATABRICKS_PROD__TOKEN", _SECRET_SENTINEL)
        monkeypatch.setenv("QLIK_ACME__API_KEY", _SECRET_SENTINEL)
        config = _sample_engine_config()

        await bootstrap_from_environment(svc, config, now=NOW)

        endpoints = session.scalars(select(EndpointRow)).all()
        assert endpoints  # sanity: something was actually imported
        for endpoint in endpoints:
            assert _SECRET_SENTINEL not in repr(endpoint.settings)
            assert endpoint.secret_ref is None or _SECRET_SENTINEL not in endpoint.secret_ref
            # The single-reference form never carries the resolved value at all --
            # only ever "env:<endpoint-key>".
            if endpoint.secret_ref is not None:
                assert endpoint.secret_ref == f"env:{endpoint.name}"

        for change in _change_rows(session):
            assert _SECRET_SENTINEL not in repr(change.old_value)
            assert _SECRET_SENTINEL not in repr(change.new_value)

    async def test_no_op_run_never_touches_pair_or_endpoint_timestamps(
        self, svc: ConfigService
    ) -> None:
        """Fails if the "no-op" path silently re-saved a row (e.g. an update call
        that happened to write identical values): updated_at would move even though
        nothing about the row changed."""
        config = _sample_engine_config()
        await bootstrap_from_environment(svc, config, now=NOW)
        pair_before = (await svc.list_sync_pairs())[0]
        endpoint_before = await svc.get_endpoint("databricks_prod")
        assert endpoint_before is not None

        await bootstrap_from_environment(svc, config, now=LATER)

        pair_after = (await svc.list_sync_pairs())[0]
        endpoint_after = await svc.get_endpoint("databricks_prod")
        assert endpoint_after is not None
        assert pair_after.updated_at == pair_before.updated_at == NOW
        assert endpoint_after.updated_at == endpoint_before.updated_at == NOW


class TestBootstrapReport:
    async def test_skipped_report_equals_its_own_default(self, svc: ConfigService) -> None:
        # Prime the store so the *next* call is the no-op path under test.
        config = _sample_engine_config()
        await bootstrap_from_environment(svc, config, now=NOW)

        report = await bootstrap_from_environment(svc, config, now=LATER)

        assert report.seeded is False
        assert report.ok is True
        assert report.endpoints_created == ()
        assert report.pairs_created == ()
        assert report.rules_created == 0
        assert report.failures == ()
        assert report.secret_ref_skips == ()

    async def test_ok_is_false_when_a_secret_ref_was_skipped_even_with_no_failures(
        self, svc: ConfigService
    ) -> None:
        config = _sample_engine_config(qlik_secrets={"api_key": "NOT_THE_CONVENTION"})
        with pytest.raises(BootstrapPartialFailureError) as exc_info:
            await bootstrap_from_environment(svc, config, now=NOW)
        assert exc_info.value.report.failures == ()
        assert exc_info.value.report.ok is False
