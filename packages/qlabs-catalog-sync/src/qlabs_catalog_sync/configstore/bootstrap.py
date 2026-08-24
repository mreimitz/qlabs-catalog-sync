"""Bootstrap import from environment-declared configuration (T10.4, decision C1).

C1: *"On first start the engine seeds them from any environment-declared pairs; from
then on the database is authoritative. RM-01's T2.3 is unchanged and keeps owning
secret backends and environment loading."* This module is that seeding step: it turns
an already-loaded, already-validated :class:`~qlabs_catalog_sync.config.EngineConfig`
(T2.3 -- this module never reads ``os.environ`` or a config file itself) into rows in
the configuration store, writing exclusively through
:class:`~qlabs_catalog_sync.configstore.service.ConfigService` so an environment-seeded
row is validated, audited and generation-bumped exactly like a console write. Nothing
here ever constructs an ``EndpointRow``/``SyncPairRow``/``SelectionRuleRow`` directly.

"First start" is a property of the data, not a flag file
----------------------------------------------------------

The store is considered *not yet seeded* precisely when
``ConfigService.current_generation() == 0``. This is deliberate and exact, not an
approximation:

* **Every** :class:`~qlabs_catalog_sync.configstore.service.ConfigService` write that
  actually changes something bumps the generation counter (T10.3's ``configstore.audit``
  -- a create always bumps; an update/reorder only bumps when it has a real change to
  record). A write that *fails* validation raises before the transaction that would
  bump it is ever entered (see ``_check_pair_direction``, ``_validate_endpoint_settings``
  and friends in ``configstore/service.py``, all called before ``_unit_of_work`` opens),
  so a rejected write leaves the generation untouched. Since ``ConfigService`` is the
  *only* writer of these tables (the hard rule this whole subsystem is built on),
  generation ``0`` is not merely "probably empty" -- it is logically equivalent to "no
  endpoint, pair, rule or override has ever been created", by construction.
* This is why deleting every endpoint through the console on purpose is safe from this
  importer: :meth:`ConfigService.delete_endpoint` bumps the generation too (deletes are
  audited exactly like creates). An operator who empties the store leaves generation at
  whatever it already was (never back to ``0``), so a later restart sees a non-zero
  generation and skips the import outright -- the deletion is never silently reversed,
  and "the database is authoritative" stays true even for "authoritatively empty".
* A flag file (or a boolean column) would need a second source of truth kept in sync
  with the tables it describes; the generation counter *is* the tables' own write
  history, already required to exist for the scheduler-reconciliation half of C1, so
  reusing it here adds no new state and cannot drift from what actually happened.

Partial failure: best-effort persistence, zero tolerance for silence
----------------------------------------------------------------------

By the time an :class:`~qlabs_catalog_sync.config.EngineConfig` reaches this module it
has already passed every check T2.3 can make in memory (every pair's ``source``/
``target`` names a real endpoint, the v1 direction guardrail, glob syntax, non-empty
entity types, ...). What it cannot have checked is anything that depends on *this*
deployment's installed connectors and their own ``ConfigModel`` -- whether
``endpoint.connector`` is actually installed, and whether ``endpoint.settings``
satisfies that connector's stricter, type-checked schema. Those are exactly the checks
:meth:`ConfigService.create_endpoint` already makes, and this module never duplicates
them privately; it just calls the service and reacts to what comes back.

The import proceeds **best-effort**: every endpoint is attempted independently, and one
endpoint failing (connector not installed, settings that do not validate, ...) does not
stop the rest from being tried; every pair is attempted independently the same way, and
so is every selection rule bridged from a pair's patterns. This is the choice that keeps
faith with C1's own framing -- "from then on the database is authoritative" -- for the
*whole* store at once, the moment any single write happens, rather than requiring a
single all-endpoints-and-pairs transaction that ``ConfigService``'s one-transaction-per-
call design does not offer (and should not: each call is audited as its own change).
A rejected item is never retried by a later restart once anything at all has been
imported (the generation is already non-zero by then, see above) -- exactly like a gap
an operator left on purpose, it is the console's job to fill in from there.

"Best-effort" governs what gets *persisted*; it does not mean the outcome is quiet.
:func:`bootstrap_from_environment` returns a :class:`BootstrapReport` naming exactly
what succeeded, and raises :class:`BootstrapPartialFailureError` -- carrying that same
report as :attr:`BootstrapPartialFailureError.report` -- the moment anything did not
import cleanly, whether that is an outright failure or a secret reference this module
refused to guess at (see below). A caller that wants best-effort-without-crashing can
catch the error and read ``.report``; a caller that does nothing special sees the
process fail loudly at startup instead of quietly coming up half-configured, which is
the only acceptable failure mode for a first-run import that just wrote to the
authoritative store.

The secrets shape mismatch: per-field mapping versus one reference
---------------------------------------------------------------------

:attr:`~qlabs_catalog_sync.config.EndpointConfig.secrets` is a **per-field** mapping --
one entry per connector config field, e.g. ``{"client_secret": "CLIENT_SECRET"}``,
resolved by :meth:`EndpointConfig.resolve` as
``backend.get_secret(endpoint=endpoint_key, key=secrets[field])``, i.e. the *value* in
the dict (not the field name) is the backend key, upper-cased, appended to the
normalized endpoint key. :attr:`~qlabs_catalog_sync.configstore.models.EndpointRow.
secret_ref` is a **single** ``"scheme:locator"`` string, resolved by ``configstore.
secrets.resolve_connector_kwargs`` as ``backend.get_secret(endpoint=ref.locator,
key=field.name)`` for *every* secret-typed field the connector's ``ConfigModel``
declares -- the key there is always the field's own name, upper-cased. There is no way
for one ``secret_ref`` to express a per-field key that is not the field's own name.

The two mechanisms agree exactly when every declared per-field key *is* (case-
insensitively) the field's own name -- the convention ``EnvironmentSecretBackend``
already documents and that RM-01's own realistic example config follows
(``{"token": "token"}``, ``{"client_id": "client_id", "client_secret":
"client_secret"}``, see ``tests/config/test_engine_config.py``). When that holds for
*every* entry in an endpoint's ``secrets``, this module emits
``secret_ref = f"env:{endpoint_key}"`` -- the same endpoint key ``EndpointConfig.
resolve`` already used as its own ``endpoint=`` argument, so the two mechanisms read the
exact same environment variables.

When it does **not** hold -- a per-field key naming an arbitrary, unrelated environment
variable, which ``EndpointConfig.secrets`` has always been free to do -- this module
refuses to guess. It imports the endpoint with every other field (connector, settings,
role, enabled) intact and ``secret_ref`` left unset, and records exactly which field(s)
did not fit the convention and what key the single-reference mechanism would actually
read instead, in :attr:`BootstrapReport.secret_ref_skips`. An import that emitted a
plausible-looking ``secret_ref`` here would resolve *something* -- silently the wrong
value, or silently nothing at all if the guessed variable happens not to be set -- and
either is a worse failure mode than an endpoint that visibly has no credential bound
until an operator does it explicitly through the console.

Every write here carries one recognizable actor
--------------------------------------------------

``config_changes.actor`` is non-nullable by schema design specifically so every change
is attributable (``configstore/service.py``'s own docstring). Every row this module
writes carries :data:`BOOTSTRAP_ACTOR`, a fixed, human-legible, unmistakably-not-a-
person string -- never a parameter callers can override -- so "this configuration line
came from the environment at first start, not from an operator" is answerable from the
audit log alone, forever, without cross-referencing anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog

from qlabs_catalog_sync.config import WRITE_CONNECTOR_NAME, EndpointConfig, EngineConfig
from qlabs_catalog_sync.configstore.secrets import SecretRefFormatError
from qlabs_catalog_sync.configstore.service import ConfigService, ConfigServiceError
from qlabs_catalog_sync.configstore.types import ChangeEntityKind, EndpointRole
from qlabs_catalog_sync.discovery import ConnectorLookupError
from qlabs_catalog_sync.selection.rules import object_rules_from_catalog_schema_patterns

__all__ = [
    "BOOTSTRAP_ACTOR",
    "BootstrapPartialFailureError",
    "BootstrapReport",
    "ImportFailure",
    "SecretRefSkipped",
    "bootstrap_from_environment",
]

_logger = structlog.get_logger("qlabs.catalog_sync.configstore.bootstrap")

#: The ``config_changes.actor`` value every row this module writes carries. Fixed and
#: never parameterized -- see the module docstring's last section.
BOOTSTRAP_ACTOR: Final[str] = "environment-bootstrap"


# --------------------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportFailure:
    """One endpoint, sync pair or selection rule that :meth:`ConfigService` refused.

    ``reason`` is the typed error's own message (``str(exc)``) -- already precise about
    what was wrong (see e.g. ``EndpointSettingsValidationError``,
    ``SyncPairEndpointError`` in ``configstore/service.py``), never re-derived here.
    """

    entity_kind: ChangeEntityKind
    key: str
    reason: str

    def explain(self) -> str:
        """e.g. ``"endpoint 'databricks_prod': connector 'databricks' is not
        registered"``."""
        return f"{self.entity_kind.value} {self.key!r}: {self.reason}"


@dataclass(frozen=True, slots=True)
class SecretRefSkipped:
    """One endpoint imported without a ``secret_ref`` because its environment-declared
    per-field ``secrets`` mapping does not follow the single-reference convention.

    See the module docstring's "secrets shape mismatch" section for exactly what the
    convention is and why this module refuses to guess rather than emit a ``secret_ref``
    that would resolve to the wrong value (or nothing).
    """

    endpoint: str
    field: str
    declared_key: str
    expected_key: str

    def explain(self) -> str:
        """e.g. ``"endpoint 'qlik_acme' field 'client_secret': declared key
        'CLIENT_SECRET_V2' does not match the single-reference convention (expected
        'CLIENT_SECRET')"``."""
        return (
            f"endpoint {self.endpoint!r} field {self.field!r}: declared secret key "
            f"{self.declared_key!r} does not match the single-reference convention "
            f"(expected {self.expected_key!r}); imported without a secret_ref -- bind "
            "one through the console"
        )


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """What one :func:`bootstrap_from_environment` call did.

    :param seeded: ``False`` when the store was already seeded (``current_generation()
        != 0``) and this call did nothing at all; every other field is then left at its
        empty default. ``True`` means this call was the first start and actually ran
        the import (which may still have found nothing to complain about, or may not
        have -- see :attr:`failures` / :attr:`secret_ref_skips`).
    """

    seeded: bool
    endpoints_created: tuple[str, ...] = ()
    pairs_created: tuple[str, ...] = ()
    rules_created: int = 0
    secret_ref_skips: tuple[SecretRefSkipped, ...] = ()
    failures: tuple[ImportFailure, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether a run that actually happened found nothing to complain about.

        Vacuously ``True`` when :attr:`seeded` is ``False`` -- a skipped run has nothing
        to be wrong about.
        """
        return not self.failures and not self.secret_ref_skips


class BootstrapPartialFailureError(RuntimeError):
    """Raised when :func:`bootstrap_from_environment` actually ran and found at least
    one problem -- an endpoint/pair/rule that :class:`ConfigService` rejected, or an
    endpoint whose secrets mapping could not be represented as one ``secret_ref``.

    Carries the complete :class:`BootstrapReport` as :attr:`report`, so a caller can
    still see exactly what *did* import cleanly (already durably committed -- see the
    module docstring's "best-effort" section) even though this was raised. This is what
    makes "the outcome must be reported, not swallowed" true even for a caller that does
    nothing but let the exception propagate: the process fails loudly at startup instead
    of coming up quietly half-configured.
    """

    def __init__(self, report: BootstrapReport) -> None:
        problems = [failure.explain() for failure in report.failures] + [
            skip.explain() for skip in report.secret_ref_skips
        ]
        super().__init__(
            f"environment bootstrap import finished with {len(problems)} problem(s): "
            + "; ".join(problems)
        )
        self.report = report


# --------------------------------------------------------------------------------------
# The secrets bridge (per-field EndpointConfig.secrets -> one EndpointRow.secret_ref)
# --------------------------------------------------------------------------------------


def _expected_secret_key(field_name: str) -> str:
    """The backend key the single-reference mechanism will actually read for
    ``field_name`` -- see ``configstore.secrets.resolve_connector_kwargs``."""
    return field_name.upper()


def _endpoint_secret_ref(
    endpoint_key: str, endpoint: EndpointConfig
) -> tuple[str | None, tuple[SecretRefSkipped, ...]]:
    """Bridge one endpoint's per-field ``secrets`` into a single ``secret_ref``, or
    explain why it cannot be, per the module docstring.

    An endpoint with no declared secrets at all needs no reference (``None``, no
    skips). One with every declared key already following the convention gets
    ``env:{endpoint_key}`` -- the same endpoint key ``EndpointConfig.resolve`` itself
    uses, so both mechanisms read the same environment variables. Any mismatch, in
    *any* field, drops the reference entirely rather than emitting a partial or
    wrong one: a ``secret_ref`` resolves every one of the connector's secret fields
    from the same prefix, so there is no way to keep the fields that do match while
    dropping only the ones that do not.
    """
    if not endpoint.secrets:
        return None, ()
    skips = tuple(
        SecretRefSkipped(
            endpoint=endpoint_key,
            field=field_name,
            declared_key=declared_key,
            expected_key=_expected_secret_key(field_name),
        )
        for field_name, declared_key in sorted(endpoint.secrets.items())
        if declared_key.upper() != _expected_secret_key(field_name)
    )
    if skips:
        return None, skips
    return f"env:{endpoint_key}", ()


def _endpoint_role(endpoint: EndpointConfig) -> EndpointRole:
    """Source or target, derived from the connector rather than asked for.

    ``EndpointConfig`` (T2.3) has no ``role`` field -- a pair's direction is validated
    cross-referentially, not declared per endpoint. But v1's direction guardrail
    (enforced unconditionally by ``EngineConfig._validate_pairs``, which this
    ``EngineConfig`` has already passed by the time it reaches this module) makes the
    connector alone sufficient to derive it: the target of every pair is always the
    sole write connector, and the source of every pair is never it. An endpoint that
    happens not to be referenced by any pair still gets an unambiguous answer from its
    connector alone, consistent with what it *would* be the moment a pair used it.
    """
    if endpoint.connector == WRITE_CONNECTOR_NAME:
        return EndpointRole.TARGET
    return EndpointRole.SOURCE


# --------------------------------------------------------------------------------------
# The import
# --------------------------------------------------------------------------------------


async def bootstrap_from_environment(
    service: ConfigService, engine_config: EngineConfig, *, now: datetime
) -> BootstrapReport:
    """Seed the configuration store from ``engine_config`` on first start (C1).

    A no-op, returning ``BootstrapReport(seeded=False)`` without calling ``service``
    again, when the store is already seeded (see the module docstring's "first start"
    section for exactly what that means and why). Otherwise imports every endpoint,
    then every sync pair, then -- for each pair that imported -- one object-scope
    include rule per ``catalog_schema_patterns`` entry (via ``selection.rules.
    object_rules_from_catalog_schema_patterns``, C3's degenerate case), all through
    ``service`` and none of it directly.

    Every write is best-effort per item; see the module docstring's "partial failure"
    section. Raises :class:`BootstrapPartialFailureError` -- after attempting
    everything, never before -- if anything failed to import or any endpoint's secrets
    could not be bridged to a ``secret_ref``; the exception carries the full
    :class:`BootstrapReport` so nothing found out along the way is lost.
    """
    generation = await service.current_generation()
    if generation != 0:
        _logger.info("bootstrap_skipped", reason="already_seeded", generation=generation)
        return BootstrapReport(seeded=False)

    endpoints_created: list[str] = []
    pairs_created: list[str] = []
    secret_ref_skips: list[SecretRefSkipped] = []
    failures: list[ImportFailure] = []
    rules_created = 0

    for endpoint_key, endpoint in engine_config.endpoints.items():
        secret_ref, skips = _endpoint_secret_ref(endpoint_key, endpoint)
        secret_ref_skips.extend(skips)
        try:
            await service.create_endpoint(
                name=endpoint_key,
                connector=endpoint.connector,
                role=_endpoint_role(endpoint),
                settings=dict(endpoint.settings),
                secret_ref=secret_ref,
                # This import knows exactly which reference each endpoint should have, and
                # `None` here means it determined the answer is "none" -- an environment
                # convention it could not map onto a single reference, already recorded as a
                # skip above. Letting create_endpoint substitute its usual "db:<name>" would
                # overwrite that finding with a binding this config never asked for.
                bind_default_secret_ref=False,
                # True, not the schema default of False: an environment-only
                # deployment never had an "enabled" concept before RM-06 -- if it was
                # declared, it was active. Matching that prior behavior (the DoD's "an
                # environment-only deployment comes up with the same behavior as
                # before") means an environment-imported endpoint starts active, not
                # sitting inert until an operator flips it on through the console.
                enabled=True,
                actor=BOOTSTRAP_ACTOR,
                now=now,
            )
        except (ConfigServiceError, ConnectorLookupError, SecretRefFormatError) as exc:
            failures.append(
                ImportFailure(
                    entity_kind=ChangeEntityKind.ENDPOINT, key=endpoint_key, reason=str(exc)
                )
            )
            _logger.error("bootstrap_endpoint_failed", endpoint=endpoint_key, reason=str(exc))
            continue
        endpoints_created.append(endpoint_key)

    for pair in engine_config.pairs:
        try:
            pair_row = await service.create_sync_pair(
                name=pair.name,
                source=pair.source,
                target=pair.target,
                target_space=pair.target_space,
                entity_types=pair.entity_types,
                cadence_seconds=pair.cadence_seconds,
                # jitter_seconds left at its default (None = "use the scheduler's
                # computed default", T2.6 jitter_seconds_for): EngineConfig/
                # SyncPairConfig has no per-pair override field, so an
                # environment-only deployment never had one either.
                manual_edit_policy=pair.manual_edit_policy,
                activation_opt_in=pair.activation_opt_in,
                enabled=True,  # see the endpoint loop's comment above
                actor=BOOTSTRAP_ACTOR,
                now=now,
            )
        except ConfigServiceError as exc:
            failures.append(
                ImportFailure(
                    entity_kind=ChangeEntityKind.SYNC_PAIR, key=pair.name, reason=str(exc)
                )
            )
            _logger.error("bootstrap_pair_failed", pair=pair.name, reason=str(exc))
            continue
        pairs_created.append(pair.name)

        for rule in object_rules_from_catalog_schema_patterns(pair.catalog_schema_patterns):
            try:
                await service.create_selection_rule(
                    pair_id=pair_row.id,
                    scope=rule.scope,
                    decision=rule.decision,
                    matcher_kind=rule.matcher_kind,
                    pattern=rule.pattern,
                    ordinal=rule.ordinal,
                    actor=BOOTSTRAP_ACTOR,
                    now=now,
                )
            except ConfigServiceError as exc:
                failures.append(
                    ImportFailure(
                        entity_kind=ChangeEntityKind.SELECTION_RULE,
                        key=f"{pair.name}:{rule.pattern}",
                        reason=str(exc),
                    )
                )
                _logger.error(
                    "bootstrap_rule_failed", pair=pair.name, pattern=rule.pattern, reason=str(exc)
                )
                continue
            rules_created += 1

    report = BootstrapReport(
        seeded=True,
        endpoints_created=tuple(endpoints_created),
        pairs_created=tuple(pairs_created),
        rules_created=rules_created,
        secret_ref_skips=tuple(secret_ref_skips),
        failures=tuple(failures),
    )
    if not report.ok:
        raise BootstrapPartialFailureError(report)
    _logger.info(
        "bootstrap_seeded",
        endpoints=len(endpoints_created),
        pairs=len(pairs_created),
        rules=rules_created,
    )
    return report
