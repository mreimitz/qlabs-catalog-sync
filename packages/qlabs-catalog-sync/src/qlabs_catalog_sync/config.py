"""Engine config & secrets loading, including the sync-pair schema.

WP2 / T2.3. This module is the engine-side counterpart to the SDK's connector-side
config base (``qlabs_catalog_sync_sdk.config.ConnectorConfig``, T1.7): it describes
*which endpoints exist* and *which pairs to sync between them*, and it is deliberately
a different object from a connector's own ``ConfigModel``. The engine (T2.1, T2.4)
binds and injects the latter; this module never duplicates or replaces it.

Three things live here:

* :class:`EndpointConfig` — one named endpoint instance: which connector implements
  it, its plain (non-secret) settings, and references to its secrets. Endpoints are
  keyed by an arbitrary, engine-chosen string (``"qlik_acme"``, ``"databricks_prod"``)
  rather than by connector name, so two tenants of the same connector — two Qlik
  spaces, for instance — are configured side by side in one process.
* :class:`SyncPairConfig` — **the sync-pair schema, the MVP's user-facing contract**
  (T2.4, T2.6, T2.8, T9.2 all build on it): source endpoint, target endpoint, Unity
  Catalog ``catalog.schema`` glob selector patterns (decision D1), the target Qlik
  space, which entity types to sync, the poll cadence, the manual-edit policy, and
  the activation opt-in flag (decision D7, off by default).
* :class:`EngineConfig` — the ``pydantic-settings`` root: ``endpoints`` plus
  ``pairs``, loadable from a JSON file and/or environment variables, with
  cross-referential validation (a pair's ``source``/``target`` must name a real
  endpoint) and the v1 direction guardrails (Qlik is the only write target and can
  never be a source) enforced at load time.

Secrets. Credentials are never embedded in config directly — :class:`EndpointConfig`
carries *references* (a backend-specific key, e.g. an env var suffix) in ``secrets``,
resolved into real ``SecretStr`` values only when :meth:`EndpointConfig.resolve` or
:meth:`EngineConfig.resolve_credentials` is called, through a pluggable
:class:`SecretBackend`. :class:`EnvironmentSecretBackend` (the default) resolves
against the process environment using the same ``<ENDPOINT>__<FIELD>`` convention as
``ConnectorConfig.for_endpoint`` (T1.7), so the resolved dict is ready to pass
straight through as that method's ``**values`` override — a secret manager backend
(Vault, a cloud KMS) is a drop-in replacement implementing the same
:class:`SecretBackend` protocol. Because the config *model* only ever stores
references, not values, secrets cannot leak through ``repr``/``str``/``model_dump``/
``model_dump_json`` of :class:`EndpointConfig` or :class:`EngineConfig` — there is
nothing sensitive in them to leak. The resolved values themselves are ``SecretStr``,
so even the returned credential dict stays redacted on ``repr``/``str``. Nothing here
is ever written to the state store or logged (see the SDK's ``logging.py``
redaction, T1.7).

Validation. Every failure a pair/endpoint definition can have — a pair naming an
endpoint that does not exist, a malformed ``catalog.schema`` pattern, an empty
entity-type list, a non-positive cadence, a blank target space, a pair pointing the
wrong direction (Qlik as source, or a non-Qlik target) — raises a message naming the
offending pair (by its ``name``) and field, not a bare pydantic dump. Unsupported
entity types (anything outside the neutral ``EntityType`` enum — there is no
``Principal``/``AccessBinding`` by design, matching the v1 no-access-control-sync
decision) are rejected by pydantic's own enum coercion, whose message already lists
the valid choices and the exact ``pairs.<n>.entity_types.<i>`` location.

Direction guardrails, and how strict they are. **Every** pair is checked, unconditionally:
the target endpoint's connector must be the sole v1 write connector (``"qlik"``), and the
source endpoint's connector must not be. This is deliberately not configurable — v1's
scope decision (``decision.md``) states "Qlik is the ONLY write target" and "Upstream-only"
as hard limits, not a default that a pair can opt out of, so getting the direction wrong is
caught here, at config load, rather than surfacing as a confusing failure deep in the write
path.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from qlabs_catalog_sync_sdk.models import EntityType

__all__ = [
    "WRITE_CONNECTOR_NAME",
    "EndpointConfig",
    "EngineConfig",
    "EnvironmentSecretBackend",
    "ManualEditMode",
    "ManualEditPolicy",
    "SecretBackend",
    "SecretNotFoundError",
    "SyncPairConfig",
    "matches_catalog_schema",
]

#: The sole v1 write connector (decision.md guardrail 1; decision-databricks-to-qlik-mvp.md).
#: A pair's ``target`` must resolve to an endpoint using this connector; a pair's ``source``
#: must not.
WRITE_CONNECTOR_NAME = "qlik"


# --------------------------------------------------------------------------------------
# Secret backend abstraction
# --------------------------------------------------------------------------------------


class SecretNotFoundError(RuntimeError):
    """A configured secret reference could not be resolved by a :class:`SecretBackend`.

    Carries ``endpoint``/``key``/``backend`` as structured attributes (not just message
    text) so a caller can react programmatically; the message itself already names all
    three plus, where the backend can offer one, a hint on how to fix it.
    """

    def __init__(self, *, endpoint: str, key: str, backend: str, hint: str | None = None) -> None:
        message = f"secret backend {backend!r} has no value for endpoint {endpoint!r}, key {key!r}"
        if hint:
            message = f"{message} ({hint})"
        super().__init__(message)
        self.endpoint = endpoint
        self.key = key
        self.backend = backend


@runtime_checkable
class SecretBackend(Protocol):
    """Pluggable source of resolved secret values for endpoint credentials.

    A backend resolves one named secret for one endpoint into a :class:`SecretStr`.
    ``endpoint`` is the engine-chosen endpoint key (e.g. ``"qlik_acme"``); ``key`` is the
    backend-specific reference stored in :attr:`EndpointConfig.secrets` (an env var
    suffix for :class:`EnvironmentSecretBackend`; a Vault path or a cloud secret-manager
    entry name for a future implementation). Swapping backends is a matter of passing a
    different object satisfying this protocol to :meth:`EndpointConfig.resolve` /
    :meth:`EngineConfig.resolve_credentials` — nothing else in this module changes.
    """

    def get_secret(self, *, endpoint: str, key: str) -> SecretStr:
        """Resolve one secret, or raise :class:`SecretNotFoundError` when it is absent."""
        ...


def _normalize_endpoint_key(endpoint: str) -> str:
    """Turn an endpoint key into the env-var-safe, upper-cased segment used to read it.

    Mirrors ``qlabs_catalog_sync_sdk.config._normalize_endpoint_key`` (T1.7) exactly, so
    a secret resolved here under endpoint ``"qlik-acme"`` lands on the same
    ``QLIK_ACME__...`` variable that ``ConnectorConfig.for_endpoint("qlik-acme")`` would
    read directly. Duplicated rather than imported: that helper is private to the SDK
    module, and the two are small and stable enough that copying is simpler than adding
    a new SDK export for it.
    """
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", endpoint.strip()).strip("_")
    if not normalized:
        raise ValueError(
            f"endpoint key must contain at least one alphanumeric character: {endpoint!r}"
        )
    return normalized.upper()


@dataclass(slots=True)
class EnvironmentSecretBackend:
    """Default :class:`SecretBackend`: reads ``<ENDPOINT>__<KEY>`` from the environment.

    This is the "environment today" half of the secret-backend abstraction (a Vault or
    cloud-secret-manager backend is a later, separate implementation of the same
    protocol — see the module docstring). Reading ``os.environ`` here is deliberate and
    scoped: this class *is* the settings machinery for the environment backend, the one
    place the engine's "never read the environment directly" convention expects it.
    """

    def get_secret(self, *, endpoint: str, key: str) -> SecretStr:
        env_name = f"{_normalize_endpoint_key(endpoint)}__{key.upper()}"
        value = os.environ.get(env_name)
        if value is None:
            raise SecretNotFoundError(
                endpoint=endpoint,
                key=key,
                backend="environment",
                hint=f"expected environment variable {env_name}",
            )
        return SecretStr(value)


# --------------------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------------------


class EndpointConfig(BaseModel):
    """One configured endpoint instance: a connector plus its settings and credentials.

    ``connector`` is the entry-point name the engine's connector registry (T2.1) looks
    the implementation up by (``"qlik"``, ``"databricks"``, ...) — this module does not
    itself validate that the name is installed; that is the registry's job at startup.

    ``settings`` carries plain, non-secret connector configuration (a base URL, a
    warehouse id, ...) verbatim. ``secrets`` carries *references*, never values: each
    entry maps a connector config field name to a backend-specific key that
    :meth:`resolve` looks up through a :class:`SecretBackend`. Keeping actual secret
    material out of this model entirely is what makes it safe to log, dump, or check
    into a config file next to non-secret settings.
    """

    model_config = ConfigDict(extra="forbid")

    connector: str = Field(min_length=1)
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)

    def resolve(self, endpoint_key: str, backend: SecretBackend) -> dict[str, Any]:
        """Resolve this endpoint's settings + secrets into ``ConnectorConfig.for_endpoint``
        override kwargs.

        Returns ``{**settings, **{field: backend.get_secret(...) for field in secrets}}``
        — a plain dict mixing ordinary values and ``SecretStr`` secrets, exactly the shape
        ``ConnectorConfig.for_endpoint(endpoint_key, **values)`` (SDK T1.7) accepts as
        explicit overrides. Raises :class:`SecretNotFoundError` on the first secret the
        backend cannot resolve.
        """
        resolved: dict[str, Any] = dict(self.settings)
        for field_name, secret_key in self.secrets.items():
            resolved[field_name] = backend.get_secret(endpoint=endpoint_key, key=secret_key)
        return resolved


# --------------------------------------------------------------------------------------
# catalog.schema selector patterns (decision D1)
# --------------------------------------------------------------------------------------

#: Allowed characters in one glob segment: UC identifiers (letters, digits, underscore)
#: plus the glob metacharacters fnmatch understands.
_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_*?\[\]!]+$")


def _validate_catalog_schema_pattern(pattern: str) -> None:
    """Raise ``ValueError`` with a precise reason when ``pattern`` is not a valid
    ``catalog.schema`` glob.

    A valid pattern has exactly one ``.`` (Unity Catalog catalog and schema names are
    plain identifiers and never contain a literal dot themselves — see decision D1),
    with non-empty catalog and schema segments built only from identifier characters
    and fnmatch wildcards (``*``, ``?``, ``[...]``, ``!``).
    """
    if pattern.count(".") != 1:
        raise ValueError(
            f"{pattern!r} must contain exactly one '.' separating the catalog and schema "
            "glob segments (e.g. 'sales.*' or 'prod_*.finance')"
        )
    catalog_part, schema_part = pattern.split(".")
    if not catalog_part or not schema_part:
        raise ValueError(
            f"{pattern!r} has an empty catalog or schema segment "
            "(both sides of the '.' must be non-empty)"
        )
    for part, side in ((catalog_part, "catalog"), (schema_part, "schema")):
        if not _SEGMENT_PATTERN.match(part):
            raise ValueError(
                f"{pattern!r} has an invalid {side} segment {part!r} — allowed characters "
                "are letters, digits, underscore, and the glob wildcards * ? [ ] !"
            )


def matches_catalog_schema(pattern: str, catalog: str, schema: str) -> bool:
    """True when ``catalog.schema`` is matched by one ``catalog.schema`` glob ``pattern``.

    Each side of ``pattern`` is matched against the corresponding argument independently
    and case-sensitively via :func:`fnmatch.fnmatchcase` (Unity Catalog identifiers are
    case-sensitive as stored) — ``"sales.*"`` matches ``("sales", "anything")`` but not
    ``("Sales", "anything")`` or ``("sales_eu", "anything")``. ``pattern`` is assumed
    already valid (see :func:`_validate_catalog_schema_pattern`, enforced by
    :class:`SyncPairConfig` on every pattern it stores).
    """
    catalog_pattern, schema_pattern = pattern.split(".", maxsplit=1)
    return fnmatch.fnmatchcase(catalog, catalog_pattern) and fnmatch.fnmatchcase(
        schema, schema_pattern
    )


# --------------------------------------------------------------------------------------
# Manual-edit policy
# --------------------------------------------------------------------------------------


class ManualEditMode(StrEnum):
    """How the engine reacts when a Qlik-side value was edited manually since last sync."""

    SOURCE_WINS = "source_wins"
    """Default: the next sync overwrites the manual edit with the source's value."""

    PRESERVE_LOCAL = "preserve_local"
    """The manual edit is left alone; the source value is not written for this scope."""


class ManualEditPolicy(BaseModel):
    """The pair's manual-edit-on-Qlik policy (source-wins by default, per decision.md
    guardrail 2), configurable to preserve local edits per entity type or per field.

    Resolution order in :meth:`mode_for`, most specific first: a ``per_field`` entry for
    the exact ``"<entity_type>.<field>"`` pair, then a ``per_entity`` entry for the
    entity type, then :attr:`default`.
    """

    model_config = ConfigDict(extra="forbid")

    default: ManualEditMode = ManualEditMode.SOURCE_WINS
    per_entity: dict[EntityType, ManualEditMode] = Field(default_factory=dict)
    per_field: dict[str, ManualEditMode] = Field(default_factory=dict)
    """Keyed ``"<entity_type value>.<neutral field name>"``, e.g. ``"data_product.description"``."""

    def mode_for(self, entity_type: EntityType, field: str | None = None) -> ManualEditMode:
        """The effective mode for ``entity_type`` (optionally narrowed to ``field``)."""
        if field is not None:
            per_field_mode = self.per_field.get(f"{entity_type.value}.{field}")
            if per_field_mode is not None:
                return per_field_mode
        return self.per_entity.get(entity_type, self.default)


# --------------------------------------------------------------------------------------
# The sync-pair schema (the MVP's user-facing contract)
# --------------------------------------------------------------------------------------


class SyncPairConfig(BaseModel):
    """One source-endpoint-to-target-endpoint sync pair — the MVP's user-facing contract.

    Every field a pair needs, per the WP2/T2.3 task and decision D1/D7:

    * ``name`` — a unique, human-chosen identifier for this pair, used in every
      validation error, log line, and (T2.6) scheduler job id that concerns it.
    * ``source`` / ``target`` — keys into :attr:`EngineConfig.endpoints`. Existence and
      the v1 direction guardrails (target must be the Qlik write connector; source must
      not be) are checked at the :class:`EngineConfig` level, where both endpoints and
      pairs are visible together.
    * ``catalog_schema_patterns`` — Unity Catalog ``catalog.schema`` glob selectors
      (decision D1): which UC schemas (each one, one Qlik data product) this pair syncs.
      See :func:`matches_catalog_schema`.
    * ``target_space`` — the Qlik space new/updated data products belong to.
    * ``entity_types`` — which neutral entity types this pair syncs; must be non-empty.
    * ``cadence_seconds`` — poll interval; must be positive. Defaults to 900s (15
      minutes), the upper end of the architecture doc's suggested data-product cadence.
    * ``manual_edit_policy`` — see :class:`ManualEditPolicy`.
    * ``activation_opt_in`` — **off by default** (decision D7): activating a Qlik data
      product makes it discoverable tenant-wide, so a pair must opt in explicitly rather
      than activation happening as a side effect of syncing.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    # validate_default=True on the three fields below is deliberate: their default is
    # empty/blank and *invalid*, so an entirely-omitted field fails through the same
    # pair-named custom validator as an explicitly-empty one, rather than pydantic
    # silently accepting an unusable default.
    catalog_schema_patterns: list[str] = Field(default_factory=list, validate_default=True)
    target_space: str = Field(default="", validate_default=True)
    entity_types: list[EntityType] = Field(default_factory=list, validate_default=True)
    cadence_seconds: int = 900
    manual_edit_policy: ManualEditPolicy = Field(default_factory=ManualEditPolicy)
    activation_opt_in: bool = False

    @field_validator("catalog_schema_patterns")
    @classmethod
    def _validate_patterns(cls, patterns: list[str], info: ValidationInfo) -> list[str]:
        name = str(info.data.get("name", "<unnamed>"))
        if not patterns:
            raise ValueError(f"sync pair {name!r}: catalog_schema_patterns must not be empty")
        for pattern in patterns:
            try:
                _validate_catalog_schema_pattern(pattern)
            except ValueError as exc:
                raise ValueError(
                    f"sync pair {name!r}: catalog_schema_patterns: {exc}"
                ) from exc
        return patterns

    @field_validator("target_space")
    @classmethod
    def _validate_target_space(cls, value: str, info: ValidationInfo) -> str:
        name = str(info.data.get("name", "<unnamed>"))
        if not value.strip():
            raise ValueError(f"sync pair {name!r}: target_space must not be blank")
        return value

    @field_validator("entity_types")
    @classmethod
    def _validate_entity_types(
        cls, value: list[EntityType], info: ValidationInfo
    ) -> list[EntityType]:
        name = str(info.data.get("name", "<unnamed>"))
        if not value:
            raise ValueError(f"sync pair {name!r}: entity_types must not be empty")
        return value

    @field_validator("cadence_seconds")
    @classmethod
    def _validate_cadence(cls, value: int, info: ValidationInfo) -> int:
        name = str(info.data.get("name", "<unnamed>"))
        if value <= 0:
            raise ValueError(f"sync pair {name!r}: cadence_seconds must be positive, got {value}")
        return value

    def matches(self, catalog: str, schema: str) -> bool:
        """True when ``catalog.schema`` is selected by any of this pair's patterns."""
        return any(
            matches_catalog_schema(pattern, catalog, schema)
            for pattern in self.catalog_schema_patterns
        )


# --------------------------------------------------------------------------------------
# Engine settings root
# --------------------------------------------------------------------------------------


class EngineConfig(BaseSettings):
    """The engine's root config: every configured endpoint plus every sync pair.

    Loadable three ways, composable: explicit constructor kwargs (highest precedence),
    a JSON config file via :meth:`load`, and environment variables (``ENDPOINTS`` /
    ``PAIRS`` as whole-field JSON, picked up automatically by ``pydantic-settings`` for
    any field not supplied another way — this is the ordinary ``BaseSettings`` behavior,
    nothing bespoke). See :meth:`load` for the file+env composition.

    Cross-referential validation runs after both ``endpoints`` and ``pairs`` are
    individually well-formed: every pair's ``source``/``target`` must name a real
    endpoint, pair names must be unique, and the v1 direction guardrails apply to every
    pair unconditionally (see the module docstring). All problems found are collected
    and raised together, so fixing a multi-pair config does not take one round trip per
    mistake.
    """

    model_config = SettingsConfigDict(extra="forbid")

    endpoints: dict[str, EndpointConfig] = Field(default_factory=dict)
    pairs: list[SyncPairConfig] = Field(default_factory=list)

    @classmethod
    def load(cls, config_file: str | Path | None = None, **overrides: Any) -> EngineConfig:
        """Build a validated :class:`EngineConfig` from a JSON file, env, and overrides.

        ``config_file``, when given, is a JSON file whose top-level keys are this
        model's fields (``endpoints``, ``pairs``); its values are treated as explicit
        constructor kwargs, so they take precedence over any matching environment
        variable — the same "explicit beats environment" precedence
        ``pydantic-settings`` already applies to ordinary init kwargs. ``overrides``
        layers on top of the file (useful for tests and for a CLI ``--set``-style
        escape hatch) and wins over both. A field present in neither the file nor
        ``overrides`` falls back to its environment variable (or default) the normal
        ``BaseSettings`` way.
        """
        data: dict[str, Any] = {}
        if config_file is not None:
            data = json.loads(Path(config_file).read_text())
        data.update(overrides)
        return cls(**data)

    def resolve_credentials(
        self, backend: SecretBackend | None = None
    ) -> dict[str, dict[str, Any]]:
        """Resolve every endpoint's settings + secrets via ``backend``.

        Defaults to :class:`EnvironmentSecretBackend` when ``backend`` is omitted.
        Returns ``{endpoint_key: EndpointConfig.resolve(...)}`` — each value is ready to
        pass as ``ConnectorConfig.for_endpoint(endpoint_key, **value)``. Raises
        :class:`SecretNotFoundError`, naming the endpoint and secret key, on the first
        secret ``backend`` cannot resolve — this is the "resolves secrets from a
        configured backend ... invalid config fails at startup with a precise message"
        half of the task's definition of done.
        """
        active_backend = backend if backend is not None else EnvironmentSecretBackend()
        return {
            endpoint_key: endpoint.resolve(endpoint_key, active_backend)
            for endpoint_key, endpoint in self.endpoints.items()
        }

    @model_validator(mode="after")
    def _validate_pairs(self) -> EngineConfig:
        errors: list[str] = []
        seen_names: set[str] = set()
        for pair in self.pairs:
            if pair.name in seen_names:
                errors.append(f"sync pair {pair.name!r}: duplicate pair name")
            seen_names.add(pair.name)

            source_endpoint = self.endpoints.get(pair.source)
            if source_endpoint is None:
                errors.append(
                    f"sync pair {pair.name!r}: source endpoint {pair.source!r} is not "
                    f"defined in 'endpoints' (known endpoints: {sorted(self.endpoints)!r})"
                )
            target_endpoint = self.endpoints.get(pair.target)
            if target_endpoint is None:
                errors.append(
                    f"sync pair {pair.name!r}: target endpoint {pair.target!r} is not "
                    f"defined in 'endpoints' (known endpoints: {sorted(self.endpoints)!r})"
                )

            if source_endpoint is not None and source_endpoint.connector == WRITE_CONNECTOR_NAME:
                errors.append(
                    f"sync pair {pair.name!r}: source endpoint {pair.source!r} uses the "
                    f"{WRITE_CONNECTOR_NAME!r} connector, but v1 is upstream-only — Qlik can "
                    "never be a sync source (decision.md guardrail 1)"
                )
            if target_endpoint is not None and target_endpoint.connector != WRITE_CONNECTOR_NAME:
                errors.append(
                    f"sync pair {pair.name!r}: target endpoint {pair.target!r} uses connector "
                    f"{target_endpoint.connector!r}, but the only write target in v1 is "
                    f"{WRITE_CONNECTOR_NAME!r} (decision.md guardrail 1)"
                )

        if errors:
            raise ValueError("; ".join(errors))
        return self
