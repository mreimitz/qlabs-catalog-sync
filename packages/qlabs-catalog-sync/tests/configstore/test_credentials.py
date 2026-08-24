"""No column in the configuration schema can hold a credential value in the clear (C2).

Amended C2 lets the console persist a credential, so the flat "no credential-shaped
column anywhere" reading of this suite no longer describes the schema. What replaced it is
narrower and stronger, and both halves are tested here: exactly one table may hold
credential material, and what it holds is ciphertext.

Three halves:

* :func:`test_no_credential_shaped_column_in_the_config_schema` reflects the six tables
  this task owns from a real migrated database and asserts the credential-shaped-column
  scanner (``configstore_helpers.credential_shaped_columns``) finds nothing except the one
  documented, allowed exception -- ``endpoints.secret_ref``, a named *reference*, never a
  value (C2).
* :func:`test_the_scanner_actually_catches_a_credential_shaped_column` is the dishonest
  case: it runs the *same* scanner against a deliberately "dirty" table with a ``password``
  column and asserts it is caught. Without this half, a scanner that always returned ``[]``
  would make the first test pass for the wrong reason.
* :func:`test_a_stored_credential_is_ciphertext_in_the_database` is the half the amendment
  makes load-bearing. An allowlist entry is a promise; this reads the bytes actually
  written to ``endpoint_secrets`` through a plain SQL query -- the same view a stolen
  database file gives -- and asserts the plaintext is not among them.

Deliberately scoped to the six tables this task adds, not the whole state-store schema:
``qlabs_catalog_sync.state.models.WatermarkRow.watermark_token`` (T2.2, not owned by this
task) is a legitimate opaque resume cursor, not a credential, and would otherwise be a
false positive against the "token" marker.
"""

from __future__ import annotations

from configstore_helpers import credential_shaped_columns
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, create_engine, inspect

from qlabs_catalog_sync.state.db import create_state_engine

CONFIG_TABLES = [
    "endpoints",
    "endpoint_secrets",
    "sync_pairs",
    "selection_rules",
    "selection_overrides",
    "config_generation",
    "config_changes",
]

#: The documented exceptions, and the only ones.
#:
#: * ``endpoints.secret_ref`` -- a named *reference*, never a value (C2 as originally
#:   written, unchanged).
#: * ``endpoint_secrets.ciphertext``/``nonce``/``key_id`` -- amended C2. Credential
#:   material, and the point of the table, but sealed: ``ciphertext`` is AES-256-GCM
#:   output, ``nonce`` is public by design, and ``key_id`` identifies the master key
#:   without deriving from it reversibly (``crypto.key_id_of``). The master key itself has
#:   no column anywhere -- it lives outside the database, which is the entire security
#:   argument. ``test_a_stored_credential_is_ciphertext_in_the_database`` is what stops
#:   this allowlist from being a place to hide a plaintext column.
ALLOWED_CREDENTIAL_ADJACENT_COLUMNS = frozenset(
    {
        ("endpoints", "secret_ref"),
        ("endpoint_secrets", "ciphertext"),
        ("endpoint_secrets", "nonce"),
        ("endpoint_secrets", "key_id"),
    }
)


def test_no_credential_shaped_column_in_the_config_schema(migrated_db_url: str) -> None:
    engine = create_state_engine(migrated_db_url)
    try:
        inspector = inspect(engine)
        hits = credential_shaped_columns(
            inspector, CONFIG_TABLES, allowed=ALLOWED_CREDENTIAL_ADJACENT_COLUMNS
        )
    finally:
        engine.dispose()

    assert hits == []


def test_the_credential_adjacent_allowlist_is_exactly_what_is_documented(
    migrated_db_url: str,
) -> None:
    """Pin the allowlist itself, so widening it is a deliberate edit to this test rather
    than a side effect of adding a table."""
    engine = create_state_engine(migrated_db_url)
    try:
        inspector = inspect(engine)
        columns = {col["name"]: col for col in inspector.get_columns("endpoints")}
    finally:
        engine.dispose()

    assert {table for table, _ in ALLOWED_CREDENTIAL_ADJACENT_COLUMNS} == {
        "endpoints",
        "endpoint_secrets",
    }, "a third table gained a credential-adjacent column; that needs its own argument"
    secret_ref_column = columns["secret_ref"]
    assert secret_ref_column["nullable"] is True
    assert str(secret_ref_column["type"]).startswith("VARCHAR")


def test_the_scanner_actually_catches_a_credential_shaped_column() -> None:
    """The dishonest case: a scanner that always returns [] would make the real test lie."""
    dirty_metadata = MetaData()
    Table(
        "fake_endpoints",
        dirty_metadata,
        Column("id", Integer, primary_key=True),
        Column("connector", String(64)),
        Column("password", String(255)),  # exactly the kind of column this must catch
        Column("enabled", Boolean),
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        dirty_metadata.create_all(engine)
        inspector = inspect(engine)
        hits = credential_shaped_columns(inspector, ["fake_endpoints"])
    finally:
        engine.dispose()

    assert hits == [("fake_endpoints", "password")]


def test_the_scanner_respects_its_allowlist() -> None:
    """A column that *is* allowed is not reported, even though its name matches a marker."""
    dirty_metadata = MetaData()
    Table(
        "fake_endpoints",
        dirty_metadata,
        Column("id", Integer, primary_key=True),
        Column("secret_ref", String(255)),
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        dirty_metadata.create_all(engine)
        inspector = inspect(engine)
        hits = credential_shaped_columns(
            inspector, ["fake_endpoints"], allowed=frozenset({("fake_endpoints", "secret_ref")})
        )
    finally:
        engine.dispose()

    assert hits == []


async def test_a_stored_credential_is_ciphertext_in_the_database(
    migrated_db_url: str,
) -> None:
    """The half the amended C2 rests on, read the way an attacker would read it.

    An allowlist entry is a promise. This one stores a credential through the real service
    and then queries ``endpoint_secrets`` with plain SQL -- the same view a stolen database
    file, a backup or a replica gives -- and asserts the plaintext is not in what comes
    back, nor anywhere else in the file's bytes.
    """
    import base64
    from datetime import UTC, datetime
    from pathlib import Path

    from config_service_helpers import make_registry
    from sqlalchemy import text

    from qlabs_catalog_sync.configstore.crypto import SecretCipher, generate_master_key
    from qlabs_catalog_sync.configstore.service import ConfigService
    from qlabs_catalog_sync.configstore.types import EndpointRole

    plaintext = "a-real-looking-qlik-client-secret-4a91"
    cipher = SecretCipher.from_key(base64.urlsafe_b64decode(generate_master_key()))
    service = ConfigService.from_url(migrated_db_url, make_registry(), cipher=cipher)
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    try:
        await service.create_endpoint(
            name="qlik_acme",
            connector="qlik",
            role=EndpointRole.TARGET,
            settings={"space_id": "acme"},
            secret_ref="db:qlik_acme",
            actor="admin",
            now=now,
        )
        await service.set_endpoint_secret("qlik_acme", "api_key", plaintext, actor="admin", now=now)

        with service.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT endpoint, field, ciphertext, nonce, key_id FROM endpoint_secrets")
            ).all()
    finally:
        await service.aclose()

    assert len(rows) == 1
    row = rows[0]
    assert row.endpoint == "qlik_acme" and row.field == "api_key"
    assert plaintext.encode() not in bytes(row.ciphertext)
    assert plaintext not in repr(rows)

    # And not merely absent from the columns this test happened to select: absent from the
    # whole database file, which is what a stolen backup actually is.
    database_bytes = Path(migrated_db_url.removeprefix("sqlite:///")).read_bytes()
    assert plaintext.encode() not in database_bytes


async def test_the_master_key_itself_has_no_column_anywhere(migrated_db_url: str) -> None:
    """The security argument is that the key lives outside the database. A column holding
    it -- however well-meant, e.g. "cached for convenience" -- would collapse encryption
    into obfuscation, so the schema must have nowhere to put one."""
    engine = create_state_engine(migrated_db_url)
    try:
        inspector = inspect(engine)
        columns = {
            (table, column["name"])
            for table in inspector.get_table_names()
            for column in inspector.get_columns(table)
        }
    finally:
        engine.dispose()

    offenders = {
        (table, column)
        for table, column in columns
        if "master" in column.lower() or column.lower() in {"key", "encryption_key"}
    }
    assert offenders == set()
