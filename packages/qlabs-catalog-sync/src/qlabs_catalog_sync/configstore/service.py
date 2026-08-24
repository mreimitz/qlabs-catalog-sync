"""The configuration service: CRUD, audit and generation in one transaction (T10.3).

WP10 / T10.3. :class:`ConfigService` is the only thing the future API routes (WP12) are
meant to call to change configuration -- it is the layer that turns "an operator wants
to register an endpoint / edit a pair / reorder a rule set" into a write that is
simultaneously validated, audited and generation-bumped, or not applied at all.

Design points, stated once here rather than repeated on every method:

* **One transaction, genuinely.** Every mutating method opens exactly one
  :class:`~sqlalchemy.orm.Session`/transaction via :meth:`ConfigService._unit_of_work`
  (mirrors :meth:`~qlabs_catalog_sync.state.store.StateStore.unit_of_work`, T2.2's own
  transactional idiom -- reused here rather than re-invented) and does not commit it
  until the whole method body -- validation, the row mutation, and the
  ``configstore.audit`` call -- has run without raising. Anything that raises inside
  that block, including something raised *after* the audit row was appended, rolls the
  whole session back: no orphan ``config_changes`` row, no generation bump left behind.
  See ``tests/configstore/test_service.py`` for the test that proves this by injecting a
  failure at exactly that point.
* **Settings are validated against the connector's own ``ConfigModel`` without ever
  requiring a secret.** See :func:`_validate_endpoint_settings` below for the mechanism
  and why it is safe (``BaseSettings.model_validate`` does not read the environment --
  only ``__init__``/``for_endpoint`` do).
* **Secrets never travel through ``settings``.** A ``settings`` value keyed by one of
  the connector's own secret-typed field names is rejected outright (
  :class:`InlineSecretRejectedError`) rather than silently accepted and later written
  into a ``config_changes`` row -- the only legitimate way to supply a credential is
  ``secret_ref`` (decision C2), which this module never resolves or reads.
* **Reordering rewrites ordinals in one pass, collision-free by construction.** See
  :meth:`ConfigService.reorder_selection_rules`.
* **``actor`` and ``now`` are required on every mutating method**, never defaulted
  inside this module -- ``config_changes.actor`` is non-nullable by schema design
  (``configstore/models.py``) precisely so every change is attributable, and the one
  administrator identity (decision C7) is the caller's concern, not this service's.
* **A blocked delete gets a typed error, not a raw ``IntegrityError``.** Deleting an
  endpoint a pair still references would hit ``sync_pairs``' ``ondelete="RESTRICT"`` FK;
  :meth:`ConfigService.delete_endpoint` checks first and raises
  :class:`EndpointInUseError` naming the blocking pairs.
* **The v1 upstream-only direction guardrail is enforced here too, not only at engine
  load time.** ``qlabs_catalog_sync.config.EngineConfig`` already rejects a
  backwards pair (Qlik as source, or a non-Qlik target) when the engine loads its
  environment-declared config (T2.3). A console write bypasses that load path entirely,
  so without a check here an operator could create a backwards pair through the API and
  only discover the problem when the engine next restarts -- worse than a validation
  error at write time. :func:`_check_pair_direction` mirrors
  ``EngineConfig._validate_pairs``'s direction check (reusing
  ``qlabs_catalog_sync.config.WRITE_CONNECTOR_NAME`` rather than re-declaring
  ``"qlik"``) and runs on every sync-pair create and on any update that touches
  ``source``/``target``.
* **``service.py`` never constructs a ``ConfigChangeRow`` directly.** Every audit row
  and every generation bump goes through ``configstore.audit`` -- see that module's
  docstring for why, and for the one-row-per-changed-field shape a multi-field update
  produces.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog
from pydantic import ValidationError
from pydantic_settings import BaseSettings
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from qlabs_catalog_sync.config import (
    WRITE_CONNECTOR_NAME,
    ManualEditPolicy,
    SecretBackend,
)
from qlabs_catalog_sync.configstore import audit
from qlabs_catalog_sync.configstore.audit import FieldChange
from qlabs_catalog_sync.configstore.crypto import SecretCipher
from qlabs_catalog_sync.configstore.models import (
    EndpointRow,
    EndpointSecretRow,
    SelectionOverrideRow,
    SelectionRuleRow,
    SyncPairRow,
)
from qlabs_catalog_sync.configstore.secrets import (
    DB_SCHEME,
    SecretRef,
    backend_for,
    default_secret_ref_for,
    secret_field_names,
)
from qlabs_catalog_sync.configstore.types import (
    ChangeEntityKind,
    EndpointRole,
    MatcherKind,
    RuleScope,
    SelectionDecision,
)
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.state.db import create_state_engine
from qlabs_catalog_sync_sdk.models import EntityType

__all__ = [
    "SECRET_AUDIT_FIELD_PREFIX",
    "UNSET",
    "ConfigService",
    "ConfigServiceError",
    "EndpointAlreadyExistsError",
    "EndpointInUseError",
    "EndpointNotFoundError",
    "EndpointSettingsValidationError",
    "EmptySecretValueError",
    "InlineSecretRejectedError",
    "SecretNotStoredError",
    "StoredSecretStatus",
    "UnknownSecretFieldError",
    "SelectionOverrideAlreadyExistsError",
    "SelectionOverrideNotFoundError",
    "SelectionRuleNotFoundError",
    "SelectionRuleOrdinalConflictError",
    "SelectionRuleReorderMismatchError",
    "SyncPairAlreadyExistsError",
    "SyncPairEndpointError",
    "SyncPairNotFoundError",
]

_logger = structlog.get_logger("qlabs.catalog_sync.configstore.service")

#: Prefix that marks a ``config_changes.field`` as being about a *stored credential*
#: rather than an ordinary endpoint column -- ``"secret:client_secret"``. A credential
#: change is a real configuration change and must be attributable like any other, but its
#: ``old_value``/``new_value`` can only ever be ``"set"``/``None``: the audit log is dumped,
#: exported and read by people, so a value must not be able to reach it even by accident.
SECRET_AUDIT_FIELD_PREFIX: Final[str] = "secret:"


# --------------------------------------------------------------------------------------
# The "not supplied" sentinel for partial updates
# --------------------------------------------------------------------------------------


class _UnsetType:
    """Sentinel marking an ``update_*`` keyword argument as "not supplied".

    Distinct from an explicit ``None``, which several nullable columns
    (``EndpointRow.secret_ref``, ``SyncPairRow.jitter_seconds``,
    ``SelectionOverrideRow.reason``) need to be able to express -- clearing a value on
    purpose is a different request from not mentioning that field at all, and a plain
    ``None`` default cannot tell the two apart.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


#: The one instance of :class:`_UnsetType` every ``update_*`` method's defaults use.
UNSET: Final = _UnsetType()


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class ConfigServiceError(Exception):
    """Base class for every typed error :class:`ConfigService` raises.

    Connector-lookup failures are a deliberate exception: those already have good typed
    errors (``qlabs_catalog_sync.discovery.ConnectorNotRegisteredError`` /
    ``ConnectorBrokenError``) and propagate unwrapped rather than being redescribed here.
    """


class EndpointNotFoundError(ConfigServiceError, LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no endpoint named {name!r} is configured")
        self.name = name


class EndpointAlreadyExistsError(ConfigServiceError):
    def __init__(self, name: str) -> None:
        super().__init__(f"an endpoint named {name!r} already exists")
        self.name = name


class EndpointInUseError(ConfigServiceError):
    """Deleting this endpoint would violate ``sync_pairs``' ``ondelete="RESTRICT"`` FK."""

    def __init__(self, name: str, *, pairs: Sequence[str]) -> None:
        joined = ", ".join(repr(pair) for pair in pairs)
        super().__init__(
            f"endpoint {name!r} is still used as source or target by sync pair(s) "
            f"{joined}; delete or repoint them first"
        )
        self.name = name
        self.pairs = tuple(pairs)


class InlineSecretRejectedError(ConfigServiceError):
    """``settings`` named one of the connector's own secret-typed fields (decision C2)."""

    def __init__(self, connector: str, fields: Sequence[str]) -> None:
        joined = ", ".join(repr(field) for field in sorted(fields))
        super().__init__(
            f"connector {connector!r} settings must not carry secret-typed field(s) "
            f"{joined} inline; bind a secret_ref instead (decision C2)"
        )
        self.connector = connector
        self.fields = tuple(sorted(fields))


class EndpointSettingsValidationError(ConfigServiceError):
    """``settings`` failed validation against the connector's own ``ConfigModel``."""

    def __init__(self, connector: str, detail: str) -> None:
        super().__init__(f"settings for connector {connector!r} are invalid: {detail}")
        self.connector = connector
        self.detail = detail


class UnknownSecretFieldError(ConfigServiceError):
    """A credential was offered for a field the connector does not declare as secret.

    Names the fields it *does* declare, because the operator is almost always one
    connector's vocabulary away from the right answer (``token`` versus ``client_secret``),
    and because storing a credential nothing will ever read is worse than refusing it.
    """

    def __init__(self, endpoint: str, field: str, declared: Sequence[str]) -> None:
        joined = ", ".join(repr(name) for name in declared) or "none"
        super().__init__(
            f"endpoint {endpoint!r} has no secret-typed field {field!r}; "
            f"its connector declares: {joined}"
        )
        self.endpoint = endpoint
        self.field = field
        self.declared = tuple(declared)


class EmptySecretValueError(ConfigServiceError):
    """An empty string was offered as a credential.

    Refused rather than stored: an empty credential fails at the tenant with a confusing
    authentication error, and "I meant to remove it" has its own explicit, separately
    audited operation.
    """

    def __init__(self, endpoint: str, field: str) -> None:
        super().__init__(
            f"the credential for endpoint {endpoint!r}, field {field!r} must not be empty; "
            f"to remove a stored credential, clear it instead"
        )
        self.endpoint = endpoint
        self.field = field


class SecretNotStoredError(ConfigServiceError):
    """Asked to clear a credential that was never stored."""

    def __init__(self, endpoint: str, field: str) -> None:
        super().__init__(f"endpoint {endpoint!r} has no stored credential for field {field!r}")
        self.endpoint = endpoint
        self.field = field


@dataclass(frozen=True, slots=True)
class StoredSecretStatus:
    """Whether one secret-typed field has a stored credential -- never which one.

    Same shape and same reasoning as
    :class:`~qlabs_catalog_sync.configstore.secrets.SecretResolveStatus`: there is no
    field here a credential could be assigned to, so this type cannot become a way for one
    to reach the console, whatever a future caller does with it.
    """

    field: str
    is_set: bool
    updated_at: datetime | None
    key_id: str | None


class SyncPairNotFoundError(ConfigServiceError, LookupError):
    def __init__(self, pair_id: uuid.UUID) -> None:
        super().__init__(f"no sync pair with id {pair_id!s} is configured")
        self.pair_id = pair_id


class SyncPairAlreadyExistsError(ConfigServiceError):
    def __init__(self, name: str) -> None:
        super().__init__(f"a sync pair named {name!r} already exists")
        self.name = name


class SyncPairEndpointError(ConfigServiceError):
    """A sync pair's ``source``/``target`` is missing or violates the v1 direction
    guardrail (upstream-only; Qlik is the sole write target)."""


class SelectionRuleNotFoundError(ConfigServiceError, LookupError):
    def __init__(self, rule_id: uuid.UUID) -> None:
        super().__init__(f"no selection rule with id {rule_id!s}")
        self.rule_id = rule_id


class SelectionRuleOrdinalConflictError(ConfigServiceError):
    def __init__(self, pair_id: uuid.UUID, scope: RuleScope, ordinal: int) -> None:
        super().__init__(
            f"a selection rule already occupies ordinal {ordinal} for pair {pair_id!s} "
            f"scope {scope.value!r}"
        )
        self.pair_id = pair_id
        self.scope = scope
        self.ordinal = ordinal


class SelectionRuleReorderMismatchError(ConfigServiceError):
    def __init__(
        self,
        pair_id: uuid.UUID,
        scope: RuleScope,
        *,
        expected: Sequence[str],
        given: Sequence[str],
    ) -> None:
        super().__init__(
            f"reorder for pair {pair_id!s} scope {scope.value!r} must name exactly the "
            f"existing rule ids, once each -- expected {sorted(expected)!r}, got "
            f"{list(given)!r}"
        )
        self.pair_id = pair_id
        self.scope = scope


class SelectionOverrideNotFoundError(ConfigServiceError, LookupError):
    def __init__(self, override_id: uuid.UUID) -> None:
        super().__init__(f"no selection override with id {override_id!s}")
        self.override_id = override_id


class SelectionOverrideAlreadyExistsError(ConfigServiceError):
    def __init__(self, pair_id: uuid.UUID, scope: RuleScope, object_id: str) -> None:
        super().__init__(
            f"pair {pair_id!s} already has a scope {scope.value!r} override for "
            f"object {object_id!r}"
        )
        self.pair_id = pair_id
        self.scope = scope
        self.object_id = object_id


# --------------------------------------------------------------------------------------
# Settings validation against the connector's own ConfigModel, without ever requiring a
# secret. See the module docstring's second bullet for the summary.
# --------------------------------------------------------------------------------------

#: A fixed, obviously-fake value used only to satisfy a *required* secret-typed field
#: during settings validation -- see ``_validate_endpoint_settings``. Never persisted,
#: never logged, never returned; it exists purely so ``ConfigModel.model_validate``
#: does not fail on a field that is not this call's business to supply.
_SETTINGS_VALIDATION_PLACEHOLDER: Final[str] = "t10-3-settings-validation-placeholder"


def _validate_endpoint_settings(
    connector: str, config_model: type[BaseSettings], settings: Mapping[str, object]
) -> None:
    """Validate ``settings`` against ``config_model``, without ever requiring a secret.

    Two passes:

    1. **Reject an inline secret outright.** If ``settings`` carries a key that names
       one of ``config_model``'s own secret-typed fields, that is a credential smuggled
       into the one column this schema promises can never hold one
       (``EndpointRow.settings`` -- see ``configstore/models.py``); the only legitimate
       way to supply a secret is ``secret_ref`` (decision C2). Raises
       :class:`InlineSecretRejectedError`, and never reaches the model at all.
    2. **Validate the rest for real**, through ``config_model.model_validate`` --
       confirmed empirically (see ``tests/configstore/test_service.py``) that
       ``BaseModel.model_validate`` does **not** read the process environment; only
       ``BaseSettings.__init__``/``ConnectorConfig.for_endpoint`` (T1.7) build the
       settings-source chain that would. So calling it here validates types,
       ``extra="forbid"``, and any custom validators the connector declares, without any
       risk of picking up a stray environment variable. Every *required* secret field is
       given :data:`_SETTINGS_VALIDATION_PLACEHOLDER` first -- a missing secret is
       ``secret_ref``'s problem, not ``settings``'s, so it must never fail this check.

    A **model-level** error (pydantic reports an empty ``loc`` for a cross-field
    validator) is skipped whenever the connector declares a secret field this pass did
    not supply -- i.e. an *optional* secret-typed field, which gets no placeholder. Such
    a rule cannot be judged from ``settings`` alone: the Databricks connector's
    "configure exactly one credential route -- ``client_id`` + ``client_secret``, or
    ``token``" is exactly this shape, and both of its credential fields are optional by
    type because only one route is ever set. Without the skip, *every* Databricks
    endpoint is unregisterable: naming a route is rejected as incomplete, naming none is
    rejected as absent, and supplying the missing half inline is rejected by the check
    above -- a three-way deadlock where the console can create no endpoint at all. What
    that rule is really asking about is credentials, which is ``secret_ref``'s question,
    answered by ``GET /endpoints/{name}/secret-resolve`` and by the healthcheck, both of
    which resolve the secret backend for real and report the same validator's message
    then. This pass keeps its own narrower job: settings are well-typed, carry no unknown
    keys, and satisfy every rule judgeable without a credential.

    Raises :class:`EndpointSettingsValidationError` naming exactly the non-secret
    problems pydantic found (an error attributed to a placeholder secret field, which
    should never happen given the placeholder is always well-typed, is reported in full
    rather than silently swallowed, so a connector with an unusually strict secret-field
    validator still gets an honest message instead of a confusing empty one).
    """
    secret_fields = secret_field_names(config_model)
    inline_secrets = secret_fields & settings.keys()
    if inline_secrets:
        raise InlineSecretRejectedError(connector, sorted(inline_secrets))

    placeholders: dict[str, object] = {
        name: _SETTINGS_VALIDATION_PLACEHOLDER
        for name, field_info in config_model.model_fields.items()
        if name in secret_fields and field_info.is_required()
    }
    #: Secret-typed fields this pass leaves unset -- the optional ones. Their presence is
    #: what makes a cross-field rule unjudgeable here; with none of them, every
    #: model-level error is about data ``settings`` really does own, and still reported.
    unsupplied_secret_fields = secret_fields - placeholders.keys()
    try:
        config_model.model_validate({**settings, **placeholders})
    except ValidationError as exc:
        errors = exc.errors()
        real_problems = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in errors
            if not (error["loc"] and error["loc"][0] in placeholders)
            and not (not error["loc"] and unsupplied_secret_fields)
        ]
        if not real_problems and any(
            not error["loc"] and unsupplied_secret_fields for error in errors
        ):
            # Every problem was a cross-field rule about credentials this pass cannot
            # supply. Not a settings error -- see the docstring.
            return
        detail = "; ".join(real_problems) if real_problems else str(exc)
        raise EndpointSettingsValidationError(connector, detail) from exc


# --------------------------------------------------------------------------------------
# JSON-safe, audit-log-safe snapshots. None of these can ever carry a credential: every
# field on every one of these rows is either plain settings/identity data or, for
# ``EndpointRow.secret_ref``, a reference rather than a value (C2).
# --------------------------------------------------------------------------------------


def _endpoint_snapshot(row: EndpointRow) -> dict[str, object]:
    return {
        "connector": row.connector,
        "role": row.role.value,
        "secret_ref": row.secret_ref,
        "settings": dict(row.settings),
        "enabled": row.enabled,
    }


def _sync_pair_snapshot(row: SyncPairRow) -> dict[str, object]:
    return {
        "name": row.name,
        "source": row.source,
        "target": row.target,
        "target_space": row.target_space,
        "entity_types": [entity_type.value for entity_type in row.entity_types],
        "cadence_seconds": row.cadence_seconds,
        "jitter_seconds": row.jitter_seconds,
        "manual_edit_policy": row.manual_edit_policy.model_dump(mode="json"),
        "activation_opt_in": row.activation_opt_in,
        "enabled": row.enabled,
    }


def _selection_rule_snapshot(row: SelectionRuleRow) -> dict[str, object]:
    return {
        "pair_id": str(row.pair_id),
        "ordinal": row.ordinal,
        "scope": row.scope.value,
        "decision": row.decision.value,
        "matcher_kind": row.matcher_kind.value,
        "pattern": row.pattern,
    }


def _selection_override_snapshot(row: SelectionOverrideRow) -> dict[str, object]:
    return {
        "pair_id": str(row.pair_id),
        "scope": row.scope.value,
        "object_id": row.object_id,
        "decision": row.decision.value,
        "reason": row.reason,
    }


# --------------------------------------------------------------------------------------
# Sync-pair direction guardrail (v1 upstream-only; mirrors config.py's
# EngineConfig._validate_pairs direction check -- see the module docstring)
# --------------------------------------------------------------------------------------


def _check_endpoint_exists(
    session: Session, name: str, *, pair_name: str, role_label: str
) -> EndpointRow:
    row = session.get(EndpointRow, name)
    if row is None:
        raise SyncPairEndpointError(
            f"sync pair {pair_name!r}: {role_label} endpoint {name!r} is not configured"
        )
    return row


def _check_pair_direction(pair_name: str, source: EndpointRow, target: EndpointRow) -> None:
    if source.connector == WRITE_CONNECTOR_NAME:
        raise SyncPairEndpointError(
            f"sync pair {pair_name!r}: source endpoint {source.name!r} uses the "
            f"{WRITE_CONNECTOR_NAME!r} connector, but v1 is upstream-only -- Qlik can "
            "never be a sync source"
        )
    if target.connector != WRITE_CONNECTOR_NAME:
        raise SyncPairEndpointError(
            f"sync pair {pair_name!r}: target endpoint {target.name!r} uses connector "
            f"{target.connector!r}, but the only write target in v1 is "
            f"{WRITE_CONNECTOR_NAME!r}"
        )


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------


class ConfigService:
    """CRUD, audit and generation for the console's configuration store, one
    transaction per write (see the module docstring)."""

    def __init__(
        self,
        engine: Engine,
        registry: ConnectorRegistry,
        *,
        cipher: SecretCipher | None = None,
    ) -> None:
        self._engine = engine
        self._session_factory: sessionmaker[Session] = sessionmaker(
            bind=engine, expire_on_commit=False, future=True
        )
        self._registry = registry
        #: Resolved on first use by :meth:`_require_cipher`, not here -- see that method
        #: for why a deployment with no master key must still be able to start. Passing one
        #: explicitly is for tests and for a future key source that is not the environment.
        self._cipher = cipher

    @classmethod
    def from_url(
        cls,
        url: str,
        registry: ConnectorRegistry,
        *,
        echo: bool = False,
        cipher: SecretCipher | None = None,
    ) -> ConfigService:
        """Build a service from a SQLAlchemy URL (mirrors ``StateStore.from_url``)."""
        return cls(create_state_engine(url, echo=echo), registry, cipher=cipher)

    @property
    def engine(self) -> Engine:
        return self._engine

    async def aclose(self) -> None:
        await asyncio.to_thread(self._engine.dispose)

    @asynccontextmanager
    async def _unit_of_work(self) -> AsyncIterator[Session]:
        """One session, one transaction -- committed only if the block does not raise.

        Mirrors ``StateStore.unit_of_work`` (T2.2). Every :class:`ConfigService`
        mutating method runs its validate -> mutate -> ``configstore.audit`` sequence
        inside exactly one of these, so any exception anywhere in that sequence -- a
        validation failure before a row is even touched, a constraint violation, or a
        failure deliberately raised after the audit row was appended -- rolls back
        everything: no orphan ``config_changes`` row, no generation bump.
        """
        session = self._session_factory()
        try:
            yield session
            await asyncio.to_thread(session.commit)
        except BaseException:
            await asyncio.to_thread(session.rollback)
            raise
        finally:
            await asyncio.to_thread(session.close)

    async def current_generation(self) -> int:
        """The current ``config_generation`` value, or ``0`` if nothing has been written."""

        def _read() -> int:
            with self._session_factory() as session:
                return audit.current_generation(session)

        return await asyncio.to_thread(_read)

    # ====================================================================================
    # Endpoints
    # ====================================================================================

    async def get_endpoint(self, name: str) -> EndpointRow | None:
        def _read() -> EndpointRow | None:
            with self._session_factory() as session:
                return session.get(EndpointRow, name)

        return await asyncio.to_thread(_read)

    async def list_endpoints(self) -> list[EndpointRow]:
        def _read() -> list[EndpointRow]:
            with self._session_factory() as session:
                stmt = select(EndpointRow).order_by(EndpointRow.name)
                return list(session.scalars(stmt).all())

        return await asyncio.to_thread(_read)

    async def create_endpoint(
        self,
        *,
        name: str,
        connector: str,
        role: EndpointRole,
        settings: Mapping[str, object] | None = None,
        secret_ref: str | None = None,
        bind_default_secret_ref: bool = True,
        enabled: bool = False,
        actor: str,
        now: datetime,
    ) -> EndpointRow:
        """Register a new endpoint instance (C6: naming an already-discovered
        connector, never fetching or executing one).

        Raises ``ConnectorNotRegisteredError``/``ConnectorBrokenError``
        (``qlabs_catalog_sync.discovery``) when ``connector`` is not installed or is
        broken; :class:`SecretRefFormatError`
        (``qlabs_catalog_sync.configstore.secrets``) when ``secret_ref`` does not parse;
        :class:`InlineSecretRejectedError` / :class:`EndpointSettingsValidationError`
        for a bad ``settings`` dict; :class:`EndpointAlreadyExistsError` when ``name``
        is already taken. None of these open a transaction -- see the module docstring.

        ``secret_ref`` defaults to :func:`default_secret_ref_for`'s ``"db:<name>"`` whenever
        the connector declares a secret-typed field at all: such an endpoint keeps its
        credentials in this configuration store unless someone deliberately says otherwise.
        That default is what lets registering an endpoint be
        "name it, fill in its settings, type its credentials" with no fourth question about
        where credentials come from -- a question that has one right answer for almost every
        deployment and that an operator adding a client should never have to think about.
        Passing an explicit ``"env:PREFIX"`` still works, unchanged, for a deployment whose
        secrets are injected as environment variables. ``bind_default_secret_ref=False``
        suppresses the default entirely, for the one caller that means *no* reference rather
        than "the usual one": ``configstore.bootstrap``, importing an environment-declared
        config whose per-field secret keys do not map onto a single reference, records that
        as a skip and imports the endpoint with nothing bound rather than inventing a
        binding the operator did not ask for.
        """
        connector_cls = self._registry.get_connector(connector)
        if secret_ref is None:
            # Only when the connector actually wants a credential. An endpoint whose
            # connector declares no secret-typed field is fully configured by `settings`
            # alone, and giving it a reference to a credential store it will never read
            # would be a lie the console then has to render.
            if bind_default_secret_ref and secret_field_names(connector_cls.ConfigModel):
                secret_ref = default_secret_ref_for(name)
        else:
            SecretRef.parse(secret_ref)
        resolved_settings: dict[str, object] = dict(settings) if settings is not None else {}
        _validate_endpoint_settings(connector, connector_cls.ConfigModel, resolved_settings)

        def _write(session: Session) -> EndpointRow:
            if session.get(EndpointRow, name) is not None:
                raise EndpointAlreadyExistsError(name)
            row = EndpointRow(
                name=name,
                connector=connector,
                role=role,
                secret_ref=secret_ref,
                settings=resolved_settings,
                enabled=enabled,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            audit.record_create(
                session,
                entity_kind=ChangeEntityKind.ENDPOINT,
                entity_id=name,
                actor=actor,
                now=now,
                new_value=_endpoint_snapshot(row),
            )
            _logger.info("endpoint_created", endpoint=name, connector=connector, actor=actor)
            return row

        async with self._unit_of_work() as session:
            return await asyncio.to_thread(_write, session)

    async def update_endpoint(
        self,
        name: str,
        *,
        connector: str | _UnsetType = UNSET,
        role: EndpointRole | _UnsetType = UNSET,
        settings: Mapping[str, object] | _UnsetType = UNSET,
        secret_ref: str | None | _UnsetType = UNSET,
        enabled: bool | _UnsetType = UNSET,
        actor: str,
        now: datetime,
    ) -> EndpointRow:
        """Partial update. A parameter left at :data:`UNSET` keeps its current value;
        ``secret_ref=None`` explicitly clears the reference (an endpoint can be
        registered, then un-bound, without deleting and recreating it).

        A change to ``connector`` and/or ``settings`` re-validates the *effective*
        settings against the *effective* connector's ``ConfigModel`` (same rules as
        :meth:`create_endpoint`); an update touching neither does not re-look-up the
        registry. A no-op update (every supplied value already matches) writes no audit
        row and does not bump the generation.
        """

        def _write(session: Session) -> EndpointRow:
            row = session.get(EndpointRow, name)
            if row is None:
                raise EndpointNotFoundError(name)

            next_connector = row.connector if isinstance(connector, _UnsetType) else connector
            next_settings = (
                dict(row.settings) if isinstance(settings, _UnsetType) else dict(settings)
            )
            if not isinstance(connector, _UnsetType) or not isinstance(settings, _UnsetType):
                connector_cls = self._registry.get_connector(next_connector)
                _validate_endpoint_settings(
                    next_connector, connector_cls.ConfigModel, next_settings
                )
            if not isinstance(secret_ref, _UnsetType) and secret_ref is not None:
                SecretRef.parse(secret_ref)

            changes: list[FieldChange] = []
            if not isinstance(connector, _UnsetType) and connector != row.connector:
                changes.append(
                    FieldChange(field="connector", old_value=row.connector, new_value=connector)
                )
                row.connector = connector
            if not isinstance(role, _UnsetType) and role != row.role:
                changes.append(
                    FieldChange(field="role", old_value=row.role.value, new_value=role.value)
                )
                row.role = role
            if not isinstance(settings, _UnsetType) and next_settings != dict(row.settings):
                changes.append(
                    FieldChange(
                        field="settings", old_value=dict(row.settings), new_value=next_settings
                    )
                )
                row.settings = next_settings
            if not isinstance(secret_ref, _UnsetType) and secret_ref != row.secret_ref:
                changes.append(
                    FieldChange(field="secret_ref", old_value=row.secret_ref, new_value=secret_ref)
                )
                row.secret_ref = secret_ref
            if not isinstance(enabled, _UnsetType) and enabled != row.enabled:
                changes.append(
                    FieldChange(field="enabled", old_value=row.enabled, new_value=enabled)
                )
                row.enabled = enabled

            if not changes:
                return row

            row.updated_at = now
            session.flush()
            audit.record_update(
                session,
                entity_kind=ChangeEntityKind.ENDPOINT,
                entity_id=name,
                actor=actor,
                now=now,
                changes=changes,
            )
            _logger.info(
                "endpoint_updated",
                endpoint=name,
                fields=[change.field for change in changes],
                actor=actor,
            )
            return row

        async with self._unit_of_work() as session:
            return await asyncio.to_thread(_write, session)

    async def delete_endpoint(self, name: str, *, actor: str, now: datetime) -> None:
        """Delete an endpoint. Raises :class:`EndpointInUseError` -- rather than
        letting the ``sync_pairs`` ``ondelete="RESTRICT"`` FK surface a raw
        ``IntegrityError`` -- when any sync pair still names it as source or target."""

        def _write(session: Session) -> None:
            row = session.get(EndpointRow, name)
            if row is None:
                raise EndpointNotFoundError(name)
            blocking = session.scalars(
                select(SyncPairRow.name).where(
                    (SyncPairRow.source == name) | (SyncPairRow.target == name)
                )
            ).all()
            if blocking:
                raise EndpointInUseError(name, pairs=tuple(blocking))

            old_value = _endpoint_snapshot(row)
            session.delete(row)
            session.flush()
            audit.record_delete(
                session,
                entity_kind=ChangeEntityKind.ENDPOINT,
                entity_id=name,
                actor=actor,
                now=now,
                old_value=old_value,
            )
            _logger.info("endpoint_deleted", endpoint=name, actor=actor)

        async with self._unit_of_work() as session:
            await asyncio.to_thread(_write, session)

    # ====================================================================================
    # Stored credentials (amended C2)
    # ====================================================================================

    def _require_cipher(self) -> SecretCipher:
        """This deployment's :class:`~qlabs_catalog_sync.configstore.crypto.SecretCipher`.

        Resolved lazily and cached, not built in ``__init__``: a deployment that never
        stores a credential never needs a key, and creating one at startup for a service
        that will not use it is noise in a directory an operator has to reason about.

        Nothing has to be configured for this to work. ``SecretCipher.for_database`` uses
        an explicitly configured key when there is one and otherwise makes one beside this
        service's own database the first time a credential is saved -- see
        :mod:`~qlabs_catalog_sync.configstore.crypto` for what that default costs and what
        it still protects.
        """
        if self._cipher is None:
            self._cipher = SecretCipher.for_database(self._engine.url)
        return self._cipher

    def secret_backend_for(self, ref: SecretRef) -> SecretBackend:
        """The :class:`~qlabs_catalog_sync.config.SecretBackend` that resolves ``ref``.

        Deliberately a method on the service rather than a free function: a ``db:``
        reference resolves against *this* configuration store, with *this* deployment's
        master key, and the service is the only object that holds both. Every caller that
        builds a connector -- the healthcheck and manifest routes, the preview route, the
        engine's connector pool -- passes this same method, so all four resolve an
        endpoint's credentials identically. Four call sites each choosing their own
        backend is how one of them ends up silently reading a credential from a place the
        operator never configured.

        An ``env:`` reference does not touch the store at all, so this is also correct,
        and cheap, for a deployment that never adopted stored credentials.
        """
        if ref.scheme == DB_SCHEME:
            return backend_for(
                ref, session_factory=self._session_factory, cipher=self._require_cipher()
            )
        return backend_for(ref)

    def _secret_fields_of(self, session: Session, endpoint: str) -> frozenset[str]:
        """The secret-typed field names ``endpoint``'s connector declares.

        Goes through the same
        :func:`~qlabs_catalog_sync.configstore.secrets.secret_field_names` the inline-secret
        rejection and the resolution path use. One definition: a field this accepts is
        exactly a field the resolver will look for, and exactly a field ``settings``
        refuses to carry.
        """
        row = session.get(EndpointRow, endpoint)
        if row is None:
            raise EndpointNotFoundError(endpoint)
        connector_cls = self._registry.get_connector(row.connector)
        return secret_field_names(connector_cls.ConfigModel)

    async def list_endpoint_secrets(self, endpoint: str) -> list[StoredSecretStatus]:
        """Which of ``endpoint``'s secret-typed fields have a stored credential.

        Every field the connector declares is listed, set or not, because "this connector
        wants a client secret and none is stored" is exactly what the console has to be
        able to render. :class:`StoredSecretStatus` has no field capable of holding a
        value -- like ``SecretResolveStatus``, that is enforced by the type rather than by
        remembering not to put one there.
        """

        def _read() -> list[StoredSecretStatus]:
            with self._session_factory() as session:
                declared = self._secret_fields_of(session, endpoint)
                stored = {
                    row.field: row
                    for row in session.scalars(
                        select(EndpointSecretRow).where(EndpointSecretRow.endpoint == endpoint)
                    ).all()
                }
                return [
                    StoredSecretStatus(
                        field=field,
                        is_set=field in stored,
                        updated_at=stored[field].updated_at if field in stored else None,
                        key_id=stored[field].key_id if field in stored else None,
                    )
                    for field in sorted(declared)
                ]

        return await asyncio.to_thread(_read)

    async def set_endpoint_secret(
        self, endpoint: str, field: str, value: str, *, actor: str, now: datetime
    ) -> None:
        """Seal ``value`` and store it as ``endpoint``'s ``field`` credential.

        Rejects a ``field`` the connector does not declare as secret-typed
        (:class:`UnknownSecretFieldError`) rather than storing a credential nothing will
        ever read, and rejects an empty value: clearing is
        :meth:`clear_endpoint_secret`, an explicit and separately-audited act, not a side
        effect of submitting a blank form field.

        The audit row names the field and records ``"set"`` -- never the value, and never
        a length or a prefix of it, both of which are real leaks for a short credential.
        """
        if not value:
            raise EmptySecretValueError(endpoint, field)

        def _write(session: Session) -> None:
            declared = self._secret_fields_of(session, endpoint)
            if field not in declared:
                raise UnknownSecretFieldError(endpoint, field, sorted(declared))

            sealed = self._require_cipher().encrypt(value, endpoint=endpoint, field=field)
            existing = session.scalar(
                select(EndpointSecretRow).where(
                    EndpointSecretRow.endpoint == endpoint, EndpointSecretRow.field == field
                )
            )
            if existing is None:
                session.add(
                    EndpointSecretRow(
                        endpoint=endpoint,
                        field=field,
                        ciphertext=sealed.ciphertext,
                        nonce=sealed.nonce,
                        key_id=sealed.key_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                existing.ciphertext = sealed.ciphertext
                existing.nonce = sealed.nonce
                existing.key_id = sealed.key_id
                existing.updated_at = now
            session.flush()
            audit.record_update(
                session,
                entity_kind=ChangeEntityKind.ENDPOINT,
                entity_id=endpoint,
                actor=actor,
                now=now,
                changes=[
                    audit.FieldChange(
                        field=f"{SECRET_AUDIT_FIELD_PREFIX}{field}",
                        old_value="set" if existing is not None else None,
                        new_value="set",
                    )
                ],
            )
            _logger.info("endpoint_secret_stored", endpoint=endpoint, field=field, actor=actor)

        async with self._unit_of_work() as session:
            await asyncio.to_thread(_write, session)

    async def clear_endpoint_secret(
        self, endpoint: str, field: str, *, actor: str, now: datetime
    ) -> None:
        """Delete ``endpoint``'s stored ``field`` credential.

        Raises :class:`SecretNotStoredError` when there is nothing to clear, rather than
        succeeding silently: "I removed that credential" and "there was never one there"
        are different answers to the operator's question, and only one of them means the
        endpoint just changed.
        """

        def _write(session: Session) -> None:
            self._secret_fields_of(session, endpoint)  # raises if the endpoint is gone
            existing = session.scalar(
                select(EndpointSecretRow).where(
                    EndpointSecretRow.endpoint == endpoint, EndpointSecretRow.field == field
                )
            )
            if existing is None:
                raise SecretNotStoredError(endpoint, field)
            session.delete(existing)
            session.flush()
            audit.record_update(
                session,
                entity_kind=ChangeEntityKind.ENDPOINT,
                entity_id=endpoint,
                actor=actor,
                now=now,
                changes=[
                    audit.FieldChange(
                        field=f"{SECRET_AUDIT_FIELD_PREFIX}{field}",
                        old_value="set",
                        new_value=None,
                    )
                ],
            )
            _logger.info("endpoint_secret_cleared", endpoint=endpoint, field=field, actor=actor)

        async with self._unit_of_work() as session:
            await asyncio.to_thread(_write, session)

    # ====================================================================================
    # Sync pairs
    # ====================================================================================

    async def get_sync_pair(self, pair_id: uuid.UUID) -> SyncPairRow | None:
        def _read() -> SyncPairRow | None:
            with self._session_factory() as session:
                return session.get(SyncPairRow, pair_id)

        return await asyncio.to_thread(_read)

    async def list_sync_pairs(self) -> list[SyncPairRow]:
        def _read() -> list[SyncPairRow]:
            with self._session_factory() as session:
                stmt = select(SyncPairRow).order_by(SyncPairRow.name)
                return list(session.scalars(stmt).all())

        return await asyncio.to_thread(_read)

    async def create_sync_pair(
        self,
        *,
        name: str,
        source: str,
        target: str,
        target_space: str,
        entity_types: Sequence[EntityType] | None = None,
        cadence_seconds: int = 900,
        jitter_seconds: float | None = None,
        manual_edit_policy: ManualEditPolicy | None = None,
        activation_opt_in: bool = False,
        enabled: bool = False,
        actor: str,
        now: datetime,
    ) -> SyncPairRow:
        """Create a sync pair. Raises :class:`SyncPairEndpointError` when ``source``/
        ``target`` do not name a configured endpoint, or when the v1 direction
        guardrail is violated (target must be the sole write connector; source must
        not be it -- see the module docstring); :class:`SyncPairAlreadyExistsError`
        when ``name`` is already taken.
        """

        def _write(session: Session) -> SyncPairRow:
            clash = session.execute(
                select(SyncPairRow.id).where(SyncPairRow.name == name)
            ).scalar_one_or_none()
            if clash is not None:
                raise SyncPairAlreadyExistsError(name)

            source_row = _check_endpoint_exists(
                session, source, pair_name=name, role_label="source"
            )
            target_row = _check_endpoint_exists(
                session, target, pair_name=name, role_label="target"
            )
            _check_pair_direction(name, source_row, target_row)

            row = SyncPairRow(
                name=name,
                source=source,
                target=target,
                target_space=target_space,
                entity_types=list(entity_types) if entity_types is not None else [],
                cadence_seconds=cadence_seconds,
                jitter_seconds=jitter_seconds,
                manual_edit_policy=(
                    manual_edit_policy if manual_edit_policy is not None else ManualEditPolicy()
                ),
                activation_opt_in=activation_opt_in,
                enabled=enabled,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            audit.record_create(
                session,
                entity_kind=ChangeEntityKind.SYNC_PAIR,
                entity_id=str(row.id),
                actor=actor,
                now=now,
                new_value=_sync_pair_snapshot(row),
            )
            _logger.info("sync_pair_created", pair=name, actor=actor)
            return row

        async with self._unit_of_work() as session:
            return await asyncio.to_thread(_write, session)

    async def update_sync_pair(
        self,
        pair_id: uuid.UUID,
        *,
        name: str | _UnsetType = UNSET,
        source: str | _UnsetType = UNSET,
        target: str | _UnsetType = UNSET,
        target_space: str | _UnsetType = UNSET,
        entity_types: Sequence[EntityType] | _UnsetType = UNSET,
        cadence_seconds: int | _UnsetType = UNSET,
        jitter_seconds: float | None | _UnsetType = UNSET,
        manual_edit_policy: ManualEditPolicy | _UnsetType = UNSET,
        activation_opt_in: bool | _UnsetType = UNSET,
        enabled: bool | _UnsetType = UNSET,
        actor: str,
        now: datetime,
    ) -> SyncPairRow:
        """Partial update, same :data:`UNSET` convention as :meth:`update_endpoint`.

        Touching ``source`` and/or ``target`` re-runs the direction guardrail against
        the *effective* endpoints (mirrors :meth:`create_sync_pair`); an update that
        touches neither does not re-check it.
        """

        def _write(session: Session) -> SyncPairRow:
            row = session.get(SyncPairRow, pair_id)
            if row is None:
                raise SyncPairNotFoundError(pair_id)

            next_name = row.name if isinstance(name, _UnsetType) else name
            if next_name != row.name:
                clash = session.execute(
                    select(SyncPairRow.id).where(
                        SyncPairRow.name == next_name, SyncPairRow.id != pair_id
                    )
                ).scalar_one_or_none()
                if clash is not None:
                    raise SyncPairAlreadyExistsError(next_name)

            next_source = row.source if isinstance(source, _UnsetType) else source
            next_target = row.target if isinstance(target, _UnsetType) else target
            if not isinstance(source, _UnsetType) or not isinstance(target, _UnsetType):
                source_row = _check_endpoint_exists(
                    session, next_source, pair_name=next_name, role_label="source"
                )
                target_row = _check_endpoint_exists(
                    session, next_target, pair_name=next_name, role_label="target"
                )
                _check_pair_direction(next_name, source_row, target_row)

            changes: list[FieldChange] = []
            if not isinstance(name, _UnsetType) and name != row.name:
                changes.append(FieldChange(field="name", old_value=row.name, new_value=name))
                row.name = name
            if not isinstance(source, _UnsetType) and source != row.source:
                changes.append(FieldChange(field="source", old_value=row.source, new_value=source))
                row.source = source
            if not isinstance(target, _UnsetType) and target != row.target:
                changes.append(FieldChange(field="target", old_value=row.target, new_value=target))
                row.target = target
            if not isinstance(target_space, _UnsetType) and target_space != row.target_space:
                changes.append(
                    FieldChange(
                        field="target_space", old_value=row.target_space, new_value=target_space
                    )
                )
                row.target_space = target_space
            if not isinstance(entity_types, _UnsetType):
                new_entity_types = list(entity_types)
                old_values = [entity_type.value for entity_type in row.entity_types]
                new_values = [entity_type.value for entity_type in new_entity_types]
                if new_values != old_values:
                    changes.append(
                        FieldChange(
                            field="entity_types", old_value=old_values, new_value=new_values
                        )
                    )
                    row.entity_types = new_entity_types
            if (
                not isinstance(cadence_seconds, _UnsetType)
                and cadence_seconds != row.cadence_seconds
            ):
                changes.append(
                    FieldChange(
                        field="cadence_seconds",
                        old_value=row.cadence_seconds,
                        new_value=cadence_seconds,
                    )
                )
                row.cadence_seconds = cadence_seconds
            if not isinstance(jitter_seconds, _UnsetType) and jitter_seconds != row.jitter_seconds:
                changes.append(
                    FieldChange(
                        field="jitter_seconds",
                        old_value=row.jitter_seconds,
                        new_value=jitter_seconds,
                    )
                )
                row.jitter_seconds = jitter_seconds
            if not isinstance(manual_edit_policy, _UnsetType):
                old_policy = row.manual_edit_policy.model_dump(mode="json")
                new_policy = manual_edit_policy.model_dump(mode="json")
                if new_policy != old_policy:
                    changes.append(
                        FieldChange(
                            field="manual_edit_policy", old_value=old_policy, new_value=new_policy
                        )
                    )
                    row.manual_edit_policy = manual_edit_policy
            if (
                not isinstance(activation_opt_in, _UnsetType)
                and activation_opt_in != row.activation_opt_in
            ):
                changes.append(
                    FieldChange(
                        field="activation_opt_in",
                        old_value=row.activation_opt_in,
                        new_value=activation_opt_in,
                    )
                )
                row.activation_opt_in = activation_opt_in
            if not isinstance(enabled, _UnsetType) and enabled != row.enabled:
                changes.append(
                    FieldChange(field="enabled", old_value=row.enabled, new_value=enabled)
                )
                row.enabled = enabled

            if not changes:
                return row

            row.updated_at = now
            session.flush()
            audit.record_update(
                session,
                entity_kind=ChangeEntityKind.SYNC_PAIR,
                entity_id=str(row.id),
                actor=actor,
                now=now,
                changes=changes,
            )
            _logger.info(
                "sync_pair_updated",
                pair=str(pair_id),
                fields=[change.field for change in changes],
                actor=actor,
            )
            return row

        async with self._unit_of_work() as session:
            return await asyncio.to_thread(_write, session)

    async def delete_sync_pair(self, pair_id: uuid.UUID, *, actor: str, now: datetime) -> None:
        """Delete a sync pair. Its ``selection_rules``/``selection_overrides`` cascade
        at the schema level (``ondelete="CASCADE"``); this method does not append a
        separate audit row per cascaded rule/override -- the pair's own DELETE row
        already records that its whole rule set is gone as of this generation.
        """

        def _write(session: Session) -> None:
            row = session.get(SyncPairRow, pair_id)
            if row is None:
                raise SyncPairNotFoundError(pair_id)
            old_value = _sync_pair_snapshot(row)
            session.delete(row)
            session.flush()
            audit.record_delete(
                session,
                entity_kind=ChangeEntityKind.SYNC_PAIR,
                entity_id=str(pair_id),
                actor=actor,
                now=now,
                old_value=old_value,
            )
            _logger.info("sync_pair_deleted", pair=str(pair_id), actor=actor)

        async with self._unit_of_work() as session:
            await asyncio.to_thread(_write, session)

    # ====================================================================================
    # Selection rules
    # ====================================================================================

    async def get_selection_rule(self, rule_id: uuid.UUID) -> SelectionRuleRow | None:
        def _read() -> SelectionRuleRow | None:
            with self._session_factory() as session:
                return session.get(SelectionRuleRow, rule_id)

        return await asyncio.to_thread(_read)

    async def list_selection_rules(
        self, pair_id: uuid.UUID, scope: RuleScope
    ) -> list[SelectionRuleRow]:
        """This pair's rules for ``scope``, in evaluation order (ordinal ascending)."""

        def _read() -> list[SelectionRuleRow]:
            with self._session_factory() as session:
                stmt = (
                    select(SelectionRuleRow)
                    .where(SelectionRuleRow.pair_id == pair_id, SelectionRuleRow.scope == scope)
                    .order_by(SelectionRuleRow.ordinal)
                )
                return list(session.scalars(stmt).all())

        return await asyncio.to_thread(_read)

    async def create_selection_rule(
        self,
        *,
        pair_id: uuid.UUID,
        scope: RuleScope,
        decision: SelectionDecision,
        matcher_kind: MatcherKind,
        pattern: str,
        ordinal: int | None = None,
        actor: str,
        now: datetime,
    ) -> SelectionRuleRow:
        """Create one rule. ``ordinal`` defaults to "append at the end" of this pair's
        (scope-scoped) rule set -- an operator adding one rule should not have to know
        the current length of the list. Raises :class:`SelectionRuleOrdinalConflictError`
        (rather than a raw ``IntegrityError``) if an explicit ``ordinal`` collides with
        an existing rule's.
        """

        def _write(session: Session) -> SelectionRuleRow:
            pair = session.get(SyncPairRow, pair_id)
            if pair is None:
                raise SyncPairNotFoundError(pair_id)

            effective_ordinal = ordinal
            if effective_ordinal is None:
                current_max = session.execute(
                    select(func.max(SelectionRuleRow.ordinal)).where(
                        SelectionRuleRow.pair_id == pair_id, SelectionRuleRow.scope == scope
                    )
                ).scalar_one()
                effective_ordinal = 0 if current_max is None else current_max + 1

            row = SelectionRuleRow(
                pair_id=pair_id,
                ordinal=effective_ordinal,
                scope=scope,
                decision=decision,
                matcher_kind=matcher_kind,
                pattern=pattern,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise SelectionRuleOrdinalConflictError(pair_id, scope, effective_ordinal) from exc

            audit.record_create(
                session,
                entity_kind=ChangeEntityKind.SELECTION_RULE,
                entity_id=str(row.id),
                actor=actor,
                now=now,
                new_value=_selection_rule_snapshot(row),
            )
            _logger.info("selection_rule_created", pair=str(pair_id), rule=str(row.id), actor=actor)
            return row

        async with self._unit_of_work() as session:
            return await asyncio.to_thread(_write, session)

    async def update_selection_rule(
        self,
        rule_id: uuid.UUID,
        *,
        decision: SelectionDecision | _UnsetType = UNSET,
        matcher_kind: MatcherKind | _UnsetType = UNSET,
        pattern: str | _UnsetType = UNSET,
        actor: str,
        now: datetime,
    ) -> SelectionRuleRow:
        """Update a rule's ``decision``/``matcher_kind``/``pattern``.

        Deliberately does not accept ``ordinal`` or ``scope`` here: ordinal position is
        an explicit operation (:meth:`reorder_selection_rules`), never an implicit side
        effect of an unrelated field edit.
        """

        def _write(session: Session) -> SelectionRuleRow:
            row = session.get(SelectionRuleRow, rule_id)
            if row is None:
                raise SelectionRuleNotFoundError(rule_id)

            changes: list[FieldChange] = []
            if not isinstance(decision, _UnsetType) and decision != row.decision:
                changes.append(
                    FieldChange(
                        field="decision", old_value=row.decision.value, new_value=decision.value
                    )
                )
                row.decision = decision
            if not isinstance(matcher_kind, _UnsetType) and matcher_kind != row.matcher_kind:
                changes.append(
                    FieldChange(
                        field="matcher_kind",
                        old_value=row.matcher_kind.value,
                        new_value=matcher_kind.value,
                    )
                )
                row.matcher_kind = matcher_kind
            if not isinstance(pattern, _UnsetType) and pattern != row.pattern:
                changes.append(
                    FieldChange(field="pattern", old_value=row.pattern, new_value=pattern)
                )
                row.pattern = pattern

            if not changes:
                return row

            row.updated_at = now
            session.flush()
            audit.record_update(
                session,
                entity_kind=ChangeEntityKind.SELECTION_RULE,
                entity_id=str(rule_id),
                actor=actor,
                now=now,
                changes=changes,
            )
            _logger.info(
                "selection_rule_updated",
                rule=str(rule_id),
                fields=[change.field for change in changes],
                actor=actor,
            )
            return row

        async with self._unit_of_work() as session:
            return await asyncio.to_thread(_write, session)

    async def delete_selection_rule(self, rule_id: uuid.UUID, *, actor: str, now: datetime) -> None:
        def _write(session: Session) -> None:
            row = session.get(SelectionRuleRow, rule_id)
            if row is None:
                raise SelectionRuleNotFoundError(rule_id)
            old_value = _selection_rule_snapshot(row)
            session.delete(row)
            session.flush()
            audit.record_delete(
                session,
                entity_kind=ChangeEntityKind.SELECTION_RULE,
                entity_id=str(rule_id),
                actor=actor,
                now=now,
                old_value=old_value,
            )
            _logger.info("selection_rule_deleted", rule=str(rule_id), actor=actor)

        async with self._unit_of_work() as session:
            await asyncio.to_thread(_write, session)

    async def reorder_selection_rules(
        self,
        pair_id: uuid.UUID,
        scope: RuleScope,
        ordered_rule_ids: Sequence[uuid.UUID],
        *,
        actor: str,
        now: datetime,
    ) -> list[SelectionRuleRow]:
        """Rewrite this pair's ``scope`` rule set to the exact order given.

        ``ordered_rule_ids`` must be exactly the existing rule ids for
        ``(pair_id, scope)``, each once -- :class:`SelectionRuleReorderMismatchError`
        otherwise. Ordinal position within the list is 0-based, evaluated low-to-high
        (C3: last match wins), so ``ordered_rule_ids[0]`` becomes ordinal ``0``.

        **Collision-free by construction.** ``(pair_id, scope, ordinal)`` is uniquely
        constrained, so renumbering N rows to a new permutation of ``0..N-1`` one
        ``UPDATE`` at a time can transiently collide with a row not yet moved (e.g.
        swapping ordinals 0 and 1 directly). This does it in two passes instead:

        1. Move every row in the set to a *temporary* ordinal in
           ``[offset, offset + N)``, where ``offset = max(existing ordinals) + 1``. By
           the pigeonhole principle, N distinct non-negative ordinals always have a
           maximum >= N - 1, so ``offset >= N`` always holds -- the temporary range
           never overlaps the final range (``[0, N)``), and it never overlaps any
           pre-move ordinal either (all of them are `< offset` by construction). Every
           ``UPDATE`` in this pass is therefore collision-free regardless of the order
           SQLAlchemy issues them in.
        2. Move every row from its temporary ordinal to its final ``0..N-1`` position.
           Finals are pairwise distinct by construction and disjoint from every
           remaining temporary value (all `>= offset >= N`), so this pass is
           collision-free the same way.

        Both passes run inside the same transaction as the rest of this method
        (:meth:`_unit_of_work`), so a failure anywhere -- including between the two
        passes -- rolls back to the original order exactly, with no dependence on the
        temporary values ever being visible outside this transaction.
        """

        def _write(session: Session) -> list[SelectionRuleRow]:
            existing = list(
                session.scalars(
                    select(SelectionRuleRow)
                    .where(SelectionRuleRow.pair_id == pair_id, SelectionRuleRow.scope == scope)
                    .order_by(SelectionRuleRow.ordinal)
                ).all()
            )
            existing_by_id = {row.id: row for row in existing}
            requested = list(ordered_rule_ids)

            if set(requested) != set(existing_by_id) or len(requested) != len(existing_by_id):
                raise SelectionRuleReorderMismatchError(
                    pair_id,
                    scope,
                    expected=[str(rule_id) for rule_id in existing_by_id],
                    given=[str(rule_id) for rule_id in requested],
                )

            if requested == [row.id for row in existing]:
                return existing  # already in the requested order: no write, no bump

            old_order = [str(row.id) for row in existing]

            offset = max((row.ordinal for row in existing), default=-1) + 1
            for index, row in enumerate(existing):
                row.ordinal = offset + index
            session.flush()

            for new_ordinal, rule_id in enumerate(requested):
                existing_by_id[rule_id].ordinal = new_ordinal
            session.flush()

            new_order = [str(rule_id) for rule_id in requested]
            audit.record_reorder(
                session,
                entity_kind=ChangeEntityKind.SELECTION_RULE,
                entity_id=f"{pair_id!s}:{scope.value}",
                actor=actor,
                now=now,
                field=f"order:{scope.value}",
                old_value=old_order,
                new_value=new_order,
            )
            _logger.info(
                "selection_rules_reordered", pair=str(pair_id), scope=scope.value, actor=actor
            )
            return sorted(existing_by_id.values(), key=lambda row: row.ordinal)

        async with self._unit_of_work() as session:
            return await asyncio.to_thread(_write, session)

    # ====================================================================================
    # Selection overrides
    # ====================================================================================

    async def get_selection_override(self, override_id: uuid.UUID) -> SelectionOverrideRow | None:
        def _read() -> SelectionOverrideRow | None:
            with self._session_factory() as session:
                return session.get(SelectionOverrideRow, override_id)

        return await asyncio.to_thread(_read)

    async def list_selection_overrides(
        self, pair_id: uuid.UUID, scope: RuleScope
    ) -> list[SelectionOverrideRow]:
        def _read() -> list[SelectionOverrideRow]:
            with self._session_factory() as session:
                stmt = (
                    select(SelectionOverrideRow)
                    .where(
                        SelectionOverrideRow.pair_id == pair_id,
                        SelectionOverrideRow.scope == scope,
                    )
                    .order_by(SelectionOverrideRow.object_id)
                )
                return list(session.scalars(stmt).all())

        return await asyncio.to_thread(_read)

    async def create_selection_override(
        self,
        *,
        pair_id: uuid.UUID,
        scope: RuleScope,
        object_id: str,
        decision: SelectionDecision,
        reason: str | None = None,
        actor: str,
        now: datetime,
    ) -> SelectionOverrideRow:
        def _write(session: Session) -> SelectionOverrideRow:
            pair = session.get(SyncPairRow, pair_id)
            if pair is None:
                raise SyncPairNotFoundError(pair_id)

            clash = session.execute(
                select(SelectionOverrideRow.id).where(
                    SelectionOverrideRow.pair_id == pair_id,
                    SelectionOverrideRow.scope == scope,
                    SelectionOverrideRow.object_id == object_id,
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise SelectionOverrideAlreadyExistsError(pair_id, scope, object_id)

            row = SelectionOverrideRow(
                pair_id=pair_id,
                scope=scope,
                object_id=object_id,
                decision=decision,
                reason=reason,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            audit.record_create(
                session,
                entity_kind=ChangeEntityKind.SELECTION_OVERRIDE,
                entity_id=str(row.id),
                actor=actor,
                now=now,
                new_value=_selection_override_snapshot(row),
            )
            _logger.info(
                "selection_override_created", pair=str(pair_id), override=str(row.id), actor=actor
            )
            return row

        async with self._unit_of_work() as session:
            return await asyncio.to_thread(_write, session)

    async def update_selection_override(
        self,
        override_id: uuid.UUID,
        *,
        decision: SelectionDecision | _UnsetType = UNSET,
        reason: str | None | _UnsetType = UNSET,
        actor: str,
        now: datetime,
    ) -> SelectionOverrideRow:
        """Update an override's ``decision``/``reason``.

        Does not accept ``scope``/``object_id`` -- together with ``pair_id`` they are
        this override's identity (the unique constraint is on the triple); moving an
        override to a different object is a delete-and-recreate, not an edit.
        """

        def _write(session: Session) -> SelectionOverrideRow:
            row = session.get(SelectionOverrideRow, override_id)
            if row is None:
                raise SelectionOverrideNotFoundError(override_id)

            changes: list[FieldChange] = []
            if not isinstance(decision, _UnsetType) and decision != row.decision:
                changes.append(
                    FieldChange(
                        field="decision", old_value=row.decision.value, new_value=decision.value
                    )
                )
                row.decision = decision
            if not isinstance(reason, _UnsetType) and reason != row.reason:
                changes.append(FieldChange(field="reason", old_value=row.reason, new_value=reason))
                row.reason = reason

            if not changes:
                return row

            row.updated_at = now
            session.flush()
            audit.record_update(
                session,
                entity_kind=ChangeEntityKind.SELECTION_OVERRIDE,
                entity_id=str(override_id),
                actor=actor,
                now=now,
                changes=changes,
            )
            _logger.info(
                "selection_override_updated",
                override=str(override_id),
                fields=[change.field for change in changes],
                actor=actor,
            )
            return row

        async with self._unit_of_work() as session:
            return await asyncio.to_thread(_write, session)

    async def delete_selection_override(
        self, override_id: uuid.UUID, *, actor: str, now: datetime
    ) -> None:
        def _write(session: Session) -> None:
            row = session.get(SelectionOverrideRow, override_id)
            if row is None:
                raise SelectionOverrideNotFoundError(override_id)
            old_value = _selection_override_snapshot(row)
            session.delete(row)
            session.flush()
            audit.record_delete(
                session,
                entity_kind=ChangeEntityKind.SELECTION_OVERRIDE,
                entity_id=str(override_id),
                actor=actor,
                now=now,
                old_value=old_value,
            )
            _logger.info("selection_override_deleted", override=str(override_id), actor=actor)

        async with self._unit_of_work() as session:
            await asyncio.to_thread(_write, session)
