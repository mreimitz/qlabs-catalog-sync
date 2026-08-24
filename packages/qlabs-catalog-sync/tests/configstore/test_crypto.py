"""Credentials stored in the configuration database are unreadable without the key.

The amended C2 lets the console persist a credential, which is only defensible because of
what this module guarantees. Each property below is one half of that:

* **A stolen database is not a stolen credential.** What lands in the row is ciphertext,
  and no part of the plaintext survives in it.
* **A ciphertext cannot be moved between rows.** Copying one endpoint's sealed
  ``client_secret`` onto another endpoint -- an ordinary ``UPDATE`` for anyone with write
  access to the table -- must fail, not silently swap a credential.
* **The wrong master key says so.** Booting against a database sealed by a different key
  is an operational mistake with an obvious fix, and it must read as one rather than as
  data corruption.
* **No error ever carries key or plaintext material**, since these are the errors the
  console shows an operator.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from qlabs_catalog_sync.configstore.crypto import (
    KEY_BYTES,
    MASTER_KEY_ENV_VAR,
    MASTER_KEY_FILE_ENV_VAR,
    MasterKeyMissingError,
    MasterKeyUnreadableError,
    SecretCipher,
    SecretDecryptionError,
    generate_master_key,
    key_id_of,
    load_master_key,
)

_PLAINTEXT = "dapi-a-real-looking-databricks-secret"


@pytest.fixture
def cipher() -> SecretCipher:
    return SecretCipher.from_key(base64.urlsafe_b64decode(generate_master_key()))


def test_a_sealed_credential_round_trips(cipher: SecretCipher) -> None:
    sealed = cipher.encrypt(_PLAINTEXT, endpoint="databricks", field="client_secret")

    opened = cipher.decrypt(sealed, endpoint="databricks", field="client_secret")

    assert opened.get_secret_value() == _PLAINTEXT


def test_what_lands_in_the_row_contains_no_plaintext(cipher: SecretCipher) -> None:
    """The whole reason this module exists: a database dump leaks nothing."""
    sealed = cipher.encrypt(_PLAINTEXT, endpoint="databricks", field="client_secret")

    row_bytes = sealed.ciphertext + sealed.nonce + sealed.key_id.encode()

    assert _PLAINTEXT.encode() not in row_bytes
    # Not even a fragment: a mode that leaked a prefix would still pass the check above.
    assert not any(_PLAINTEXT[:chunk].encode() in row_bytes for chunk in range(4, len(_PLAINTEXT)))


def test_the_same_value_seals_differently_every_time(cipher: SecretCipher) -> None:
    """A per-value random nonce, so two endpoints sharing one credential are not
    detectable as sharing it by comparing their rows."""
    first = cipher.encrypt(_PLAINTEXT, endpoint="a", field="client_secret")
    second = cipher.encrypt(_PLAINTEXT, endpoint="a", field="client_secret")

    assert first.ciphertext != second.ciphertext
    assert first.nonce != second.nonce


def test_a_ciphertext_moved_to_another_endpoint_will_not_open(cipher: SecretCipher) -> None:
    """Binding, tested as the attack it prevents: an ``UPDATE`` that copies staging's
    sealed credential onto the production endpoint's row."""
    sealed = cipher.encrypt(_PLAINTEXT, endpoint="staging", field="client_secret")

    with pytest.raises(SecretDecryptionError) as excinfo:
        cipher.decrypt(sealed, endpoint="production", field="client_secret")

    assert "production" in str(excinfo.value)
    assert _PLAINTEXT not in str(excinfo.value)


def test_a_ciphertext_moved_to_another_field_will_not_open(cipher: SecretCipher) -> None:
    """The other half of the binding: a connector with two secret fields (Databricks has
    ``client_secret`` and ``token``) must not accept one in the other's place."""
    sealed = cipher.encrypt(_PLAINTEXT, endpoint="databricks", field="token")

    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(sealed, endpoint="databricks", field="client_secret")


def test_a_tampered_ciphertext_will_not_open(cipher: SecretCipher) -> None:
    sealed = cipher.encrypt(_PLAINTEXT, endpoint="databricks", field="client_secret")
    flipped = bytes([sealed.ciphertext[0] ^ 0x01]) + sealed.ciphertext[1:]

    with pytest.raises(SecretDecryptionError):
        cipher.decrypt(
            type(sealed)(ciphertext=flipped, nonce=sealed.nonce, key_id=sealed.key_id),
            endpoint="databricks",
            field="client_secret",
        )


def test_the_wrong_master_key_is_reported_as_the_wrong_key(cipher: SecretCipher) -> None:
    """Running the service against a database sealed by a different key is an ordinary
    deployment mistake. It must not surface as "corrupt": the fix is to supply the right
    key, and the message has to point there."""
    sealed = cipher.encrypt(_PLAINTEXT, endpoint="databricks", field="client_secret")
    other = SecretCipher.from_key(base64.urlsafe_b64decode(generate_master_key()))

    with pytest.raises(SecretDecryptionError) as excinfo:
        other.decrypt(sealed, endpoint="databricks", field="client_secret")

    message = str(excinfo.value)
    assert "different master key" in message
    assert sealed.key_id in message and other.key_id in message


def test_a_key_id_does_not_expose_the_key() -> None:
    """The id goes into database rows and error messages, so it must be safe in both: an
    HMAC over a fixed label, not a digest of the key itself."""
    encoded = generate_master_key()
    key = base64.urlsafe_b64decode(encoded)

    identifier = key_id_of(key)

    assert identifier == key_id_of(key), "the id must be stable for a given key"
    assert key.hex() not in identifier
    assert encoded.strip("=") not in identifier
    assert len(identifier) < len(key.hex()), "the id must not be the key material itself"


def test_a_key_file_is_preferred_over_the_inline_variable(tmp_path: Path) -> None:
    """A deployment that mounts a secret file and also carries a stale inline value means
    the file. Silently preferring the environment there opens every credential with the
    wrong key."""
    file_key = generate_master_key()
    key_file = tmp_path / "master.key"
    key_file.write_text(file_key)

    loaded = load_master_key(
        {MASTER_KEY_FILE_ENV_VAR: str(key_file), MASTER_KEY_ENV_VAR: generate_master_key()}
    )

    assert loaded == base64.urlsafe_b64decode(file_key)


def test_a_key_with_stripped_padding_still_loads() -> None:
    """A shell, an editor or a copy-paste can eat ``=`` padding. The key material is
    intact and the operator has no way to see what went wrong, so refusing here is a pure
    own-goal."""
    encoded = generate_master_key()

    loaded = load_master_key({MASTER_KEY_ENV_VAR: encoded.rstrip("=")})

    assert loaded == base64.urlsafe_b64decode(encoded)


def test_no_key_at_all_names_both_variables_and_the_generator() -> None:
    """The first error a fresh deployment hits. The fix is one command, so the message
    has to carry it."""
    with pytest.raises(MasterKeyMissingError) as excinfo:
        load_master_key({})

    message = str(excinfo.value)
    assert MASTER_KEY_ENV_VAR in message
    assert MASTER_KEY_FILE_ENV_VAR in message
    assert "make_secret_key.py" in message


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("not-base64-at-all-!!!", "urlsafe-base64"),
        (base64.urlsafe_b64encode(b"too-short").decode(), "bytes"),
    ],
)
def test_an_unusable_key_says_what_is_wrong_without_echoing_it(value: str, expected: str) -> None:
    with pytest.raises(MasterKeyUnreadableError) as excinfo:
        load_master_key({MASTER_KEY_ENV_VAR: value})

    message = str(excinfo.value)
    assert expected in message
    assert value not in message, "the error echoed the configured key material"


def test_a_generated_key_is_the_right_size() -> None:
    assert len(base64.urlsafe_b64decode(generate_master_key())) == KEY_BYTES


def test_a_key_is_created_on_first_use_so_nothing_has_to_be_configured(tmp_path: Path) -> None:
    """The reason saving a credential needs no setup step at all.

    Requiring an operator to generate and install a key before their first credential can be
    saved is one more thing between "add a client" and "the client is added". The service
    makes its own the first time it needs one.
    """
    from qlabs_catalog_sync.configstore.crypto import ensure_key_file

    key_file = tmp_path / "sub" / ".qlabs-secret.key"

    key = ensure_key_file(key_file)

    assert len(key) == KEY_BYTES
    assert key_file.exists()
    # Restricted from the moment it exists -- created 0600, never created world-readable and
    # then tightened, which would leave a window where it was not.
    assert key_file.stat().st_mode & 0o077 == 0, "the key file is readable by other users"


def test_an_existing_key_is_reused_not_replaced(tmp_path: Path) -> None:
    """A second key would strand every credential the first one sealed, so a restart must
    never generate one over the top of an existing file."""
    from qlabs_catalog_sync.configstore.crypto import ensure_key_file

    key_file = tmp_path / ".qlabs-secret.key"

    first = ensure_key_file(key_file)
    second = ensure_key_file(key_file)

    assert first == second


def test_a_configured_key_always_beats_the_generated_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that has separated its key must never silently fall back to one sitting
    next to its database -- that would quietly undo the separation it asked for."""
    from qlabs_catalog_sync.configstore.crypto import resolve_master_key

    configured = generate_master_key()
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, configured)

    resolved = resolve_master_key(f"sqlite:///{tmp_path / 'state.db'}")

    assert resolved == base64.urlsafe_b64decode(configured)
    assert not (tmp_path / ".qlabs-secret.key").exists(), "it generated a key it did not need"


def test_the_generated_key_lands_beside_the_database(tmp_path: Path) -> None:
    """Beside the database the service already writes to: a directory that is definitionally
    writable and that the operator already knows about."""
    from qlabs_catalog_sync.configstore.crypto import default_key_path_for

    assert (
        default_key_path_for(f"sqlite:///{tmp_path / 'state.db'}")
        == tmp_path / ".qlabs-secret.key"
    )
