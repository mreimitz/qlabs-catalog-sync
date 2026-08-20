"""Row factories and the credential-shaped-column scanner shared by configstore tests.

Named ``configstore_helpers`` rather than ``helpers``/``conftest_utils`` -- this repo
collects tests with pytest's ``importlib`` import mode over one flat module namespace,
and both of those basenames have already collided across packages.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.engine import Inspector

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


__all__ = [
    "CREDENTIAL_NAME_MARKERS",
    "LATER",
    "NOW",
    "credential_shaped_columns",
    "make_endpoint",
    "make_sync_pair",
    "sample_manual_edit_policy",
]
