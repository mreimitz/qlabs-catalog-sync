"""Row factories, the credential scanner, and the model/migration parity checker shared
by run-history tests, plus a target connector that manufactures decision D2/D3-shaped
unresolved fields on a real write.

Named ``run_history_helpers`` rather than ``helpers`` -- this repo collects tests with
pytest's ``importlib`` import mode over one flat module namespace, and that basename has
already collided across packages (``tests/identity/helpers.py``). The parity checker and
the credential scanner are intentionally not imported from
``tests/configstore/configstore_helpers.py``: this package does not own that file, and a
test-only helper module is not a stable, cross-package API.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import CheckConstraint, MetaData, UniqueConstraint
from sqlalchemy.engine import Dialect, Inspector

from qlabs_catalog_sync_sdk.contract import WriteOutcome, WriteResult
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    FieldDiff,
    IdentityRef,
    NeutralEntity,
    Party,
    PartyRole,
    Tag,
    TextField,
)
from qlabs_catalog_sync_sdk.testing import FakeConnector, qlik_shaped_manifest

#: A fixed instant every test builds rows against, so assertions compare exact values
#: instead of "close enough" timestamps.
NOW: datetime = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
LATER: datetime = datetime(2026, 8, 21, 9, 30, 0, tzinfo=UTC)


def data_product(
    name: str,
    *,
    description: str | None = None,
    tags: Sequence[tuple[str, str | None]] = (),
    owners: Sequence[str] = (),
) -> DataProduct:
    """A neutral data product with just enough shape to produce a real, multi-field diff."""
    return DataProduct(
        name=name,
        description=None if description is None else TextField.plain(description),
        tags=[Tag(key=key, value=value) for key, value in tags],
        owners=[Party(email=email, role=PartyRole.OWNER) for email in owners],
    )


def seed_product(connector: FakeConnector, native_key: str, **kwargs: object) -> IdentityRef:
    """Seed one data product into ``connector`` under an explicit native key.

    Native keys are Unity Catalog paths (``catalog.schema``) because that is what decision
    D1's ``catalog.schema`` selector is applied to.
    """
    name = str(kwargs.pop("name", native_key.split(".")[-1]))
    return connector.seed(data_product(name, **kwargs), native_key=native_key)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Model <-> migration schema parity. Same approach (and the same reasoning) as
# tests/configstore/configstore_helpers.py's schema_parity_report -- duplicated rather
# than imported, see the module docstring.
# --------------------------------------------------------------------------------------


def schema_parity_report(
    model_metadata: MetaData,
    inspector: Inspector,
    table_names: Iterable[str],
) -> list[str]:
    """Human-readable mismatches between ``model_metadata``'s tables and what
    ``inspector`` reflects from a real, migrated database, restricted to
    ``table_names``. An empty list means the two agree exactly on every column name
    (both directions), nullability, dialect-compiled type, and unique/check constraint
    name.
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
                f"{table_name}: check constraint {extra!r} in the migration, "
                "missing from the model"
            )

    return problems


# --------------------------------------------------------------------------------------
# The credential-shaped-column scanner. Same markers and approach as
# tests/configstore/configstore_helpers.py's credential_shaped_columns.
# --------------------------------------------------------------------------------------

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
    """``[(table, column), ...]`` for every column whose name looks credential-shaped."""
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
# A target connector that reports a decision D2/D3-shaped unresolved field on every
# write -- an owner email or dataset member the target could not resolve, "omitted and
# reported, never invented" (WriteResult's own docstring). FakeConnector's own
# skipped_fields channel only ever reports fields that already matched
# (WriteOutcome.NO_OP / the unchanged half of an update); this is the other reason that
# channel exists, exercised so a run-history test proves the recorder's handling of it
# with a genuine WriteResult rather than a hand-built RecordReport.
# --------------------------------------------------------------------------------------


class UnresolvedRefTarget(FakeConnector):
    """A Qlik-shaped write target whose every create/update also reports one field the
    target claims it could not resolve -- simulating an unresolved dataset member (D2)
    or an owner email matching no Qlik user (D3)."""

    name: ClassVar[str] = "fake-target"

    def __init__(self, *, unresolved_field: str = "owners", **kwargs: Any) -> None:
        super().__init__(manifest=qlik_shaped_manifest(), **kwargs)
        self.unresolved_field = unresolved_field

    async def create(self, entity: NeutralEntity) -> WriteResult:
        result = await super().create(entity)
        return result.model_copy(
            update={
                "skipped_fields": [*result.skipped_fields, self.unresolved_field],
                "detail": f"{self.unresolved_field}: no matching Qlik user/member found",
            }
        )

    async def update(self, ref: IdentityRef, diff: FieldDiff) -> WriteResult:
        result = await super().update(ref, diff)
        if result.outcome is WriteOutcome.NO_OP:
            # A no-op write cannot report written fields, but is free to report an
            # additional skipped one -- the model only forbids the former.
            return result.model_copy(
                update={"skipped_fields": [*result.skipped_fields, self.unresolved_field]}
            )
        return result.model_copy(
            update={
                "skipped_fields": [*result.skipped_fields, self.unresolved_field],
                "detail": f"{self.unresolved_field}: no matching Qlik user/member found",
            }
        )
