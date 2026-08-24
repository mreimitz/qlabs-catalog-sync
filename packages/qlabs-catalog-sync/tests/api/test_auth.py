"""Administrator authentication for the console and the API (C7, WP12/T12.2).

The definition of done, in tests: unauthenticated access to every route except the health
surface and sign-in is refused; a mutating request without a valid CSRF token is refused;
no credential configured means the console does not serve; the credential never appears in
a log record.

Every test here is written to fail on a *dishonest* implementation, not merely to describe
the honest one. In particular:

* ``test_a_route_added_without_thinking_about_auth_is_still_protected`` registers a brand
  new route the way T12.3-T12.7 will and fails if it answers an anonymous caller — the
  single most valuable test in this file, because it is the one that keeps being true
  after this task is over.
* ``test_only_these_paths_live_outside_the_api_prefix`` fails when anyone adds a route
  outside the prefix, which is the one place the middleware's console-shell allowance
  could be inherited by accident.
* ``test_the_password_and_its_hash_never_reach_a_log_record`` drives real sign-ins through
  the engine's *real* structlog processors (``REDACTION_TEST_PROCESSORS`` -- the same
  technique ``tests/configstore/test_secrets.py`` uses) and asserts on sentinels.
* ``test_csrf_token_is_bound_to_the_session_that_minted_it`` presents one live session's
  token with another live session's cookie.
* ``test_a_signed_out_cookie_stops_working`` replays the raw cookie by hand, rather than
  trusting the client's cookie jar to have dropped it.
* ``test_verify_derives_the_key_and_compares_in_constant_time_even_for_a_wrong_username``
  asserts on the code path (``hmac.compare_digest`` / ``hashlib.scrypt`` actually being
  called) rather than on wall-clock timing, which would be flaky.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pydantic import SecretStr

from qlabs_catalog_sync.api.app import API_PREFIX, create_app
from qlabs_catalog_sync.api.auth import (
    ADMIN_PASSWORD_HASH_KEY,
    ADMIN_PASSWORD_KEY,
    ADMIN_SECRET_ENDPOINT,
    ADMIN_USERNAME_KEY,
    AUTH_SESSION_ROUTE,
    CSRF_HEADER,
    DEFAULT_ADMIN_USERNAME,
    DEFAULT_SCRYPT_PARAMS,
    MAX_KDF_MEMORY_BYTES,
    MIN_PASSWORD_LENGTH,
    PUBLIC_PROBE_PATHS,
    SESSION_COOKIE,
    SESSION_COOKIE_SECURE,
    AdminCredential,
    AuthConfigurationError,
    AuthNotConfiguredError,
    ConsoleAuth,
    ScryptParams,
    console_auth_from_environment,
    hash_password,
    load_admin_credential,
    require_session,
    schema_and_docs_paths,
)
from qlabs_catalog_sync.api.errors import APIError
from qlabs_catalog_sync.config import SecretNotFoundError
from qlabs_catalog_sync.observability import REDACTION_TEST_PROCESSORS, HealthRegistry

from .api_helpers import write_console_dist

# --------------------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------------------

#: Sentinels. Every "must not leak" assertion below looks for one of these two strings in
#: a log record, a response body, an exception, a traceback or the OpenAPI schema. They
#: are distinctive enough that a match can only mean a real leak.
PASSWORD = "SENTINEL-console-admin-password-4b71ce"
WRONG_PASSWORD = "SENTINEL-not-the-console-password-9de204"
USERNAME = "console-operator-7fd2b1"

#: Deliberately at the cost *floor* rather than the shipped default: the floor is what
#: this suite is allowed to run at ~500 times without taking a minute, and using the real
#: parser/validator path means a hash below the floor is still rejected (see
#: ``test_a_hash_below_the_cost_floor_is_refused``).
TEST_PARAMS = ScryptParams(log_n=14, r=8, p=1)

#: Computed once: hashing is intentionally expensive, and the *value* is a sentinel too --
#: the configured hash must not leak any more than the password it was derived from.
PASSWORD_HASH = hash_password(PASSWORD, params=TEST_PARAMS)

SESSION_PATH = f"{API_PREFIX}{AUTH_SESSION_ROUTE}"


class MappingSecretBackend:
    """A :class:`~qlabs_catalog_sync.config.SecretBackend` over a plain dict.

    Reuses the real :class:`~qlabs_catalog_sync.config.SecretNotFoundError` rather than
    inventing a second "missing secret" signal, so ``load_admin_credential`` takes exactly
    the code path it takes against a real environment with the variable unset.
    """

    def __init__(self, values: dict[tuple[str, str], str] | None = None) -> None:
        self._values = values or {}

    def get_secret(self, *, endpoint: str, key: str) -> SecretStr:
        try:
            return SecretStr(self._values[(endpoint, key)])
        except KeyError:
            raise SecretNotFoundError(
                endpoint=endpoint,
                key=key,
                backend="test-mapping",
                hint=f"expected environment variable {endpoint.upper()}__{key.upper()}",
            ) from None


def configured_backend(
    *, password_hash: str = PASSWORD_HASH, username: str | None = USERNAME
) -> MappingSecretBackend:
    values = {(ADMIN_SECRET_ENDPOINT, ADMIN_PASSWORD_HASH_KEY): password_hash}
    if username is not None:
        values[(ADMIN_SECRET_ENDPOINT, ADMIN_USERNAME_KEY)] = username
    return MappingSecretBackend(values)


class FakeClock:
    """A movable clock, so session expiry is tested by advancing time rather than sleeping."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def make_auth(
    *,
    metrics_public: bool = True,
    session_ttl: timedelta = timedelta(hours=8),
    clock: FakeClock | None = None,
) -> ConsoleAuth:
    return ConsoleAuth(
        credential=AdminCredential.from_password_hash(PASSWORD_HASH, username=USERNAME),
        session_ttl=session_ttl,
        metrics_public=metrics_public,
        clock=clock or FakeClock(),
    )


def build_app(
    *, auth: ConsoleAuth | None = None, static_dir: Path | None = None
) -> tuple[FastAPI, ConsoleAuth]:
    """An app with authentication installed, plus the :class:`ConsoleAuth` it was built with."""
    console_auth = auth or make_auth()
    app = create_app(
        health=HealthRegistry(),
        metrics_registry=CollectorRegistry(),
        static_dir=static_dir,
        auth=console_auth,
    )
    return app, console_auth


def client_for(app: FastAPI, *, base_url: str = "http://testserver") -> TestClient:
    return TestClient(app, base_url=base_url, raise_server_exceptions=False)


def sign_in(
    client: TestClient, *, username: str = USERNAME, password: str = PASSWORD, **kwargs: Any
) -> Any:
    return client.post(SESSION_PATH, json={"username": username, "password": password}, **kwargs)


def sign_in_ok(client: TestClient) -> str:
    """Sign in, asserting success, and return the session's CSRF token."""
    response = sign_in(client)
    assert response.status_code == 200, response.text
    token = response.json()["csrf_token"]
    assert isinstance(token, str) and token
    return token


def add_route(app: FastAPI, path: str, *, methods: list[str]) -> None:
    """Register a route the way a later WP12 task would, ahead of the SPA catch-all.

    ``create_app`` registers the catch-all last on purpose (``static.py``), and
    ``add_api_route`` appends -- so a route appended afterwards would be unreachable. Real
    routes are added inside ``create_app``; this mirrors where they land.
    """

    async def _endpoint() -> dict[str, str]:
        return {"reached": path}

    app.add_api_route(path, _endpoint, methods=methods, include_in_schema=False)
    routes = app.router.routes
    new_route = routes.pop()
    routes.insert(len(routes) - 1, new_route)


def registered_paths(app: FastAPI) -> set[str]:
    """Every path template this app answers on, from both the schema and the route table."""
    paths: set[str] = set(app.openapi()["paths"])
    for route in app.router.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
    return paths


def rendered(entries: list[Any]) -> str:
    """Every captured log record flattened to one searchable string."""
    return json.dumps(entries, default=repr)


@pytest.fixture
def captured_logs() -> Iterator[list[Any]]:
    """Capture through the engine's *real* redaction/context processors, not a copy."""
    with structlog.testing.capture_logs(processors=REDACTION_TEST_PROCESSORS) as entries:
        yield entries


# --------------------------------------------------------------------------------------
# No credential configured: the console does not serve (DoD 3)
# --------------------------------------------------------------------------------------


def test_no_credential_configured_means_the_console_does_not_serve() -> None:
    """The refusal is a raise at startup, not a degraded app.

    Deliberately not "serve only /healthz": a container that boots and answers its
    liveness probe while the console is permanently unusable looks healthy to every
    orchestrator, which is worse than a process that exits with a reason. There is no code
    path from "nothing configured" to a serving app -- ``console_auth_from_environment`` is
    the only documented way to obtain the ``ConsoleAuth`` ``create_app`` needs, and it
    raises.
    """
    empty = MappingSecretBackend()

    with pytest.raises(AuthNotConfiguredError):
        load_admin_credential(backend=empty)

    with pytest.raises(AuthNotConfiguredError):
        console_auth_from_environment(backend=empty)


def test_the_refusal_names_the_variable_the_operator_has_to_set() -> None:
    with pytest.raises(AuthNotConfiguredError) as excinfo:
        load_admin_credential(backend=MappingSecretBackend())

    message = str(excinfo.value)
    assert ADMIN_SECRET_ENDPOINT.upper() in message.upper()
    assert ADMIN_PASSWORD_HASH_KEY.upper() in message.upper()
    assert "hash_password" in message


def test_the_refusal_is_logged_once_at_start(captured_logs: list[Any]) -> None:
    with pytest.raises(AuthNotConfiguredError):
        load_admin_credential(backend=MappingSecretBackend())

    refusals = [e for e in captured_logs if e.get("event") == "console.auth.not_configured"]
    assert len(refusals) == 1
    assert refusals[0]["log_level"] == "error"


def test_the_refusal_is_not_logged_per_request(captured_logs: list[Any]) -> None:
    """ "Logged once at start" has to mean *not on every request*. A configured app under
    load must never emit the startup refusal, and an unauthenticated caller must never be
    able to make it emit one."""
    app, _ = build_app()
    client = client_for(app)

    for _ in range(5):
        client.get(f"{API_PREFIX}/anything")
        client.get("/healthz")

    assert [e for e in captured_logs if e.get("event") == "console.auth.not_configured"] == []


# --------------------------------------------------------------------------------------
# The plaintext-password escape hatch (local deployments)
# --------------------------------------------------------------------------------------


def plaintext_backend(
    *, password: str = PASSWORD, username: str | None = USERNAME
) -> MappingSecretBackend:
    """A backend holding a *password* under :data:`ADMIN_PASSWORD_KEY` and no hash."""
    values = {(ADMIN_SECRET_ENDPOINT, ADMIN_PASSWORD_KEY): password}
    if username is not None:
        values[(ADMIN_SECRET_ENDPOINT, ADMIN_USERNAME_KEY)] = username
    return MappingSecretBackend(values)


def test_a_plaintext_password_configures_the_credential() -> None:
    """``QLABS_CONSOLE_ADMIN__PASSWORD`` is hashed at startup and works like a hash.

    This is the convenience path for a deployment where the environment is not a shared
    secret store -- a local container, a developer machine -- and it has to produce a
    credential indistinguishable from the hash path, or it is not actually an alternative.
    """
    credential = load_admin_credential(backend=plaintext_backend())

    assert credential.username == USERNAME
    assert credential.verify(username=USERNAME, password=PASSWORD)
    assert not credential.verify(username=USERNAME, password=WRONG_PASSWORD)
    assert not credential.verify(username="somebody-else", password=PASSWORD)


def test_the_plaintext_password_defaults_the_username_like_the_hash_path_does() -> None:
    credential = load_admin_credential(backend=plaintext_backend(username=None))

    assert credential.username == DEFAULT_ADMIN_USERNAME


def test_a_configured_hash_wins_over_a_configured_plaintext_password() -> None:
    """Both set is a misconfiguration, and the *safer* value has to be the one that counts.

    Resolving it the other way would mean a stray ``QLABS_CONSOLE_ADMIN__PASSWORD`` left in
    a shell profile could silently replace a deployment's real credential.
    """
    backend = MappingSecretBackend(
        {
            (ADMIN_SECRET_ENDPOINT, ADMIN_PASSWORD_HASH_KEY): PASSWORD_HASH,
            (ADMIN_SECRET_ENDPOINT, ADMIN_PASSWORD_KEY): WRONG_PASSWORD,
            (ADMIN_SECRET_ENDPOINT, ADMIN_USERNAME_KEY): USERNAME,
        }
    )

    credential = load_admin_credential(backend=backend)

    assert credential.verify(username=USERNAME, password=PASSWORD)
    assert not credential.verify(username=USERNAME, password=WRONG_PASSWORD)


def test_the_plaintext_password_path_warns_that_it_is_the_weaker_choice(
    captured_logs: list[Any],
) -> None:
    """It must be visible in the logs of a deployment that took this path by accident."""
    load_admin_credential(backend=plaintext_backend())

    warnings = [e for e in captured_logs if e.get("event") == "console.auth.plaintext_password"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"


def test_the_plaintext_password_never_reaches_a_log_record(captured_logs: list[Any]) -> None:
    """The sentinel discipline the hash path is held to, applied to the password variable."""
    load_admin_credential(backend=plaintext_backend())

    rendered = json.dumps([{k: str(v) for k, v in e.items()} for e in captured_logs])
    assert PASSWORD not in rendered


def test_a_plaintext_password_below_the_minimum_length_is_refused() -> None:
    """``hash_password``'s length floor is the only password policy a hash-based credential
    leaves available, so the convenience path must not be a way around it."""
    short = "SENTINEL-x"[:MIN_PASSWORD_LENGTH - 1]

    with pytest.raises(AuthConfigurationError) as excinfo:
        load_admin_credential(backend=plaintext_backend(password=short))

    assert short not in str(excinfo.value)
    assert str(MIN_PASSWORD_LENGTH) in str(excinfo.value)


def test_a_refused_plaintext_password_is_logged_without_its_value(
    captured_logs: list[Any],
) -> None:
    with pytest.raises(AuthConfigurationError):
        load_admin_credential(backend=plaintext_backend(password="short"))

    events = [e for e in captured_logs if e.get("event") == "console.auth.credential_malformed"]
    assert len(events) == 1
    assert "short" not in json.dumps({k: str(v) for k, v in events[0].items()})


def test_the_refusal_names_the_password_variable_too() -> None:
    """With neither variable set, the operator has to learn about *both* ways out."""
    with pytest.raises(AuthNotConfiguredError) as excinfo:
        load_admin_credential(backend=MappingSecretBackend())

    message = str(excinfo.value).upper()
    assert ADMIN_PASSWORD_HASH_KEY.upper() in message
    assert ADMIN_PASSWORD_KEY.upper() in message


def test_console_auth_from_environment_accepts_a_plaintext_password() -> None:
    """The deployment's one call has to work through the convenience path as well, all the
    way to a real sign-in over HTTP."""
    auth = console_auth_from_environment(backend=plaintext_backend())
    assert auth.username == USERNAME

    app, _ = build_app(auth=auth)
    response = client_for(app).post(
        SESSION_PATH, json={"username": USERNAME, "password": PASSWORD}
    )

    assert response.status_code == 200


def test_building_an_app_with_no_auth_at_all_is_logged_as_a_warning(
    captured_logs: list[Any],
) -> None:
    """``create_app(auth=None)`` is the escape hatch every other API test suite uses. It
    must not be quiet: a deployment that reached this state has no administrator, and the
    log is what makes that visible rather than a silently open console."""
    create_app(health=HealthRegistry(), metrics_registry=CollectorRegistry())

    warnings = [e for e in captured_logs if e.get("event") == "api.auth.not_installed"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"


def test_a_configured_credential_defaults_the_username_but_uses_one_when_given() -> None:
    default = load_admin_credential(backend=configured_backend(username=None))
    assert default.username == DEFAULT_ADMIN_USERNAME

    named = load_admin_credential(backend=configured_backend(username=f"  {USERNAME}  "))
    assert named.username == USERNAME


# --------------------------------------------------------------------------------------
# The credential: a hash, hashed properly, compared in constant time
# --------------------------------------------------------------------------------------


def test_hash_password_round_trips_and_uses_a_fresh_salt_each_time() -> None:
    first = hash_password(PASSWORD, params=TEST_PARAMS)
    second = hash_password(PASSWORD, params=TEST_PARAMS)

    assert first != second, "a fixed salt would make two hashes of one password identical"
    for encoded in (first, second):
        credential = AdminCredential.from_password_hash(encoded, username=USERNAME)
        assert credential.verify(username=USERNAME, password=PASSWORD)
        assert not credential.verify(username=USERNAME, password=WRONG_PASSWORD)


def test_the_encoded_hash_is_not_the_password_and_not_a_plain_digest_of_it() -> None:
    assert PASSWORD not in PASSWORD_HASH
    assert hashlib.sha256(PASSWORD.encode()).hexdigest() not in PASSWORD_HASH
    assert PASSWORD_HASH.startswith("$scrypt$ln=")


def test_default_scrypt_parameters_meet_the_documented_cost_and_memory_bounds() -> None:
    """The shipped defaults, not the cheap ones this suite runs at."""
    params = DEFAULT_SCRYPT_PARAMS
    # At least the work of OWASP's N=2**17, r=8, p=1 configuration.
    assert params.n * params.r * params.p >= (1 << 17) * 8
    # ...without asking the container for more memory than one verification may have.
    assert 128 * params.r * params.n <= MAX_KDF_MEMORY_BYTES


@pytest.mark.parametrize(
    "params",
    [
        {"log_n": 13},  # below the 16 MiB floor
        {"log_n": 21},  # past the ceiling
        {"log_n": 20, "r": 32},  # would demand 1 GiB per verification
        {"r": 0},
        {"p": 0},
        {"dklen": 8},
    ],
)
def test_scrypt_parameters_outside_the_accepted_range_are_refused(params: dict[str, int]) -> None:
    with pytest.raises(AuthConfigurationError):
        ScryptParams(**params)


def test_a_hash_below_the_cost_floor_is_refused_even_though_it_parses() -> None:
    """A weak hash smuggled into the deployment's configuration must not be usable, and
    the parameters come *from the hash*, so this is the only place to catch it."""
    weak = (
        "$scrypt$ln=10,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )

    with pytest.raises(AuthConfigurationError):
        AdminCredential.from_password_hash(weak, username=USERNAME)


def test_hash_password_rejects_a_password_below_the_minimum_length() -> None:
    short = "sh0rt"
    with pytest.raises(AuthConfigurationError) as excinfo:
        hash_password(short)

    assert str(MIN_PASSWORD_LENGTH) in str(excinfo.value)
    assert short not in str(excinfo.value), "the rejected password must not be echoed"


def test_credential_repr_never_exposes_the_salt_or_the_derived_key() -> None:
    """A credential caught in a traceback or a pytest assertion diff must not hand over
    the material an offline attack needs."""
    credential = AdminCredential.from_password_hash(PASSWORD_HASH, username=USERNAME)
    text = repr(credential)

    assert USERNAME in text
    assert credential.salt.hex() not in text
    assert credential.digest.hex() not in text
    assert str(credential.salt) not in text
    assert str(credential.digest) not in text


def test_verify_accepts_only_the_configured_username_and_password() -> None:
    credential = AdminCredential.from_password_hash(PASSWORD_HASH, username=USERNAME)

    assert credential.verify(username=USERNAME, password=PASSWORD)
    assert not credential.verify(username=USERNAME, password=WRONG_PASSWORD)
    assert not credential.verify(username="someone-else", password=PASSWORD)
    assert not credential.verify(username="someone-else", password=WRONG_PASSWORD)
    assert not credential.verify(username=USERNAME, password="")
    assert not credential.verify(username=USERNAME, password=PASSWORD + "x")


def test_verify_derives_the_key_and_compares_in_constant_time_even_for_a_wrong_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserting on the code path, never on wall-clock timing.

    Two things would each turn sign-in into an oracle and neither shows up in a functional
    test: comparing the derived key with ``==`` (a byte-by-byte early exit), and returning
    early on a username mismatch (skipping the expensive derivation entirely, so a wrong
    username answers far faster than a wrong password). This fails on either.
    """
    real_compare = hmac.compare_digest
    real_scrypt = hashlib.scrypt
    compare_calls: list[int] = []
    scrypt_calls: list[int] = []

    def counting_compare(a: Any, b: Any) -> bool:
        compare_calls.append(1)
        return real_compare(a, b)

    def counting_scrypt(*args: Any, **kwargs: Any) -> bytes:
        scrypt_calls.append(1)
        return real_scrypt(*args, **kwargs)

    monkeypatch.setattr(hmac, "compare_digest", counting_compare)
    monkeypatch.setattr(hashlib, "scrypt", counting_scrypt)

    credential = AdminCredential.from_password_hash(PASSWORD_HASH, username=USERNAME)
    assert not credential.verify(username="definitely-not-the-admin", password=WRONG_PASSWORD)

    assert len(scrypt_calls) == 1, "the key derivation must run even for an unknown username"
    assert len(compare_calls) == 2, "both the password and the username go through compare_digest"


def test_wrong_username_and_wrong_password_are_indistinguishable_on_the_wire() -> None:
    app, _ = build_app()
    client = client_for(app)

    wrong_password = sign_in(client, password=WRONG_PASSWORD)
    wrong_username = sign_in(client, username="not-the-admin")
    wrong_both = sign_in(client, username="not-the-admin", password=WRONG_PASSWORD)

    bodies = {wrong_password.text, wrong_username.text, wrong_both.text}
    statuses = {wrong_password.status_code, wrong_username.status_code, wrong_both.status_code}
    assert statuses == {401}
    assert len(bodies) == 1, f"the refusal must not vary with which half was wrong: {bodies}"
    assert wrong_password.json()["code"] == "invalid_credentials"


@pytest.mark.parametrize(
    "malformed",
    [
        PASSWORD,  # the operator pasted the password into the hash variable
        "",
        "   ",
        "$scrypt$ln=14,r=8,p=1$onlythreeparts",
        "$argon2$ln=14,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "$scrypt$ln=abc,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        # "\u00b2".isdigit() is True but int("\u00b2") raises, quoting the fragment back.
        "$scrypt$ln=\u00b2,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "$scrypt$ln="
        + "9" * 20000
        + ",r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "$scrypt$ln=14,r=8$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "$scrypt$ln=14,r=8,p=1$not!base64!$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "$scrypt$ln=14,r=8,p=1$AA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # 1-byte salt
    ],
)
def test_a_malformed_credential_fails_clearly_without_ever_echoing_its_value(
    malformed: str, captured_logs: list[Any]
) -> None:
    """The value in ``QLABS_CONSOLE_ADMIN__PASSWORD_HASH`` may well *be* a password -- the
    first case here is exactly that mistake. So the refusal describes the expected shape
    and nothing else, in the exception, in its traceback, and in the log record."""
    backend = configured_backend(password_hash=malformed)

    with pytest.raises(AuthConfigurationError) as excinfo:
        load_admin_credential(backend=backend)

    text = "".join(
        [
            str(excinfo.value),
            repr(excinfo.value),
            "".join(traceback.format_exception(excinfo.value)),
            rendered(captured_logs),
        ]
    )
    stripped = malformed.strip()
    if stripped:
        assert stripped not in text
    # ...and it is still a *useful* refusal: it names what was being read and what shape
    # was expected, so an operator can fix it without ever being shown the bad value.
    assert "administrator password hash" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# Default deny: every route is protected unless it is a named exception (DoD 1)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_unauthenticated_requests_under_the_api_prefix_are_refused(method: str) -> None:
    app, _ = build_app()
    client = client_for(app)

    response = client.request(method, f"{API_PREFIX}/endpoints")

    assert response.status_code == 401
    if method != "HEAD":
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["code"] == "unauthenticated"


def test_unauthenticated_access_to_the_session_route_is_refused_except_for_sign_in() -> None:
    app, _ = build_app()
    client = client_for(app)

    assert client.get(SESSION_PATH).status_code == 401
    assert client.delete(SESSION_PATH).status_code == 401
    # POST -- sign-in itself -- is reachable, and answers on its own terms.
    assert sign_in(client, password=WRONG_PASSWORD).status_code == 401
    assert sign_in(client).status_code == 200


def test_a_route_added_without_thinking_about_auth_is_still_protected() -> None:
    """THE DISHONEST-CASE TEST, and the one that has to keep working after T12.2 is done.

    T12.3-T12.7 add real routes. If authentication were a per-route dependency, or an
    allowlist someone has to remember to extend, a route added like this would answer an
    anonymous caller. It is registered exactly the way a later task registers one -- no
    dependency, no decorator, nothing opting in -- and must be refused anyway, while
    remaining perfectly reachable *with* a session (otherwise this test would pass on an
    app where the route simply does not exist).
    """
    app, _ = build_app()
    add_route(app, f"{API_PREFIX}/newly-added-by-a-later-task", methods=["GET"])
    client = client_for(app)

    anonymous = client.get(f"{API_PREFIX}/newly-added-by-a-later-task")
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "unauthenticated"

    sign_in_ok(client)
    authenticated = client.get(f"{API_PREFIX}/newly-added-by-a-later-task")
    assert authenticated.status_code == 200
    assert authenticated.json() == {"reached": f"{API_PREFIX}/newly-added-by-a-later-task"}


def test_an_unknown_api_path_is_refused_before_it_can_reveal_that_it_is_unknown() -> None:
    """404 for a missing route and 401 for an existing one would enumerate the API for an
    anonymous caller. Authentication is decided before routing, so both are 401."""
    app, _ = build_app()
    client = client_for(app)

    assert client.get(f"{API_PREFIX}/definitely-not-a-real-route").status_code == 401

    sign_in_ok(client)
    # ...and once signed in, the honest answer comes back.
    assert client.get(f"{API_PREFIX}/definitely-not-a-real-route").status_code == 404


def test_only_these_paths_live_outside_the_api_prefix() -> None:
    """The one hole the middleware cannot close from the inside, held shut by a test.

    Anything outside ``API_PREFIX`` is served to unauthenticated safe-method requests --
    that allowance exists so the console shell and its assets can load. A route added
    outside the prefix would inherit it. If this assertion fails, the choice is: move the
    route under ``API_PREFIX`` (where it is protected by default), or add it here and
    defend why an anonymous browser may read it.
    """
    app, _ = build_app()

    outside = {path for path in registered_paths(app) if not path.startswith(API_PREFIX)}
    expected = PUBLIC_PROBE_PATHS | schema_and_docs_paths(app) | {"/{full_path:path}"}

    assert outside == expected


def test_the_health_surface_still_answers_without_credentials() -> None:
    """``tests/api/test_serve_single_origin.py`` starts the real service and requires this;
    a container's liveness probe has no session and can never acquire one."""
    app, _ = build_app()
    client = client_for(app)

    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200
    assert set(PUBLIC_PROBE_PATHS) == {"/healthz", "/metrics"}


def test_metrics_can_be_taken_out_of_the_public_set() -> None:
    """``/metrics`` is public by default because a Prometheus scraper has no session -- but
    it does expose operational data on the same origin as the console, so the deployment
    gets a switch."""
    app, _ = build_app(auth=make_auth(metrics_public=False))
    client = client_for(app)

    assert client.get("/metrics").status_code == 401
    assert client.get("/healthz").status_code == 200

    sign_in_ok(client)
    assert client.get("/metrics").status_code == 200


def test_the_schema_and_docs_endpoints_require_a_session() -> None:
    """They describe the administrative API. They sit outside ``API_PREFIX``, so without
    an explicit carve-out they would inherit the console-shell allowance."""
    app, _ = build_app()
    client = client_for(app)

    for path in sorted(schema_and_docs_paths(app)):
        assert client.get(path).status_code == 401, path

    sign_in_ok(client)
    assert client.get("/openapi.json").status_code == 200


def test_require_session_refuses_rather_than_falling_through_to_anonymous_success() -> None:
    """If the middleware were somehow not installed, route code asking "who is acting?"
    must still refuse -- never treat the absence of a session as permission."""
    request = Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    )

    with pytest.raises(APIError) as excinfo:
        require_session(request)

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "unauthenticated"


# --------------------------------------------------------------------------------------
# The console shell versus the JSON 401
# --------------------------------------------------------------------------------------

SPA_SENTINEL = "<html>SPA-SENTINEL-console-shell</html>"


def test_an_unauthenticated_browser_navigation_gets_the_console_shell(tmp_path: Path) -> None:
    """A JSON 401 in the address bar is raw text in the browser and no way to sign in. The
    shell has to load so the SPA can draw a sign-in screen."""
    app, _ = build_app(static_dir=write_console_dist(tmp_path, index_html=SPA_SENTINEL))
    client = client_for(app)

    root = client.get("/")
    deep_link = client.get("/endpoints/42")

    assert root.status_code == 200
    assert root.text == SPA_SENTINEL
    assert deep_link.status_code == 200
    assert deep_link.text == SPA_SENTINEL


def test_an_unauthenticated_asset_request_is_served_so_the_shell_can_render(
    tmp_path: Path,
) -> None:
    app, _ = build_app(static_dir=write_console_dist(tmp_path, index_html=SPA_SENTINEL))
    client = client_for(app)

    response = client.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_an_unauthenticated_api_request_gets_json_even_when_the_shell_exists(
    tmp_path: Path,
) -> None:
    """The flip side, and the one that must never be swapped: the console's own fetch()
    calls have to receive a typed JSON error, never HTML it would fail to parse."""
    app, _ = build_app(static_dir=write_console_dist(tmp_path, index_html=SPA_SENTINEL))
    client = client_for(app)

    response = client.get(f"{API_PREFIX}/endpoints")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert SPA_SENTINEL not in response.text
    assert response.json()["code"] == "unauthenticated"


def test_the_shell_allowance_covers_safe_methods_only(tmp_path: Path) -> None:
    """Serving the shell to an anonymous ``GET`` must not become "anything outside the API
    prefix is anonymous"."""
    app, _ = build_app(static_dir=write_console_dist(tmp_path, index_html=SPA_SENTINEL))
    client = client_for(app)

    response = client.post("/endpoints/42")

    assert response.status_code == 401
    assert SPA_SENTINEL not in response.text


# --------------------------------------------------------------------------------------
# The session cookie
# --------------------------------------------------------------------------------------


def set_cookie_header(response: Any) -> str:
    headers = response.headers.get_list("set-cookie")
    assert headers, "expected the response to set a cookie"
    return headers[0]


def test_the_session_cookie_is_httponly_and_samesite_and_not_secure_on_plain_http() -> None:
    """``Secure`` on a plain-HTTP deployment means the browser silently drops the cookie
    and nobody can sign in at all -- so it tracks the request's scheme rather than being
    hard-coded either way."""
    app, _ = build_app()
    client = client_for(app)

    header = set_cookie_header(sign_in(client))

    assert header.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    assert "Path=/" in header
    assert "Secure" not in header
    assert "Max-Age=" in header


def test_the_session_cookie_is_secure_and_host_prefixed_over_tls() -> None:
    app, _ = build_app()
    client = client_for(app, base_url="https://testserver")

    header = set_cookie_header(sign_in(client))

    assert header.startswith(f"{SESSION_COOKIE_SECURE}=")
    assert "Secure" in header
    assert "HttpOnly" in header
    assert "SameSite=lax" in header


def test_a_tls_terminating_proxy_still_gets_a_secure_cookie() -> None:
    """The normal deployment is an ingress terminating TLS at an address uvicorn does not
    know to trust, so the request reaches this process as plain HTTP."""
    app, _ = build_app()
    client = client_for(app)

    header = set_cookie_header(sign_in(client, headers={"X-Forwarded-Proto": "https"}))

    assert "Secure" in header
    assert header.startswith(f"{SESSION_COOKIE_SECURE}=")


def test_the_cookie_carries_an_opaque_token_and_not_the_credential() -> None:
    app, _ = build_app()
    client = client_for(app)

    sign_in_ok(client)
    token = client.cookies.get(SESSION_COOKIE)

    assert token is not None
    assert PASSWORD not in token
    assert USERNAME not in token
    assert PASSWORD_HASH not in token


def test_a_session_expires() -> None:
    clock = FakeClock()
    app, _ = build_app(auth=make_auth(session_ttl=timedelta(minutes=30), clock=clock))
    client = client_for(app)

    sign_in_ok(client)
    assert client.get(SESSION_PATH).status_code == 200

    clock.advance(timedelta(minutes=29))
    assert client.get(SESSION_PATH).status_code == 200

    clock.advance(timedelta(minutes=2))
    assert client.get(SESSION_PATH).status_code == 401


def test_an_invented_session_token_is_not_accepted() -> None:
    app, _ = build_app()
    client = client_for(app)

    response = client.get(
        SESSION_PATH, headers={"Cookie": f"{SESSION_COOKIE}=not-a-real-session-token"}
    )

    assert response.status_code == 401


def test_a_signed_out_cookie_stops_working() -> None:
    """Sign-out has to invalidate server-side. Replaying the raw cookie by hand is the
    point: trusting the client's cookie jar to have dropped it would pass against an
    implementation that only cleared the cookie and left the session alive."""
    app, console_auth = build_app()
    client = client_for(app)

    csrf = sign_in_ok(client)
    token = client.cookies.get(SESSION_COOKIE)
    assert token is not None
    assert console_auth.live_session_count == 1

    signed_out = client.delete(SESSION_PATH, headers={CSRF_HEADER: csrf})
    assert signed_out.status_code == 204
    assert console_auth.live_session_count == 0

    replayed = client.get(SESSION_PATH, headers={"Cookie": f"{SESSION_COOKIE}={token}"})
    assert replayed.status_code == 401


def test_sign_out_does_not_end_a_second_administrator_session() -> None:
    app, console_auth = build_app()
    first = client_for(app)
    second = client_for(app)

    first_csrf = sign_in_ok(first)
    sign_in_ok(second)
    assert console_auth.live_session_count == 2

    assert first.delete(SESSION_PATH, headers={CSRF_HEADER: first_csrf}).status_code == 204

    assert first.get(SESSION_PATH).status_code == 401
    assert second.get(SESSION_PATH).status_code == 200


def test_the_current_session_endpoint_reports_the_configured_username() -> None:
    app, _ = build_app()
    client = client_for(app)

    sign_in_ok(client)
    body = client.get(SESSION_PATH).json()

    assert body["username"] == USERNAME
    assert isinstance(body["csrf_token"], str) and body["csrf_token"]
    assert isinstance(body["expires_at"], str)


# --------------------------------------------------------------------------------------
# CSRF (DoD 2)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_mutating_request_without_a_csrf_token_is_refused(method: str) -> None:
    app, _ = build_app()
    client = client_for(app)
    sign_in_ok(client)

    response = client.request(method, f"{API_PREFIX}/endpoints")

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_token_invalid"


@pytest.mark.parametrize("token", ["", "   ", "not-the-token", "0" * 43])
def test_a_mutating_request_with_a_bad_csrf_token_is_refused(token: str) -> None:
    app, _ = build_app()
    client = client_for(app)
    sign_in_ok(client)

    response = client.post(f"{API_PREFIX}/endpoints", headers={CSRF_HEADER: token})

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_token_invalid"


def test_csrf_token_is_bound_to_the_session_that_minted_it() -> None:
    """A token that is merely *a* valid token is not enough -- otherwise a global constant,
    or a token lifted from any other session, would pass. Two live sessions, each other's
    tokens, both refused."""
    app, _ = build_app()
    first = client_for(app)
    second = client_for(app)

    first_csrf = sign_in_ok(first)
    second_csrf = sign_in_ok(second)
    assert first_csrf != second_csrf

    assert first.delete(SESSION_PATH, headers={CSRF_HEADER: second_csrf}).status_code == 403
    assert second.delete(SESSION_PATH, headers={CSRF_HEADER: first_csrf}).status_code == 403

    # ...and each one's own token still works, so this is binding, not blanket rejection.
    assert first.delete(SESSION_PATH, headers={CSRF_HEADER: first_csrf}).status_code == 204
    assert second.delete(SESSION_PATH, headers={CSRF_HEADER: second_csrf}).status_code == 204


def test_a_csrf_token_from_an_ended_session_is_refused() -> None:
    app, _ = build_app()
    client = client_for(app)

    csrf = sign_in_ok(client)
    assert client.delete(SESSION_PATH, headers={CSRF_HEADER: csrf}).status_code == 204

    sign_in_ok(client)
    stale = client.delete(SESSION_PATH, headers={CSRF_HEADER: csrf})

    assert stale.status_code == 403


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_a_safe_method_never_needs_a_csrf_token(method: str) -> None:
    app, _ = build_app()
    client = client_for(app)
    sign_in_ok(client)

    response = client.request(method, SESSION_PATH)

    assert response.status_code != 403


def test_sign_in_itself_needs_no_csrf_token() -> None:
    """There is no session yet to bind one to. ``SameSite=Lax`` plus a JSON body -- which
    a cross-site HTML form cannot produce -- is what covers login CSRF instead."""
    app, _ = build_app()
    client = client_for(app)

    assert sign_in(client).status_code == 200


def test_csrf_applies_outside_the_api_prefix_too(tmp_path: Path) -> None:
    """ "Every mutating request" means every one, not every one under the prefix."""
    app, _ = build_app(static_dir=write_console_dist(tmp_path, index_html=SPA_SENTINEL))
    client = client_for(app)
    csrf = sign_in_ok(client)

    without = client.post("/endpoints/42")
    assert without.status_code == 403

    # With the token the request is allowed through to routing, which has no POST route
    # there -- the point is that it got past the CSRF gate, not that it succeeded.
    with_token = client.post("/endpoints/42", headers={CSRF_HEADER: csrf})
    assert with_token.status_code != 403


# --------------------------------------------------------------------------------------
# The credential never leaks (DoD 4)
# --------------------------------------------------------------------------------------


def test_the_password_and_its_hash_never_reach_a_log_record(captured_logs: list[Any]) -> None:
    """Through the engine's real structlog processors, over a full exercise of the module:
    a good sign-in, a bad one, a session read and a sign-out."""
    app, _ = build_app()
    client = client_for(app)

    sign_in(client, password=WRONG_PASSWORD)
    csrf = sign_in_ok(client)
    client.get(SESSION_PATH)
    client.delete(SESSION_PATH, headers={CSRF_HEADER: csrf})
    client.get(f"{API_PREFIX}/endpoints")

    text = rendered(captured_logs)
    assert captured_logs, "the exercise above must actually produce log records"
    assert PASSWORD not in text
    assert WRONG_PASSWORD not in text
    assert PASSWORD_HASH not in text


def test_a_failed_sign_in_does_not_log_the_attempted_username(captured_logs: list[Any]) -> None:
    """An attempted username is very often a password typed one field too high, and
    recording which usernames were tried is an existence oracle for whoever reads the log."""
    app, _ = build_app()
    client = client_for(app)

    sign_in(client, username=PASSWORD, password=WRONG_PASSWORD)

    failures = [e for e in captured_logs if e.get("event") == "console.auth.sign_in_failed"]
    assert len(failures) == 1
    assert PASSWORD not in rendered([failures[0]])
    assert set(failures[0]) <= {"event", "log_level"}


def test_the_session_and_csrf_tokens_never_reach_a_log_record(captured_logs: list[Any]) -> None:
    """The session cookie is a bearer token: a log record containing one is a replayable
    credential sitting in the log pipeline."""
    app, _ = build_app()
    client = client_for(app)

    csrf = sign_in_ok(client)
    token = client.cookies.get(SESSION_COOKIE)
    assert token is not None
    client.get(SESSION_PATH)
    client.delete(SESSION_PATH, headers={CSRF_HEADER: csrf})

    text = rendered(captured_logs)
    assert token not in text
    assert csrf not in text


def test_the_credential_never_appears_in_any_response_body() -> None:
    app, _ = build_app()
    client = client_for(app)

    responses = [
        sign_in(client, password=WRONG_PASSWORD),
        sign_in(client),
        client.get(SESSION_PATH),
        client.get(f"{API_PREFIX}/nope"),
        client.post(f"{API_PREFIX}/nope"),
        client.get("/healthz"),
        client.get("/metrics"),
    ]

    for response in responses:
        assert PASSWORD not in response.text
        assert WRONG_PASSWORD not in response.text
        assert PASSWORD_HASH not in response.text


def test_the_credential_never_appears_in_the_openapi_schema() -> None:
    """T12.8 generates a TypeScript client from this document and it is served to a
    browser; a credential baked into it would be published, not merely logged."""
    app, _ = build_app()

    schema = json.dumps(app.openapi())

    assert PASSWORD not in schema
    assert PASSWORD_HASH not in schema
    assert USERNAME not in schema
    # The sign-in contract itself is still declared -- this is not passing by omitting it.
    assert SESSION_PATH in app.openapi()["paths"]
    assert "SignInRequest" in schema


def test_a_validation_error_on_the_sign_in_body_does_not_echo_the_password() -> None:
    """``password`` is a ``SecretStr`` precisely so pydantic cannot put the value into a
    validation error on its way back out."""
    app, _ = build_app()
    client = client_for(app)

    response = client.post(SESSION_PATH, json={"password": PASSWORD})

    assert response.status_code == 422
    assert PASSWORD not in response.text
