"""Authenticated encryption for credentials the console stores (amended C2).

C2 originally said the console never persists a secret value: an endpoint held a
``secret_ref`` and the value lived in the process environment. That works for a
single-tenant container whose environment is written once at deploy time, and it does not
work at all for the deployment this product is actually for -- an operator standing up a
new *client's* endpoints in the browser. A credential that can only arrive through
``.env`` means editing a file on the host and restarting the service for every client
added, which defeats the console-first premise C1 states outright. Endpoint configuration
belongs in the configuration database, and a credential is endpoint configuration.

So it moves into the database, and this module is what makes that safe to do.

**What is protected, and from what.** Every stored credential is sealed with AES-256-GCM
under one key held *outside* the database. A stolen database file, a backup, a replica, a
``.sql`` dump, or anyone with read access to the ``endpoint_secrets`` table gets
ciphertext and nothing else. What this does **not** defend against is an attacker who has
the master key *and* the database -- that is the definition of the trust boundary, not an
oversight. Keep the key off the same medium as the backups.

**One key per installation, not per client.** The master key is deployment configuration:
set once, at install time, by whoever runs the service. It is never touched again when a
client is added -- that is the whole point, and the difference between this and the
environment backend it replaces. There is no way to encrypt without a key that lives
somewhere other than the ciphertext, so "no configuration outside the database at all" is
not on the menu; one install-scoped value is the floor.

**Where the key comes from, and why nobody has to supply one.** :func:`resolve_master_key`
reads, in order:

* ``QLABS_SECRET_KEY_FILE`` -- a path to a file holding the key.
* ``QLABS_SECRET_KEY`` -- the key inline.
* **otherwise, a key file beside the service's own database, created on first use.**

The third case is the default, and it is deliberate. Requiring an operator to generate and
install a key before they can save their first credential is one more setup step between
"I want to add a client" and "the client is added" -- exactly the kind of step this whole
change exists to delete. The service makes its own key the first time it needs one, writes
it ``0600``, and says so once in the log.

**What that costs, stated plainly.** A key sitting next to the database is weaker than a key
somewhere else: anyone who can read both files can read every credential. It still defends
the cases that actually happen most -- a database dump, a ``.sql`` export, a replica, a
backup copied without its directory, a query-level leak -- because none of those carry the
key file. A deployment that wants the stronger separation sets ``QLABS_SECRET_KEY_FILE`` to
a mounted secret and gets it, with no other change. That is the trade this default makes:
zero setup by default, full separation available, and the difference written down here
rather than implied.

Every form carries the same thing: 32 bytes, urlsafe-base64 encoded, as
``scripts/make_secret_key.py`` prints. Reading the environment directly here is the same
deliberate, scoped exception ``config.EnvironmentSecretBackend`` documents for itself: this
module *is* the settings machinery for the master key, so it is the one place allowed to
look.

**Binding, so a ciphertext cannot be moved.** Each value is sealed with additional
authenticated data of ``"<endpoint>\\x00<field>"``. Copying the ``client_secret``
ciphertext from a staging endpoint's row onto a production endpoint's row -- a plain
``UPDATE`` by anyone with write access to the table -- produces a decryption failure
rather than a silently working credential swap. GCM authenticates that AAD; it is not
stored, it is recomputed from the row's own identity at decrypt time.

**Key identity, so a wrong key says so.** Each row records ``key_id``, a short digest of
the key that sealed it (:func:`key_id_of`, an HMAC over a fixed label rather than a hash
of the key itself, so the id reveals nothing usable about the key). Booting with the wrong
key then produces "this secret was sealed with a different master key" instead of an
authentication-tag failure that reads like data corruption, and it is what makes key
rotation legible later: rows are re-sealed one at a time and their ``key_id`` says which
have been done.

**Nothing here logs, returns, or reprs a plaintext.** :func:`decrypt` returns a pydantic
``SecretStr``, redacted from ``repr``/``str``/``model_dump`` by pydantic and from
structured logs by the SDK's ``logging.redact_secrets`` (T1.7). :class:`EncryptedSecret`
has no field capable of holding a plaintext.
"""

from __future__ import annotations

import base64
import hmac
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final

import structlog
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

_LOG = structlog.get_logger("qlabs.catalog_sync.configstore.crypto")

__all__ = [
    "MASTER_KEY_ENV_VAR",
    "MASTER_KEY_FILE_ENV_VAR",
    "default_key_path_for",
    "ensure_key_file",
    "resolve_master_key",
    "EncryptedSecret",
    "MasterKeyError",
    "MasterKeyMissingError",
    "MasterKeyUnreadableError",
    "SecretCipher",
    "SecretDecryptionError",
    "generate_master_key",
    "key_id_of",
    "load_master_key",
]

#: The master key inline, urlsafe-base64 of 32 raw bytes.
MASTER_KEY_ENV_VAR: Final[str] = "QLABS_SECRET_KEY"

#: A path to a file holding the same thing. Checked first -- see the module docstring for
#: why a file is the better of the two.
MASTER_KEY_FILE_ENV_VAR: Final[str] = "QLABS_SECRET_KEY_FILE"

#: AES-256. Not configurable: a key length that varies per deployment is a way to end up
#: with a weak one, and there is no threat model here that AES-128 fits and AES-256 does not.
KEY_BYTES: Final[int] = 32

#: GCM's standard nonce length. 96 bits is what the mode is specified around; a random
#: nonce per encryption is safe at any volume this store will ever see (a credential is
#: written by hand, by an operator, not in a loop).
NONCE_BYTES: Final[int] = 12

#: Fixed label the key id is derived over. Constant on purpose: ``key_id_of`` must return
#: the same id for the same key forever, or a row's recorded id stops matching the key
#: that actually sealed it.
_KEY_ID_LABEL: Final[bytes] = b"qlabs-catalog-sync/master-key-id/v1"

#: Bytes of the derived id kept. Eight bytes is far too little to attack the key through
#: and plenty to tell two keys apart in a log line or a table.
_KEY_ID_BYTES: Final[int] = 8


class MasterKeyError(Exception):
    """Base for every "this deployment's master key is not usable" failure."""


class MasterKeyMissingError(MasterKeyError):
    """No master key is configured, but a stored credential needs one.

    Names both variables and the generator script, because this is the error a fresh
    deployment hits first and the fix is one command.
    """

    def __init__(self) -> None:
        super().__init__(
            "no master key is configured, so stored credentials cannot be read or written: "
            f"set {MASTER_KEY_FILE_ENV_VAR} to a file holding the key, or {MASTER_KEY_ENV_VAR} "
            "to the key itself. Generate one with: "
            "uv run python scripts/make_secret_key.py"
        )


class MasterKeyUnreadableError(MasterKeyError):
    """A master key is configured but cannot be used -- unreadable file, not valid
    base64, or the wrong length. Names what was wrong, never the value read."""

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"the master key from {source} is unusable: {reason}")
        self.source = source


class SecretDecryptionError(Exception):
    """A stored credential could not be opened.

    ``endpoint``/``field`` name the row; no part of this carries plaintext or ciphertext.
    ``sealed_with``/``opened_with`` are key *ids*, which is what makes the common cause --
    the service is running with a different master key than the one that wrote the row --
    say so plainly instead of surfacing as an authentication-tag failure.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        field: str,
        reason: str,
        sealed_with: str | None = None,
        opened_with: str | None = None,
    ) -> None:
        message = f"the stored credential for endpoint {endpoint!r}, field {field!r} {reason}"
        if sealed_with is not None and opened_with is not None:
            message = f"{message} (sealed with key {sealed_with}, opened with key {opened_with})"
        super().__init__(message)
        self.endpoint = endpoint
        self.field = field


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    """One sealed credential, exactly as the ``endpoint_secrets`` row stores it.

    Deliberately minimal, and deliberately incapable of holding a plaintext: there is no
    field here a decrypted value could be assigned to, so "the ciphertext row accidentally
    carried the value" is prevented by the type rather than by care.
    """

    ciphertext: bytes
    nonce: bytes
    key_id: str


def generate_master_key() -> str:
    """A fresh, correctly-sized master key, urlsafe-base64 encoded.

    Used by ``scripts/make_secret_key.py`` and by tests. ``os.urandom`` rather than
    ``secrets.token_bytes`` only because the latter is a thin wrapper over it; both are
    the OS CSPRNG.
    """
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode("ascii")


def key_id_of(key: bytes) -> str:
    """A short, stable, non-reversing identifier for ``key``.

    An HMAC over a fixed label keyed by the master key, truncated -- rather than a plain
    digest *of* the key, which would hand an attacker a target to grind offline. The id
    goes in database rows and error messages, so it must be safe in both.
    """
    digest = hmac.new(key, _KEY_ID_LABEL, sha256).digest()
    return digest[:_KEY_ID_BYTES].hex()


def load_master_key(environ: dict[str, str] | None = None) -> bytes:
    """Load and validate this deployment's master key.

    ``QLABS_SECRET_KEY_FILE`` wins over ``QLABS_SECRET_KEY`` when both are set: a
    deployment that mounts a secret file and also has a stale inline value in its
    environment means the file, and silently preferring the environment there would open
    every credential with the wrong key.

    Raises :class:`MasterKeyMissingError` when neither is set, and
    :class:`MasterKeyUnreadableError` when what is set cannot be turned into
    :data:`KEY_BYTES` bytes. Neither error ever includes the value it read.
    """
    env = os.environ if environ is None else environ

    path_value = (env.get(MASTER_KEY_FILE_ENV_VAR) or "").strip()
    if path_value:
        source = f"{MASTER_KEY_FILE_ENV_VAR} ({path_value})"
        try:
            encoded = Path(path_value).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise MasterKeyUnreadableError(
                source, f"the file could not be read ({exc.strerror})"
            ) from exc
        return _decode_key(encoded, source)

    inline_value = (env.get(MASTER_KEY_ENV_VAR) or "").strip()
    if inline_value:
        return _decode_key(inline_value, MASTER_KEY_ENV_VAR)

    raise MasterKeyMissingError()


#: Name of the key file the service creates for itself beside its own database. Leading dot
#: and an obvious name: an operator who lists that directory should be able to tell at a
#: glance that the file matters and must be backed up separately from the database.
DEFAULT_KEY_FILENAME: Final[str] = ".qlabs-secret.key"


def default_key_path_for(database_url: object) -> Path:
    """Where this deployment's key file lives when nothing configured one.

    Beside the SQLite database the service already writes to, because that directory is
    definitionally somewhere the process can write and somewhere the operator already knows
    about. For a non-SQLite URL there is no such directory, so it falls back to the working
    directory -- a Postgres deployment is past the point where an auto-generated key is the
    right answer anyway, and should be setting ``QLABS_SECRET_KEY_FILE``.
    """
    # Ask the URL object for its own database path when it is one, rather than slicing the
    # string: SQLAlchemy spells an absolute SQLite path with four slashes and a relative one
    # with three, and getting that wrong puts the key somewhere other than beside the database
    # -- which reads as "it generated a second key" the next time the service starts.
    database = getattr(database_url, "database", None)
    get_backend_name = getattr(database_url, "get_backend_name", None)
    is_sqlite = (
        get_backend_name() == "sqlite"
        if callable(get_backend_name)
        else str(database_url).startswith("sqlite:")
    )
    if is_sqlite and isinstance(database, str) and database and database != ":memory:":
        return Path(database).parent / DEFAULT_KEY_FILENAME

    text = str(database_url)
    if text.startswith("sqlite:///") and not text.endswith(":memory:"):
        return Path(text.removeprefix("sqlite:///")).parent / DEFAULT_KEY_FILENAME
    return Path(DEFAULT_KEY_FILENAME)


def ensure_key_file(path: Path) -> bytes:
    """Read the key at ``path``, creating one if it is not there yet.

    Written ``0600`` before anything is put in it -- created restricted, never created
    world-readable and then tightened, which would leave a window where it was not. The
    write is atomic (a temporary file in the same directory, then ``replace``) so a crash
    mid-write cannot leave a truncated key that silently fails to open every credential.

    Concurrent first starts are handled by re-reading after a lost race rather than
    overwriting: two processes generating two keys and the second winning would strand
    every credential the first one had already sealed.
    """
    if path.exists():
        return _decode_key(path.read_text(encoding="utf-8").strip(), f"the key file at {path}")

    generated = generate_master_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.touch(mode=0o600)
        temporary.write_text(generated, encoding="utf-8")
        # os.link + unlink rather than replace: link fails if the target already exists, so a
        # process that lost the race leaves the winner's key in place instead of clobbering it.
        try:
            os.link(temporary, path)
        except FileExistsError:
            return _decode_key(
                path.read_text(encoding="utf-8").strip(), f"the key file at {path}"
            )
    finally:
        temporary.unlink(missing_ok=True)

    _LOG.warning(
        "configstore.master_key_generated",
        path=str(path),
        detail=(
            "created a new credential-encryption key beside the database. Back it up "
            "separately from the database: losing it means every stored credential must be "
            "entered again, and keeping both in one place means neither is protected from "
            "whoever holds that copy. Set QLABS_SECRET_KEY_FILE to a mounted secret to keep "
            "them apart."
        ),
    )
    return _decode_key(generated, f"the key file at {path}")


def resolve_master_key(database_url: object) -> bytes:
    """This deployment's master key: configured if it was, generated on first use if not.

    The order is the module docstring's: an explicitly configured key always wins, so a
    deployment that has separated its key never silently falls back to a generated one
    sitting next to its database.
    """
    env = os.environ
    if (env.get(MASTER_KEY_FILE_ENV_VAR) or "").strip() or (
        env.get(MASTER_KEY_ENV_VAR) or ""
    ).strip():
        return load_master_key()
    return ensure_key_file(default_key_path_for(database_url))


def _decode_key(encoded: str, source: str) -> bytes:
    try:
        # validate=True, and b64decode rather than urlsafe_b64decode: the urlsafe variant
        # takes no `validate` argument and silently discards characters outside the
        # alphabet, so a typo'd key decodes to plausible-looking garbage and the operator
        # is told the length is wrong rather than that the value is not a key at all.
        key = base64.b64decode(_pad_base64(encoded), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise MasterKeyUnreadableError(
            source, "it is not valid urlsafe-base64 (generate one with scripts/make_secret_key.py)"
        ) from exc
    if len(key) != KEY_BYTES:
        raise MasterKeyUnreadableError(
            source, f"it decodes to {len(key)} bytes; {KEY_BYTES} are required"
        )
    return key


def _pad_base64(encoded: str) -> str:
    """Restore ``=`` padding a shell, an editor, or a copy-paste may have eaten.

    Rejecting an otherwise-correct key over missing padding is a pure own-goal: the key
    material is intact, and the operator has no way to tell what went wrong.
    """
    return encoded + "=" * (-len(encoded) % 4)


@dataclass(frozen=True, slots=True)
class SecretCipher:
    """Seals and opens credentials under one master key.

    Constructed once per service and reused: ``AESGCM`` does per-call key scheduling
    anyway, so this holds the raw key and its id rather than a cipher object, which keeps
    the type trivially copyable and thread-safe.
    """

    key: bytes
    key_id: str

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> SecretCipher:
        """Build from :func:`load_master_key` -- configured key only, never generated."""
        key = load_master_key(environ)
        return cls(key=key, key_id=key_id_of(key))

    @classmethod
    def for_database(cls, database_url: object) -> SecretCipher:
        """Build from :func:`resolve_master_key`: the configured key, or one created beside
        ``database_url`` on first use. This is what the running service uses, and the reason
        saving a credential needs no setup step at all."""
        key = resolve_master_key(database_url)
        return cls(key=key, key_id=key_id_of(key))

    @classmethod
    def from_key(cls, key: bytes) -> SecretCipher:
        """Build from raw key bytes -- for tests, and for a future key-management path
        that does not go through the environment."""
        if len(key) != KEY_BYTES:
            raise MasterKeyUnreadableError(
                "the supplied key", f"it is {len(key)} bytes; {KEY_BYTES} are required"
            )
        return cls(key=key, key_id=key_id_of(key))

    def encrypt(self, value: str, *, endpoint: str, field: str) -> EncryptedSecret:
        """Seal ``value`` for one endpoint's one field.

        The ``(endpoint, field)`` pair is bound in as additional authenticated data, so
        the result only ever opens in the row it was written for -- see the module
        docstring on why a ciphertext that can be moved between rows is a real problem and
        not a theoretical one.
        """
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(self.key).encrypt(
            nonce, value.encode("utf-8"), _binding(endpoint=endpoint, field=field)
        )
        return EncryptedSecret(ciphertext=ciphertext, nonce=nonce, key_id=self.key_id)

    def decrypt(self, sealed: EncryptedSecret, *, endpoint: str, field: str) -> SecretStr:
        """Open ``sealed``, or raise :class:`SecretDecryptionError` naming only the row.

        A ``key_id`` mismatch is reported before decryption is even attempted, because
        "you are running with a different master key" and "this row is corrupt or was
        tampered with" are different operational problems with different fixes, and GCM
        gives the same ``InvalidTag`` for both.
        """
        if sealed.key_id != self.key_id:
            raise SecretDecryptionError(
                endpoint=endpoint,
                field=field,
                reason="was sealed with a different master key than this service is running with",
                sealed_with=sealed.key_id,
                opened_with=self.key_id,
            )
        try:
            plaintext = AESGCM(self.key).decrypt(
                sealed.nonce, sealed.ciphertext, _binding(endpoint=endpoint, field=field)
            )
        except InvalidTag as exc:
            raise SecretDecryptionError(
                endpoint=endpoint,
                field=field,
                reason=(
                    "failed authentication: it is corrupt, or it was moved here from a "
                    "different endpoint or field"
                ),
            ) from exc
        return SecretStr(plaintext.decode("utf-8"))


def _binding(*, endpoint: str, field: str) -> bytes:
    """The additional authenticated data one credential is sealed under.

    ``NUL`` as the separator rather than ``:`` or ``.`` so no pair of (endpoint, field)
    values can produce the same binding as a different pair: endpoint names and field
    names are both ordinary identifiers and neither can contain a ``NUL``.
    """
    return f"{endpoint}\x00{field}".encode()
