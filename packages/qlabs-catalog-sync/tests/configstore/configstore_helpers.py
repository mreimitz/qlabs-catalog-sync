"""Row factories, the credential scanner, and the model/migration parity checker shared
by configstore tests.

Named ``configstore_helpers`` rather than ``helpers``/``conftest_utils`` -- this repo
collects tests with pytest's ``importlib`` import mode over one flat module namespace,
and both of those basenames have already collided across packages.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, MetaData, UniqueConstraint
from sqlalchemy.engine import Dialect, Inspector

from qlabs_catalog_sync.config import ManualEditMode, ManualEditPolicy
from qlabs_catalog_sync.configstore.models import EndpointRow, SyncPairRow
from qlabs_catalog_sync.configstore.types import EndpointRole
from qlabs_catalog_sync_sdk.models import EntityType

#: A fixed instant every test builds rows against, so assertions compare exact values
#: instead of "close enough" timestamps.
NOW: datetime = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
LATER: datetime = datetime(2026, 8, 21, 9, 30, 0, tzinfo=UTC)


def make_endpoint(
    name: str = "qlik_acme",
    *,
    connector: str = "qlik",
    role: EndpointRole = EndpointRole.TARGET,
    secret_ref: str | None = "env:QLIK_ACME",
    settings: dict[str, object] | None = None,
    enabled: bool = False,
    created_at: datetime = NOW,
    updated_at: datetime = NOW,
) -> EndpointRow:
    """An :class:`EndpointRow` with sensible defaults, overridable per test."""
    return EndpointRow(
        name=name,
        connector=connector,
        role=role,
        secret_ref=secret_ref,
        settings=settings if settings is not None else {"space_id": "personal"},
        enabled=enabled,
        created_at=created_at,
        updated_at=updated_at,
    )


def make_sync_pair(
    name: str = "databricks-to-qlik",
    *,
    source: str = "databricks_prod",
    target: str = "qlik_acme",
    target_space: str = "personal",
    entity_types: list[EntityType] | None = None,
    cadence_seconds: int = 900,
    jitter_seconds: float | None = None,
    manual_edit_policy: ManualEditPolicy | None = None,
    activation_opt_in: bool = False,
    enabled: bool = False,
    created_at: datetime = NOW,
    updated_at: datetime = NOW,
) -> SyncPairRow:
    """A :class:`SyncPairRow` with sensible defaults, overridable per test."""
    return SyncPairRow(
        name=name,
        source=source,
        target=target,
        target_space=target_space,
        entity_types=entity_types if entity_types is not None else [EntityType.DATA_PRODUCT],
        cadence_seconds=cadence_seconds,
        jitter_seconds=jitter_seconds,
        manual_edit_policy=(
            manual_edit_policy if manual_edit_policy is not None else ManualEditPolicy()
        ),
        activation_opt_in=activation_opt_in,
        enabled=enabled,
        created_at=created_at,
        updated_at=updated_at,
    )


def sample_manual_edit_policy() -> ManualEditPolicy:
    """A non-default :class:`ManualEditPolicy` exercising every field, for round-trip tests."""
    return ManualEditPolicy(
        default=ManualEditMode.SOURCE_WINS,
        per_entity={EntityType.GLOSSARY_TERM: ManualEditMode.PRESERVE_LOCAL},
        per_field={"data_product.description": ManualEditMode.PRESERVE_LOCAL},
    )


# --------------------------------------------------------------------------------------
# The credential-shaped-column scanner. A real function over a real reflected schema, not
# a decorative assertion -- see tests/configstore/test_credentials.py, which runs this
# both against the actual configuration schema (must come back empty) and against a
# deliberately "dirty" ad-hoc table with a `password` column (must come back non-empty),
# so a future column named/typed to hold a credential fails the same way in either case.
# --------------------------------------------------------------------------------------

#: Substrings (case-insensitive) that mark a column name as credential-shaped. Broad on
#: purpose: "secret" alone catches secret_value/secret/client_secret/etc., "token" catches
#: access_token/refresh_token/api_token/etc.
CREDENTIAL_NAME_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "credential",
    "token",
    "private_key",
    "apikey",
    "api_key",
)


def credential_shaped_columns(
    inspector: Inspector,
    table_names: list[str],
    *,
    allowed: frozenset[tuple[str, str]] = frozenset(),
) -> list[tuple[str, str]]:
    """``[(table, column), ...]`` for every column whose name looks credential-shaped.

    ``allowed`` is the explicit, documented exception list -- for the real schema, exactly
    ``{("endpoints", "secret_ref")}`` (a named *reference*, never a value; C2). Anything
    else that matches is reported, regardless of the column's declared type, because a
    JSON or Text column is just as capable of holding a credential value as a String one.
    """
    hits: list[tuple[str, str]] = []
    for table in table_names:
        for column in inspector.get_columns(table):
            column_name = str(column["name"])
            lowered = column_name.lower()
            is_credential_shaped = any(marker in lowered for marker in CREDENTIAL_NAME_MARKERS)
            if is_credential_shaped and (table, column_name) not in allowed:
                hits.append((table, column_name))
    return hits


# --------------------------------------------------------------------------------------
# Model <-> migration schema parity. A hand-written Alembic migration and the ORM models
# it is supposed to match are two independent descriptions of one schema; nothing stops
# them drifting apart except a test that actually diffs them. The ORM round-trip tests
# exercise the columns they happen to touch and no more -- a column added to
# ``configstore/models.py`` and forgotten in ``0002_config_store.py`` (or the reverse)
# would pass every one of them silently. This is the check that catches it, table by
# table: every column name (both directions), nullability, and the column's *dialect-
# compiled* type string (``Column.type.compile(dialect=...)``, which unwraps a
# ``TypeDecorator`` to what SQLite actually stores -- e.g. ``StrEnumType`` compiles to
# ``VARCHAR(n)``, ``Uuid`` to ``CHAR(32)`` -- rather than the generic, dialect-less
# ``str(type)``, which does not match what ``sqlalchemy.inspect`` reports back), plus
# every unique/check constraint name.
# --------------------------------------------------------------------------------------


def schema_parity_report(
    model_metadata: MetaData,
    inspector: Inspector,
    table_names: Iterable[str],
) -> list[str]:
    """Human-readable mismatches between ``model_metadata``'s tables and what
    ``inspector`` reflects from a real, migrated database, restricted to
    ``table_names``. An empty list means the two agree exactly; see the module-level
    comment above for exactly what "agree" checks.
    """
    problems: list[str] = []
    reflected_table_names = set(inspector.get_table_names())
    dialect: Dialect = inspector.dialect

    for table_name in table_names:
        model_table = model_metadata.tables.get(table_name)
        if model_table is None:
            problems.append(f"{table_name}: expected in model_metadata, not found there")
            continue
        if table_name not in reflected_table_names:
            problems.append(f"{table_name}: expected in the reflected database, not found there")
            continue

        model_columns = {column.name: column for column in model_table.columns}
        reflected_columns = {
            str(column["name"]): column for column in inspector.get_columns(table_name)
        }

        for missing in sorted(set(model_columns) - set(reflected_columns)):
            problems.append(
                f"{table_name}.{missing}: declared in the ORM model, missing from the migration"
            )
        for extra in sorted(set(reflected_columns) - set(model_columns)):
            problems.append(
                f"{table_name}.{extra}: created by the migration, missing from the ORM model"
            )

        for name in sorted(set(model_columns) & set(reflected_columns)):
            model_column = model_columns[name]
            reflected_column = reflected_columns[name]

            model_nullable = bool(model_column.nullable)
            reflected_nullable = bool(reflected_column["nullable"])
            if model_nullable != reflected_nullable:
                problems.append(
                    f"{table_name}.{name}: nullable mismatch "
                    f"(model={model_nullable!r}, migration={reflected_nullable!r})"
                )

            model_type = model_column.type.compile(dialect=dialect)
            reflected_type = str(reflected_column["type"])
            if model_type != reflected_type:
                problems.append(
                    f"{table_name}.{name}: type mismatch "
                    f"(model={model_type!r}, migration={reflected_type!r})"
                )

        model_unique_names = {
            constraint.name
            for constraint in model_table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        reflected_unique_names = {
            str(uc["name"]) for uc in inspector.get_unique_constraints(table_name)
        }
        for missing in sorted(model_unique_names - reflected_unique_names):
            problems.append(
                f"{table_name}: unique constraint {missing!r} in the model, "
                "missing from the migration"
            )
        for extra in sorted(reflected_unique_names - model_unique_names):
            problems.append(
                f"{table_name}: unique constraint {extra!r} in the migration, "
                "missing from the model"
            )

        model_check_names = {
            constraint.name
            for constraint in model_table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        reflected_check_names = {
            str(ck["name"]) for ck in inspector.get_check_constraints(table_name)
        }
        for missing in sorted(model_check_names - reflected_check_names):
            problems.append(
                f"{table_name}: check constraint {missing!r} in the model, "
                "missing from the migration"
            )
        for extra in sorted(reflected_check_names - model_check_names):
            problems.append(
                f"{table_name}: check constraint {extra!r} in the migration, missing from the model"
            )

    return problems


__all__ = [
    "CREDENTIAL_NAME_MARKERS",
    "LATER",
    "NOW",
    "credential_shaped_columns",
    "make_endpoint",
    "make_sync_pair",
    "sample_manual_edit_policy",
    "schema_parity_report",
]
