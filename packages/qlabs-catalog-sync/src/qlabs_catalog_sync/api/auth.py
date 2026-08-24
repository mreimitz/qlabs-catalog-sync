"""Administrator authentication for the console and the API (C7).

WP12 / T12.2. Decision C7 in full: *"Single administrator, credential supplied by the
environment, failing closed. One operator identity, a credential provided through the
environment or a secret manager, a hashed comparison, an ``HttpOnly`` ``SameSite``
session cookie, and a CSRF token on every mutating request. If no credential is
configured the console does not serve. OIDC, multiple users and a role model are
deferred."* There is exactly one identity here; nothing in this module has, or should
grow, a concept of users, groups, roles or scopes.

What the operator configures
----------------------------

**A password hash by preference.** The configured value is a self-describing PHC-style
scrypt string produced by :func:`hash_password`::

    $scrypt$ln=16,r=8,p=2$<salt-b64>$<hash-b64>

A hash rather than the password because the password puts a directly reusable credential
into ``docker inspect``, the pod spec, the CI variable store and ``/proc/self/environ``,
where every process in the container can read it. A hash there is not directly reusable,
and anyone who *can* read it already holds the deployment's secrets. The cost is that the
operator needs a step to produce the hash, so :func:`hash_password` is a plain function
they can run without the password ever entering shell history — ``scripts/make_admin_hash.py``
prints the line to configure::

    uv run python scripts/make_admin_hash.py

**A plaintext password where that reasoning does not apply.** That whole argument is about
an environment that is a shared secret store. It is not about a container on the operator's
own laptop, where the environment holds nothing anyone else can reach and the setup step is
pure friction. So :data:`ADMIN_PASSWORD_KEY` — ``QLABS_CONSOLE_ADMIN__PASSWORD`` — takes
the password itself and :func:`load_admin_credential` hashes it once at startup, producing a
credential identical to the configured-hash one. It is deliberately the second choice, not
an equal alternative: the hash wins when both are set, and taking this path logs
``console.auth.plaintext_password`` at ``warning`` every startup so a deployment that
reached for it by accident says so in its own logs. Neither path applies a password-strength
policy: any non-empty password is accepted, because the person configuring this console is
the person who has to sign in to it.

Both keys are read through the existing
:class:`~qlabs_catalog_sync.config.SecretBackend` — the same abstraction endpoint
credentials use (T2.3, C2) — under the fixed endpoint key :data:`ADMIN_SECRET_ENDPOINT`,
so the shipped :class:`~qlabs_catalog_sync.config.EnvironmentSecretBackend` reads them
from ``QLABS_CONSOLE_ADMIN__PASSWORD_HASH`` / ``QLABS_CONSOLE_ADMIN__PASSWORD`` and a
future Vault backend resolves the same references without this module changing. This
module never reads ``os.environ`` itself: "a reference to a credential, resolved through a
backend" already has one implementation in this codebase and does not get a second.

Failing closed
--------------

:func:`load_admin_credential` **raises** :class:`AuthNotConfiguredError` when no hash is
configured, and logs ``console.auth.not_configured`` once, at that moment. It is a
startup-only function — no request path calls it — so "once at start" is structural
rather than a promise. The process therefore refuses to start rather than starting and
serving only ``/healthz``: a container that boots but cannot do its job is the worst of
the three outcomes, because the liveness probe keeps passing and the deployment looks
healthy while the console is permanently unusable. Exiting non-zero with one fatal log
line surfaces as ``CrashLoopBackOff`` (or a non-zero ``docker run``) with the reason in
the logs, which is unambiguous. The liveness probe is unaffected either way: it never
gets a chance to run against a process that refused to start, and once a credential *is*
configured ``/healthz`` answers 200 without credentials, exactly as it did before this
task (see :data:`PUBLIC_PROBE_PATHS`).

Default-deny, and the one deliberate exception
----------------------------------------------

Authentication is an ASGI middleware, not a per-route dependency, so a route added by a
later task (T12.3-T12.7) is protected because it exists, not because its author
remembered to opt in. **Everything under** :data:`~qlabs_catalog_sync.api.app.API_PREFIX`
**needs a session**, unconditionally, decided before routing — so a path no route claims
refuses exactly like one that exists, and an anonymous caller learns nothing about which
routes there are. Outside that prefix the app serves the console's own static assets,
where the exemptions live:

* :data:`PUBLIC_PROBE_PATHS` (``/healthz``, ``/metrics``) — the health surface, and a
  Prometheus scraper that has no session and no way to acquire one. ``/metrics``'s
  exemption is a deliberate, documented exposure of operational data (pair and endpoint
  names, counters) and can be turned off with ``ConsoleAuth(metrics_public=False)``.
* ``POST {api_prefix}/auth/session`` — sign-in itself, the one thing that cannot already
  have a session.
* Any other **safe-method** request outside the prefix — the SPA shell and its assets
  (``static.py``). An unauthenticated browser opening ``/endpoints/42`` must receive the
  console shell so the SPA can render a sign-in screen; a JSON 401 would render as raw
  text in the address bar, and the bundle has to be fetchable to draw that screen at all.
  FastAPI's own ``/openapi.json``, ``/docs`` and ``/redoc`` are carved back **out** of
  this allowance by :func:`install_auth`, which reads their real paths off the app: they
  describe the administrative API and no anonymous browser needs them.

The cost of that last allowance is the one rule this module cannot enforce from inside:
a future route registered *outside* ``api_prefix`` would be readable without a session.
``tests/api/test_auth.py`` enumerates every registered path and fails when one appears
outside the prefix that is not on the short documented list, so adding one is a decision
somebody has to make on purpose rather than an accident.

Every refusal is the shared :class:`~qlabs_catalog_sync.api.errors.ErrorModel` as JSON, so
the console gets a typed ``code`` rather than an untyped ``catch``.

Sessions and CSRF
-----------------

Sessions are held server-side, keyed by the SHA-256 of an opaque 256-bit token, and the
cookie carries only that token. Stateless signed cookies were rejected for one reason:
C7's sign-out has to actually invalidate, and a signed cookie can only be un-signed by a
denylist that is itself server state. Sign-out deletes the record; the cookie stops
working immediately. Sessions have an absolute expiry (:data:`DEFAULT_SESSION_TTL`) and
are pruned on every lookup.

The CSRF token is generated per session and stored **on the session record**, so it is
bound to that session by construction — another valid session's token fails, and there is
no global constant to leak. It is handed to the SPA in the sign-in response body (and
again from ``GET {api_prefix}/auth/session`` after a reload) and presented in the
:data:`CSRF_HEADER` request header on every ``POST``/``PUT``/``PATCH``/``DELETE``.
Sign-in itself cannot present one — there is no session yet — and is protected instead by
``SameSite=Lax`` plus a JSON request body, which an HTML form cannot forge cross-site.

What never leaves this module
-----------------------------

The password, the configured hash, the session token and the CSRF token appear in no log
record, no error message, no exception traceback and no OpenAPI schema. Parse failures
never echo the value they failed on — a value in ``QLABS_CONSOLE_ADMIN__PASSWORD_HASH``
that fails to parse is quite likely a password someone pasted into the wrong variable, so
every parser here raises ``from None`` with a message that describes the *expected* shape
only. ``tests/api/test_auth.py`` proves this with sentinels through the real structlog
processors, the same technique ``tests/configstore/test_secrets.py`` uses.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import secrets as stdlib_secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, SecretStr

from qlabs_catalog_sync.config import (
    EnvironmentSecretBackend,
    SecretBackend,
    SecretNotFoundError,
)
from qlabs_catalog_sync.observability import get_logger

from .errors import API_ERROR_RESPONSES, APIError, ErrorModel
from .static import path_is_under_api_prefix

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

_LOG = get_logger("qlabs.catalog_sync.api.auth")

__all__ = [
    "ADMIN_PASSWORD_HASH_KEY",
    "ADMIN_PASSWORD_KEY",
    "ADMIN_SECRET_ENDPOINT",
    "ADMIN_USERNAME_KEY",
    "AUTH_SESSION_ROUTE",
    "CSRF_HEADER",
    "DEFAULT_ADMIN_USERNAME",
    "DEFAULT_SCRYPT_PARAMS",
    "DEFAULT_SESSION_TTL",
    "MAX_KDF_MEMORY_BYTES",
    "PUBLIC_PROBE_PATHS",
    "SESSION_COOKIE",
    "SESSION_COOKIE_SECURE",
    "AdminCredential",
    "AuthConfigurationError",
    "AuthNotConfiguredError",
    "ConsoleAuth",
    "ConsoleSession",
    "ScryptParams",
    "SessionInfo",
    "SignInRequest",
    "console_auth_from_environment",
    "current_session",
    "hash_password",
    "install_auth",
    "load_admin_credential",
    "require_session",
    "schema_and_docs_paths",
]


# --------------------------------------------------------------------------------------
# Configuration surface
# --------------------------------------------------------------------------------------

#: The :class:`~qlabs_catalog_sync.config.SecretBackend` ``endpoint`` the administrator
#: credential is filed under. With the shipped
#: :class:`~qlabs_catalog_sync.config.EnvironmentSecretBackend` this normalizes to the
#: ``QLABS_CONSOLE_ADMIN`` environment-variable prefix, so the hash is read from
#: ``QLABS_CONSOLE_ADMIN__PASSWORD_HASH``, the plaintext-password alternative from
#: ``QLABS_CONSOLE_ADMIN__PASSWORD``, and the (optional) username from
#: ``QLABS_CONSOLE_ADMIN__USERNAME``.
ADMIN_SECRET_ENDPOINT: Final[str] = "qlabs_console_admin"

#: Backend key holding the PHC-style scrypt hash :func:`hash_password` produces. The
#: preferred way to configure the credential; see :data:`ADMIN_PASSWORD_KEY` for the
#: opt-in alternative and why it is second choice.
ADMIN_PASSWORD_HASH_KEY: Final[str] = "password_hash"

#: Backend key holding the administrator's password *in the clear*, hashed once at startup
#: by :meth:`AdminCredential.from_password`. The convenience path for a deployment where
#: the environment is not a shared secret store -- a local container, a developer machine --
#: read from ``QLABS_CONSOLE_ADMIN__PASSWORD`` by the shipped backend. It is second choice,
#: not equivalent: the value here *is* a directly reusable credential wherever the
#: environment is readable, so :func:`load_admin_credential` prefers
#: :data:`ADMIN_PASSWORD_HASH_KEY` when both are set and warns whenever it falls back to
#: this one.
ADMIN_PASSWORD_KEY: Final[str] = "password"

#: Backend key holding the administrator's username. Optional; defaults to
#: :data:`DEFAULT_ADMIN_USERNAME`. Read through the same backend as the hash rather than
#: through a second, separate environment reader — C7 has exactly one identity, so its
#: two attributes come from one place.
ADMIN_USERNAME_KEY: Final[str] = "username"

#: Username assumed when the backend holds no :data:`ADMIN_USERNAME_KEY`.
DEFAULT_ADMIN_USERNAME: Final[str] = "admin"

#: There is deliberately **no password-strength policy** here — no minimum length, no
#: character classes. This console is an internal, operator-facing tool for one
#: administrator, run by the person who chose the password; a length floor here does not
#: stop a bad password, it only stops the operator from using the one they wanted, and
#: they route around it in ways nobody can see. Choosing the password is theirs.
#:
#: The one value :func:`hash_password` still refuses is the empty string, which is not a
#: weak password but the absence of one: the console's own sign-in form marks the field
#: ``required``, so an empty configured password produces a credential nobody can ever
#: sign in with. Refusing it at startup turns a silent lockout into a message, and catches
#: the far more common cause -- a ``QLABS_CONSOLE_ADMIN__PASSWORD=`` line with nothing
#: after the ``=``.

#: Absolute session lifetime. Not sliding: a session created now stops working in eight
#: hours regardless of activity, which is a working day for the one operator this console
#: has and bounds how long a stolen cookie is worth anything.
DEFAULT_SESSION_TTL: Final[timedelta] = timedelta(hours=8)

#: Request header carrying the per-session CSRF token on a mutating request.
CSRF_HEADER: Final[str] = "X-CSRF-Token"

#: Session cookie name on a plain-HTTP origin.
SESSION_COOKIE: Final[str] = "qlabs_console_session"

#: Session cookie name on a TLS origin. The ``__Host-`` prefix is enforced by the
#: browser: it only accepts the cookie when it is ``Secure``, ``Path=/`` and has no
#: ``Domain``, which is what stops a sibling subdomain (``anything.example.com``) from
#: tossing a cookie onto ``console.example.com``. The name has to differ because the
#: prefix cannot be used without ``Secure``, and ``Secure`` cannot be used on a plain-HTTP
#: deployment without the browser silently dropping the cookie and nobody being able to
#: sign in. Both names are accepted on read; which one is *set* follows the request scheme.
SESSION_COOKIE_SECURE: Final[str] = f"__Host-{SESSION_COOKIE}"

#: Paths that answer without a session: the health surface, exactly where T2.7 put it and
#: where ``tests/api/test_serve_single_origin.py`` probes it. Written out here rather than
#: imported because ``api/app.py`` registers them as literals too; ``tests/api/test_auth.py``
#: fails if either drifts, because it asserts both answer 200 unauthenticated on an
#: authenticated app.
PUBLIC_PROBE_PATHS: Final[frozenset[str]] = frozenset({"/healthz", "/metrics"})

#: Sign-in / current-session / sign-out, relative to the API prefix
#: :func:`install_auth` is given. One path, three methods: ``POST`` creates a session,
#: ``GET`` describes it, ``DELETE`` ends it.
AUTH_SESSION_ROUTE: Final[str] = "/auth/session"

_SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})
_MUTATING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SESSION_SCOPE_KEY: Final[str] = "qlabs_console_session"

#: Hard ceiling on the memory one password verification may ask scrypt for. The
#: parameters come from the *stored hash*, which means a malformed or hostile value could
#: otherwise request an arbitrary allocation on an unauthenticated code path.
MAX_KDF_MEMORY_BYTES: Final[int] = 256 * 1024 * 1024

#: Most sessions that may exist at once. One administrator does not need more, and this
#: bounds the store against an operator (or a script) signing in repeatedly. The oldest
#: session is evicted when the cap is reached.
_MAX_SESSIONS: Final[int] = 32


class AuthConfigurationError(RuntimeError):
    """The administrator credential is configured but unusable.

    Its message describes the *expected* shape only. It never contains, quotes or
    summarizes the offending value: a value that failed to parse as a password hash is
    quite plausibly a password pasted into the wrong variable, and this exception is
    printed by whatever refused to start.
    """


class AuthNotConfiguredError(AuthConfigurationError):
    """No administrator credential is configured at all, so nothing may serve (C7)."""


# --------------------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScryptParams:
    """scrypt cost parameters, carried inside the encoded hash so they can be raised
    without a code change: re-run :func:`hash_password` with different parameters and the
    verifier honours whatever the new hash declares.

    The defaults are ``N=2**16, r=8, p=2`` — 64 MiB and roughly 160 ms per verification.
    That is one of OWASP's listed scrypt configurations, chosen over the more commonly
    quoted ``N=2**17, r=8, p=1`` because the two cost the same CPU work while this one
    needs half the memory, and this KDF runs inside the engine's own container on an
    unauthenticated code path. :data:`MAX_KDF_MEMORY_BYTES` and the floor on ``log_n``
    bound it from both directions.
    """

    log_n: int = 16
    r: int = 8
    p: int = 2
    dklen: int = 32

    def __post_init__(self) -> None:
        if not 14 <= self.log_n <= 20:
            raise AuthConfigurationError("scrypt log2(N) must be between 14 and 20")
        if not 1 <= self.r <= 32:
            raise AuthConfigurationError("scrypt r must be between 1 and 32")
        if not 1 <= self.p <= 16:
            raise AuthConfigurationError("scrypt p must be between 1 and 16")
        if not 16 <= self.dklen <= 64:
            raise AuthConfigurationError("scrypt output length must be between 16 and 64 bytes")
        if 128 * self.r * self.n > MAX_KDF_MEMORY_BYTES:
            raise AuthConfigurationError(
                "scrypt parameters would need more than "
                f"{MAX_KDF_MEMORY_BYTES // (1024 * 1024)} MiB of memory per verification"
            )

    @property
    def n(self) -> int:
        """The CPU/memory cost parameter ``N``, i.e. ``2 ** log_n``."""
        return 1 << self.log_n

    @property
    def maxmem(self) -> int:
        """The ``maxmem`` ceiling handed to :func:`hashlib.scrypt`.

        OpenSSL refuses outright rather than allocating past this, so it is computed from
        the parameters (``128 * r * (N + p + 2)``, its own accounting) plus a megabyte of
        slack instead of being left at the library default, which is smaller than the
        defaults here need.
        """
        return 128 * self.r * (self.n + self.p + 2) + (1 << 20)

    def encode(self) -> str:
        """The ``ln=..,r=..,p=..`` segment of the encoded hash."""
        return f"ln={self.log_n},r={self.r},p={self.p}"


#: Parameters :func:`hash_password` uses when the caller does not choose. See
#: :class:`ScryptParams` for why these and not OWASP's more commonly quoted pair.
DEFAULT_SCRYPT_PARAMS: Final[ScryptParams] = ScryptParams()

_SALT_BYTES: Final[int] = 16
_HASH_SCHEME: Final[str] = "scrypt"


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    try:
        return base64.b64decode(encoded + padding, validate=True)
    except (binascii.Error, ValueError):
        raise AuthConfigurationError(
            "administrator password hash is not valid base64 in one of its segments"
        ) from None


def _derive(password: str, params: ScryptParams, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        dklen=params.dklen,
        maxmem=params.maxmem,
    )


def hash_password(password: str, *, params: ScryptParams = DEFAULT_SCRYPT_PARAMS) -> str:
    """Encode ``password`` as the PHC-style scrypt string the deployment configures.

    This is operator tooling and startup code, never a request path: run it once, put the
    result in ``QLABS_CONSOLE_ADMIN__PASSWORD_HASH`` (or the equivalent secret-manager
    entry), and the password itself never has to exist anywhere the deployment can read it.
    :meth:`AdminCredential.from_password` calls it once at startup for deployments that
    configure the password instead. The salt is fresh per call, so hashing the same
    password twice gives two different strings and neither reveals that they match.

    Any non-empty password is accepted, of any length or shape — see the note above
    :data:`ADMIN_SECRET_ENDPOINT`'s neighbours on why there is no strength policy. Raises
    :class:`AuthConfigurationError` — with a message that never echoes the input — only
    for the empty string.
    """
    if not password:
        raise AuthConfigurationError(
            "administrator password must not be empty (the sign-in form requires one, so "
            "an empty password is a credential nobody could ever sign in with)"
        )
    salt = stdlib_secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, params, salt)
    return f"${_HASH_SCHEME}${params.encode()}${_b64encode(salt)}${_b64encode(digest)}"


def _parse_password_hash(encoded: str) -> tuple[ScryptParams, bytes, bytes]:
    """Split an encoded hash into its parameters, salt and digest.

    Every failure path raises :class:`AuthConfigurationError` ``from None``. That is
    deliberate: ``int("hunter2")`` and friends put the offending fragment into their own
    message, and the offending fragment here may well *be* a password.
    """
    parts = encoded.split("$")
    if len(parts) != 5 or parts[0] != "":
        raise AuthConfigurationError(
            "administrator password hash must look like "
            "'$scrypt$ln=<n>,r=<r>,p=<p>$<salt>$<hash>' (produce one with "
            "qlabs_catalog_sync.api.auth.hash_password)"
        )
    _, scheme, raw_params, raw_salt, raw_digest = parts
    if scheme != _HASH_SCHEME:
        raise AuthConfigurationError(
            f"administrator password hash uses an unsupported scheme; expected {_HASH_SCHEME!r}"
        )

    values: dict[str, int] = {}
    for item in raw_params.split(","):
        name, separator, value = item.partition("=")
        # ``isascii()`` is load-bearing, not decoration: ``"²".isdigit()`` is True
        # while ``int("²")`` raises a ValueError that quotes the offending fragment.
        # An unguarded raise there would put part of the configured value -- possibly a
        # password -- into a traceback. The length cap keeps a 10,000-digit "parameter"
        # from reaching ``int()`` at all, which raises for its own, equally chatty reason.
        if not separator or not value.isascii() or not value.isdigit() or len(value) > 6:
            raise AuthConfigurationError(
                "administrator password hash has a malformed parameter segment; expected "
                "'ln=<n>,r=<r>,p=<p>' with short decimal values"
            ) from None
        values[name] = int(value)
    if set(values) != {"ln", "r", "p"}:
        raise AuthConfigurationError(
            "administrator password hash must declare exactly the parameters ln, r and p"
        )

    salt = _b64decode(raw_salt)
    digest = _b64decode(raw_digest)
    if not 8 <= len(salt) <= 64:
        raise AuthConfigurationError(
            "administrator password hash has a salt outside the accepted 8-64 byte range"
        )
    params = ScryptParams(log_n=values["ln"], r=values["r"], p=values["p"], dklen=len(digest))
    return params, salt, digest


@dataclass(frozen=True, slots=True, repr=False)
class AdminCredential:
    """The single administrator identity (C7): a username and a verifier for a password.

    Holds no password and no reusable secret — only the salt and derived key from the
    configured hash — and its ``repr`` deliberately shows neither, so a credential caught
    in a traceback, a pytest assertion diff or a structured log field cannot expose the
    material an offline attack would need.
    """

    username: str
    params: ScryptParams
    salt: bytes = field(repr=False)
    digest: bytes = field(repr=False)

    def __repr__(self) -> str:
        return f"AdminCredential(username={self.username!r}, params={self.params!r})"

    @classmethod
    def from_password_hash(cls, encoded: str, *, username: str) -> AdminCredential:
        """Build a credential from an encoded hash, raising :class:`AuthConfigurationError`
        — never quoting ``encoded`` — for anything malformed, unsupported or below the
        accepted cost floor."""
        cleaned_username = username.strip()
        if not cleaned_username:
            raise AuthConfigurationError("administrator username must not be blank")
        params, salt, digest = _parse_password_hash(encoded.strip())
        return cls(username=cleaned_username, params=params, salt=salt, digest=digest)

    @classmethod
    def from_password(
        cls, password: str, *, username: str, params: ScryptParams = DEFAULT_SCRYPT_PARAMS
    ) -> AdminCredential:
        """Build a credential by hashing ``password`` here and now, with a fresh salt.

        The credential this returns is indistinguishable from one built by
        :meth:`from_password_hash` — same derivation, same constant-time
        :meth:`verify` — and the plaintext is not retained. What differs is only where the
        plaintext lived beforehand: see :data:`ADMIN_PASSWORD_KEY`.

        Raises :class:`AuthConfigurationError` — with a message that never echoes the
        input — for an empty password.
        """
        return cls.from_password_hash(
            hash_password(password, params=params), username=username
        )

    def verify(self, *, username: str, password: str) -> bool:
        """``True`` when ``username``/``password`` are the configured administrator's.

        Both comparisons run through :func:`hmac.compare_digest`, and the key derivation
        runs **before** the username is looked at, so a wrong username and a wrong
        password take the same path and cost the same work — there is no early return to
        turn into an account-existence oracle. Both results are computed before either is
        combined; ``and`` here operates on two ``bool`` values that already exist, so it
        short-circuits nothing.

        Synchronous and CPU-bound by design; :meth:`ConsoleAuth.sign_in` is what keeps it
        off the event loop.
        """
        derived = _derive(password, self.params, self.salt)
        password_ok = hmac.compare_digest(derived, self.digest)
        username_ok = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        return password_ok and username_ok


def load_admin_credential(*, backend: SecretBackend | None = None) -> AdminCredential:
    """Read the administrator credential through ``backend``, or refuse (C7).

    ``backend`` defaults to :class:`~qlabs_catalog_sync.config.EnvironmentSecretBackend`,
    the same one endpoint credentials use, so the credential lives in the environment today
    and in a secret manager the day a Vault backend exists, with no change here.

    Two keys can carry it. :data:`ADMIN_PASSWORD_HASH_KEY` holds a hash and is preferred;
    :data:`ADMIN_PASSWORD_KEY` holds a password in the clear, is hashed here at startup,
    and is logged as ``console.auth.plaintext_password`` at ``warning`` every time it is
    used. When both are set the hash wins — resolving it the other way would let a stray
    ``QLABS_CONSOLE_ADMIN__PASSWORD`` in a shell profile silently replace a deployment's
    real credential.

    Raises :class:`AuthNotConfiguredError` when neither is configured, and
    :class:`AuthConfigurationError` when what is configured cannot be used. Both are
    logged once, here, at the moment they are raised — this function runs at startup and
    nothing on a request path calls it, so "logged once at start" is a property of where
    it sits rather than a counter someone has to maintain. Neither the log record nor the
    exception carries any part of the configured value.
    """
    active: SecretBackend = backend if backend is not None else EnvironmentSecretBackend()

    def _lookup(key: str) -> SecretStr | None:
        try:
            return active.get_secret(endpoint=ADMIN_SECRET_ENDPOINT, key=key)
        except SecretNotFoundError:
            return None

    encoded = _lookup(ADMIN_PASSWORD_HASH_KEY)
    plaintext = _lookup(ADMIN_PASSWORD_KEY) if encoded is None else None

    if encoded is None and plaintext is None:
        # The message names the endpoint, both keys and the environment variables they
        # normalize to -- never a value.
        hash_variable = f"{ADMIN_SECRET_ENDPOINT.upper()}__{ADMIN_PASSWORD_HASH_KEY.upper()}"
        password_variable = f"{ADMIN_SECRET_ENDPOINT.upper()}__{ADMIN_PASSWORD_KEY.upper()}"
        _LOG.error(
            "console.auth.not_configured",
            secret_endpoint=ADMIN_SECRET_ENDPOINT,
            secret_key=ADMIN_PASSWORD_HASH_KEY,
            alternative_secret_key=ADMIN_PASSWORD_KEY,
        )
        raise AuthNotConfiguredError(
            "no administrator credential is configured, so the console and API refuse to "
            f"serve (C7). Set {hash_variable} to a hash produced by "
            "qlabs_catalog_sync.api.auth.hash_password (scripts/make_admin_hash.py prints "
            f"the line), or -- for a local deployment -- set {password_variable} to the "
            "password itself and it will be hashed at startup."
        ) from None

    source_key = ADMIN_PASSWORD_HASH_KEY if encoded is not None else ADMIN_PASSWORD_KEY

    try:
        username = active.get_secret(
            endpoint=ADMIN_SECRET_ENDPOINT, key=ADMIN_USERNAME_KEY
        ).get_secret_value()
    except SecretNotFoundError:
        username = DEFAULT_ADMIN_USERNAME

    try:
        if encoded is not None:
            credential = AdminCredential.from_password_hash(
                encoded.get_secret_value(), username=username
            )
        else:
            assert plaintext is not None  # noqa: S101 -- narrowed by the guard above
            credential = AdminCredential.from_password(
                plaintext.get_secret_value(), username=username
            )
    except AuthConfigurationError as exc:
        _LOG.error(
            "console.auth.credential_malformed",
            reason=str(exc),
            secret_endpoint=ADMIN_SECRET_ENDPOINT,
            secret_key=source_key,
        )
        # Re-raised naming the variable that carries the rejected value: two keys can
        # configure this credential now, and "the password is too short" is only
        # actionable if the operator knows which of them to go and edit. ``from None``
        # keeps the original off the traceback -- it carries no value either, but the
        # rule in this module is that nothing derived from a credential gets re-exposed
        # by accident.
        raise AuthConfigurationError(
            f"{exc} (configured in "
            f"{ADMIN_SECRET_ENDPOINT.upper()}__{source_key.upper()})"
        ) from None

    if encoded is None:
        _LOG.warning(
            "console.auth.plaintext_password",
            secret_endpoint=ADMIN_SECRET_ENDPOINT,
            secret_key=ADMIN_PASSWORD_KEY,
            preferred_secret_key=ADMIN_PASSWORD_HASH_KEY,
            reason=(
                "the administrator password is configured in the clear and was hashed at "
                "startup; anything that can read this deployment's environment holds a "
                "directly reusable credential"
            ),
        )

    _LOG.info(
        "console.auth.configured",
        username=credential.username,
        kdf=_HASH_SCHEME,
        kdf_params=credential.params.encode(),
        secret_key=source_key,
    )
    return credential


# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ConsoleSession:
    """One signed-in administrator session.

    ``token_digest`` is the SHA-256 of the cookie's token, which is also this session's
    key in the store — the token itself is never held anywhere server-side, so a memory
    dump or a stray ``repr`` yields nothing a browser could replay. ``csrf_token`` lives
    here rather than in a signed cookie or a module-level constant, which is what makes
    it bound to *this* session: another session's token simply is not this value.
    """

    username: str
    csrf_token: str
    created_at: datetime
    expires_at: datetime
    token_digest: str

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


def _digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _SessionStore:
    """In-memory session table for the one process C8 says the console runs in.

    Sessions do not survive a restart. That is a deliberate trade rather than an
    oversight: persisting them would put a replayable bearer token into the state store,
    and the cost of the alternative is one operator signing in again after a deploy.
    """

    def __init__(self, *, ttl: timedelta, clock: Callable[[], datetime]) -> None:
        self._ttl = ttl
        self._clock = clock
        self._sessions: OrderedDict[str, ConsoleSession] = OrderedDict()

    def create(self, username: str) -> tuple[str, ConsoleSession]:
        """Mint a session, returning ``(cookie token, session)``."""
        now = self._clock()
        self._prune(now)
        token = stdlib_secrets.token_urlsafe(32)
        session = ConsoleSession(
            username=username,
            csrf_token=stdlib_secrets.token_urlsafe(32),
            created_at=now,
            expires_at=now + self._ttl,
            token_digest=_digest_token(token),
        )
        while len(self._sessions) >= _MAX_SESSIONS:
            self._sessions.popitem(last=False)
        self._sessions[session.token_digest] = session
        return token, session

    def lookup(self, token: str | None) -> ConsoleSession | None:
        """The live session for ``token``, or ``None`` — expired, revoked and unknown
        tokens are indistinguishable to the caller."""
        now = self._clock()
        self._prune(now)
        if not token:
            return None
        session = self._sessions.get(_digest_token(token))
        if session is None or session.is_expired(now):
            return None
        return session

    def revoke(self, session: ConsoleSession) -> None:
        self._sessions.pop(session.token_digest, None)

    def _prune(self, now: datetime) -> None:
        expired = [key for key, value in self._sessions.items() if value.is_expired(now)]
        for key in expired:
            del self._sessions[key]

    def __len__(self) -> int:
        return len(self._sessions)


# --------------------------------------------------------------------------------------
# The policy object installed on the app
# --------------------------------------------------------------------------------------


class ConsoleAuth:
    """Everything the app needs to authenticate the one administrator.

    Constructed by the deployment (see :func:`console_auth_from_environment`) and handed
    to :func:`~qlabs_catalog_sync.api.app.create_app` as ``auth=``. Holds the credential,
    the session table and the two policy switches this task exposes.

    One instance belongs to one event loop, because its single-flight semaphore does —
    which is the deployment's shape anyway (C8: one process, one loop, one listener). Two
    apps that must run on two loops get two instances, and therefore two session tables;
    that is the honest reading of "one administrator identity, one signed-in session set".
    """

    def __init__(
        self,
        *,
        credential: AdminCredential,
        session_ttl: timedelta = DEFAULT_SESSION_TTL,
        metrics_public: bool = True,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._credential = credential
        self._session_ttl = session_ttl
        self._clock = clock
        self._sessions = _SessionStore(ttl=session_ttl, clock=clock)
        # Single-flight around the KDF. It keeps a 64 MiB, 160 ms derivation off the event
        # loop *and* off every other request's back: only one verification runs at a time,
        # so concurrent sign-in attempts cannot multiply the memory cost, and an online
        # guessing attack is throttled to the KDF's own rate. This is the whole of the
        # brute-force story -- there is no lockout, deliberately (see the task report).
        self._verify_lock = asyncio.Semaphore(1)
        self.metrics_public = metrics_public

    @property
    def session_ttl(self) -> timedelta:
        return self._session_ttl

    @property
    def username(self) -> str:
        """The configured administrator's username. Not a secret; the console shows it."""
        return self._credential.username

    async def sign_in(
        self, *, username: str, password: SecretStr
    ) -> tuple[str, ConsoleSession] | None:
        """``(cookie token, session)`` on success, ``None`` on any failure.

        One return value for "no such user" and "wrong password" on purpose: the caller
        has nothing to tell them apart with, so it cannot accidentally build a response
        that does. The key derivation runs either way (see
        :meth:`AdminCredential.verify`), serialized through this object's semaphore, and
        off the event loop.
        """
        async with self._verify_lock:
            ok = await asyncio.to_thread(
                self._credential.verify,
                username=username,
                password=password.get_secret_value(),
            )
        if not ok:
            # No username, no password, no length, no hint of which half was wrong: an
            # operator reading the log learns that a sign-in failed, and an attacker
            # reading it over their shoulder learns nothing else. The attempted username
            # is omitted precisely because it is so often a password typed one field up.
            _LOG.warning("console.auth.sign_in_failed")
            return None
        token, session = self._sessions.create(self._credential.username)
        _LOG.info("console.auth.sign_in_succeeded", username=session.username)
        return token, session

    def session_for(self, request: Request) -> ConsoleSession | None:
        """The live session this request presents, or ``None``."""
        cookies = request.cookies
        token = cookies.get(SESSION_COOKIE_SECURE) or cookies.get(SESSION_COOKIE)
        return self._sessions.lookup(token)

    def sign_out(self, session: ConsoleSession) -> None:
        """Invalidate ``session`` server-side. The cookie stops working immediately."""
        self._sessions.revoke(session)
        _LOG.info("console.auth.signed_out", username=session.username)

    def csrf_token_matches(self, session: ConsoleSession, presented: str | None) -> bool:
        """Constant-time comparison of ``presented`` against *this session's* token."""
        if not presented:
            return False
        return hmac.compare_digest(presented.encode("utf-8"), session.csrf_token.encode("utf-8"))

    @property
    def live_session_count(self) -> int:
        """How many sessions are currently valid. Exposed for tests and diagnostics."""
        return len(self._sessions)


def console_auth_from_environment(
    *,
    backend: SecretBackend | None = None,
    session_ttl: timedelta = DEFAULT_SESSION_TTL,
    metrics_public: bool = True,
) -> ConsoleAuth:
    """The deployment's one call: build a :class:`ConsoleAuth`, or refuse to serve.

    Propagates :class:`AuthNotConfiguredError` / :class:`AuthConfigurationError` from
    :func:`load_admin_credential` unchanged, so a process wired through this function
    cannot start without a credential.
    """
    return ConsoleAuth(
        credential=load_admin_credential(backend=backend),
        session_ttl=session_ttl,
        metrics_public=metrics_public,
    )


# --------------------------------------------------------------------------------------
# Request plumbing
# --------------------------------------------------------------------------------------


def current_session(request: Request) -> ConsoleSession | None:
    """The session the auth middleware attached to ``request``, if any."""
    state = request.scope.get("state")
    if not isinstance(state, dict):
        return None
    session = state.get(_SESSION_SCOPE_KEY)
    return session if isinstance(session, ConsoleSession) else None


def require_session(request: Request) -> ConsoleSession:
    """:func:`current_session`, or raise a 401 in the shared error shape.

    Route code from T12.3 onward calls this when it needs to know who is acting. Reaching
    the raise means the middleware was not installed on this app; the answer is still a
    refusal, never an anonymous success.
    """
    session = current_session(request)
    if session is None:
        raise APIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="unauthenticated",
            message="sign in to use this API",
        )
    return session


def _is_tls(request: Request) -> bool:
    """Whether this request reached us over TLS.

    ``request.url.scheme`` is authoritative for a direct connection and for uvicorn's own
    proxy-header handling, but uvicorn only rewrites the scheme for proxies whose address
    it trusts, and the normal deployment shape here is a TLS-terminating ingress at an
    address nobody configured. So ``X-Forwarded-Proto`` is consulted too. Guessing wrong
    in the permissive direction (marking the cookie ``Secure`` on a plain-HTTP origin)
    only stops the cookie being stored; guessing wrong in the other direction would ship
    a session cookie over cleartext, which is the failure that matters.
    """
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto")
    if forwarded is None:
        return False
    return forwarded.split(",")[0].strip().lower() == "https"


def _refuse(status_code: int, code: str, message: str) -> JSONResponse:
    """An authentication refusal in the shared :class:`ErrorModel` shape.

    Built here rather than raised as an :class:`~qlabs_catalog_sync.api.errors.APIError`
    because middleware sits *outside* the app's exception handlers: an exception raised
    at this level would never reach them and would surface as a bare 500.
    """
    return JSONResponse(
        status_code=status_code,
        content=ErrorModel(code=code, message=message).model_dump(mode="json"),
    )


class _AuthMiddleware:
    """Default-deny authentication and CSRF for every route on the app.

    A pure ASGI middleware rather than ``BaseHTTPMiddleware``: it only needs the request
    line, the headers and the cookies, never the body, and this shape neither buffers the
    response nor interferes with ``FileResponse`` (which is how the console shell is
    served).

    It decides on the request *path*, before routing, and never inspects the route table.
    That is deliberate. Reading the route table would couple this — the security surface —
    to FastAPI's internal representation of an included router, which is exactly the kind
    of thing that changes in a minor release and fails open when it does. The path rule is
    the same rule ``static.py`` already uses to decide JSON-versus-shell, so the two cannot
    disagree: both call :func:`~qlabs_catalog_sync.api.static.path_is_under_api_prefix`.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        auth: ConsoleAuth,
        api_prefix: str,
        protected_paths: frozenset[str],
    ) -> None:
        self._app = app
        self._auth = auth
        self._api_prefix = api_prefix
        self._sign_in_path = f"{api_prefix}{AUTH_SESSION_ROUTE}"
        # Paths outside the API prefix that must NOT inherit the console-shell allowance:
        # FastAPI's own schema and documentation endpoints (see install_auth).
        self._protected_paths = protected_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope)
        refusal = self._authorize(request)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _is_console_asset_request(self, path: str, method: str) -> bool:
        """Whether an anonymous request may be answered by the console's static mount.

        Safe methods only, never under the API prefix, never one of the schema or
        documentation endpoints, and never the health surface (which is exempt earlier and
        for its own reasons, not as an asset).
        """
        if method not in _SAFE_METHODS:
            return False
        if path_is_under_api_prefix(path, api_prefix=self._api_prefix):
            return False
        if path in PUBLIC_PROBE_PATHS:
            # Exempt earlier and on their own terms, or -- when metrics_public is off --
            # deliberately not exempt at all. Either way they are not console assets, and
            # reaching here means the answer is "no".
            return False
        return path not in self._protected_paths

    def _authorize(self, request: Request) -> JSONResponse | None:
        path = request.url.path
        method = request.method

        if path == "/healthz":
            return None
        if path == "/metrics" and self._auth.metrics_public:
            return None
        if path == self._sign_in_path and method == "POST":
            return None

        session = self._auth.session_for(request)

        if session is None:
            if self._is_console_asset_request(path, method):
                # The console shell and its assets, so the SPA can draw a sign-in screen
                # in the browser instead of the browser rendering a JSON 401 as text.
                return None
            return _refuse(
                status.HTTP_401_UNAUTHORIZED,
                "unauthenticated",
                "sign in to use this API",
            )

        if method in _MUTATING_METHODS and not self._auth.csrf_token_matches(
            session, request.headers.get(CSRF_HEADER)
        ):
            return _refuse(
                status.HTTP_403_FORBIDDEN,
                "csrf_token_invalid",
                f"this request needs a valid {CSRF_HEADER} header for the current session",
            )

        state = request.scope.setdefault("state", {})
        if isinstance(state, dict):
            state[_SESSION_SCOPE_KEY] = session
        return None


# --------------------------------------------------------------------------------------
# The sign-in / sign-out routes
# --------------------------------------------------------------------------------------


class SignInRequest(BaseModel):
    """The sign-in body. ``password`` is a ``SecretStr`` so that neither a pydantic
    validation error, a ``repr`` nor a structured log field can ever carry its value."""

    model_config = ConfigDict(extra="forbid")

    username: str
    password: SecretStr


class SessionInfo(BaseModel):
    """What the console learns about its own session.

    ``csrf_token`` is here, and only here, because the session cookie is ``HttpOnly``:
    the SPA cannot read the cookie, so this is how it obtains the token it must echo in
    :data:`CSRF_HEADER`. Fetching it needs a valid session already, and ``SameSite=Lax``
    keeps a cross-site page from making that request with the operator's cookie.
    """

    model_config = ConfigDict(frozen=True)

    username: str
    csrf_token: str
    expires_at: datetime


def _set_session_cookie(
    response: Response, *, request: Request, token: str, ttl: timedelta
) -> None:
    secure = _is_tls(request)
    response.set_cookie(
        key=SESSION_COOKIE_SECURE if secure else SESSION_COOKIE,
        value=token,
        max_age=int(ttl.total_seconds()),
        path="/",
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def _build_auth_router(auth: ConsoleAuth) -> APIRouter:
    router = APIRouter(tags=["auth"])

    @router.post(
        AUTH_SESSION_ROUTE,
        response_model=SessionInfo,
        responses=API_ERROR_RESPONSES,
        summary="Sign in as the administrator",
    )
    async def sign_in(payload: SignInRequest, request: Request, response: Response) -> SessionInfo:
        """Exchange the administrator credential for a session cookie and a CSRF token.

        The refusal is one shape for every failure — wrong username, wrong password,
        wrong both — because :meth:`ConsoleAuth.sign_in` gives this route nothing finer
        to report.
        """
        result = await auth.sign_in(username=payload.username, password=payload.password)
        if result is None:
            raise APIError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="invalid_credentials",
                message="invalid username or password",
            )
        token, session = result
        _set_session_cookie(response, request=request, token=token, ttl=auth.session_ttl)
        return SessionInfo(
            username=session.username,
            csrf_token=session.csrf_token,
            expires_at=session.expires_at,
        )

    @router.get(
        AUTH_SESSION_ROUTE,
        response_model=SessionInfo,
        responses=API_ERROR_RESPONSES,
        summary="Describe the current administrator session",
    )
    async def read_session(request: Request) -> SessionInfo:
        """What the SPA calls on boot to find out whether it is signed in, and to recover
        the CSRF token it cannot read from the ``HttpOnly`` cookie."""
        session = require_session(request)
        return SessionInfo(
            username=session.username,
            csrf_token=session.csrf_token,
            expires_at=session.expires_at,
        )

    @router.delete(
        AUTH_SESSION_ROUTE,
        status_code=status.HTTP_204_NO_CONTENT,
        responses=API_ERROR_RESPONSES,
        summary="Sign out",
    )
    async def sign_out(request: Request) -> Response:
        """Invalidate the session server-side and clear the cookie.

        Mutating, so it needs a valid :data:`CSRF_HEADER` like every other mutating
        request — being forcibly signed out by a cross-site page is a small harm, but it
        is still a harm, and "every mutating request" is easier to keep true than a list
        of exceptions.
        """
        session = require_session(request)
        auth.sign_out(session)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(
            key=SESSION_COOKIE_SECURE if _is_tls(request) else SESSION_COOKIE,
            path="/",
            httponly=True,
            samesite="lax",
            secure=_is_tls(request),
        )
        return response

    return router


def schema_and_docs_paths(app: FastAPI) -> frozenset[str]:
    """FastAPI's own schema/documentation paths on ``app``, whichever are enabled.

    Read off the application rather than hard-coded, so an app constructed with a
    different ``openapi_url`` or with the docs disabled still gets exactly the right set.
    These sit outside the API prefix, which is the one place unauthenticated safe-method
    requests are served — this is what excludes them from that allowance.
    """
    candidates = (
        app.openapi_url,
        app.docs_url,
        app.redoc_url,
        app.swagger_ui_oauth2_redirect_url,
    )
    return frozenset(path for path in candidates if path)


def install_auth(app: FastAPI, *, auth: ConsoleAuth | None, api_prefix: str) -> None:
    """Install administrator authentication on ``app``, or record that it was not.

    Call from :func:`~qlabs_catalog_sync.api.app.create_app` **before** the API routes and
    ``mount_static``: the sign-in routes registered here have to land ahead of the SPA
    catch-all, which claims every unmatched ``GET``.

    ``auth=None`` builds an app with no authentication at all. That is what T12.1's tests
    and every other suite that exercises an unrelated part of the API use, and it is
    logged as a warning on every construction so that a *deployment* which reached this
    state cannot do so quietly. The deployment path never passes ``None``: it goes through
    :func:`console_auth_from_environment`, which raises rather than returning ``None``
    when no credential is configured.
    """
    if auth is None:
        _LOG.warning(
            "api.auth.not_installed",
            detail=(
                "this application instance serves without administrator authentication; "
                "a deployment must build its ConsoleAuth with "
                "console_auth_from_environment(), which refuses to return one when no "
                "credential is configured (C7)"
            ),
        )
        return
    app.include_router(_build_auth_router(auth), prefix=api_prefix)
    app.add_middleware(
        _AuthMiddleware,
        auth=auth,
        api_prefix=api_prefix,
        protected_paths=schema_and_docs_paths(app),
    )
