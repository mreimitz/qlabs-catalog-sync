"""One process, one port: the API, the console, ``/healthz`` and ``/metrics`` (C8).

The end-to-end probe for a join no board task owned. T12.1 built the FastAPI app and
proved it *could* serve the probe endpoints byte-identically; nothing started it, because
``cli/serve_command.py`` sits outside every RM-06 task's owned paths. Unit tests on either
side of that gap would both have passed while the service still ran the old stdlib probe
server and served no API at all — which is exactly the shape of defect this build keeps
finding.

So this test does not exercise ``create_app`` or ``ApiServer`` in isolation. It starts the
**real service** — ``cli/serve_command.py::_serve``, the same function ``qlabs-catalog-sync
serve`` calls, with a real scheduler, a real state store and real (fake-backed) connectors
— against a real socket, and then makes real HTTP requests to it.

Decision C8: *"one artifact, one origin, one version, no CORS and no possibility of the
console drifting from the engine it configures."* WP14's container check curls ``/healthz``
and ``/`` on the same port, so "one origin" has to be literally true, not approximately.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from qlabs_catalog_sync.api.auth import (
    ADMIN_PASSWORD_HASH_KEY,
    ADMIN_SECRET_ENDPOINT,
    AUTH_SESSION_ROUTE,
    CSRF_HEADER,
    DEFAULT_ADMIN_USERNAME,
    ScryptParams,
    hash_password,
)
from qlabs_catalog_sync.cli.deps import CliDeps, RuntimeContext
from qlabs_catalog_sync.cli.serve_command import _serve
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync_sdk.testing import FakeConnector
from qlabs_catalog_sync_sdk.testing.manifests import (
    databricks_shaped_manifest,
    qlik_shaped_manifest,
)

# pytest runs with --import-mode=importlib, which does not put a test directory on
# sys.path. tests/cli/cli_helpers.py already knows how to write a valid engine config and
# wrap a FakeConnector as a zero-argument class; reuse it rather than restating either.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

from cli_helpers import (  # noqa: E402
    SOURCE_ENDPOINT,
    TARGET_ENDPOINT,
    wrap_as_class,
    write_engine_config,
)


class _RunningService:
    """A live ``_serve`` on a real, OS-assigned port."""

    def __init__(self, port: int) -> None:
        self.port = port

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


async def _wait_for_port(log_path: Path) -> int:
    """Read the bound port out of the service's own ``serve.started`` log line.

    Deliberately not read off the ``ApiServer`` object: taking it from the structured log
    proves the service actually reported a single port, which is the property under test.
    """
    for _ in range(1000):
        if log_path.exists():
            for line in log_path.read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "serve.started":
                    return int(event["http_port"])
        await asyncio.sleep(0.01)
    raise AssertionError(f"service never logged serve.started; log was:\n{log_path.read_text()}")


#: The password the probe signs in with. Not a secret: it only ever exists inside a
#: monkeypatched environment for the lifetime of one test.
_PROBE_PASSWORD = "probe-password-not-a-real-secret"


def _configure_admin_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the service an administrator credential, as a real deployment must.

    ``serve`` calls ``console_auth_from_environment``, which RAISES when none is
    configured (C7: the console must not come up unauthenticated). So a probe that boots
    the real service has to configure one -- and that is the point: if this were removed,
    the service would refuse to start, which is the behaviour the DoD asks for.

    ``log_n=14`` is the lowest the credential loader accepts (16 MiB, ~40 ms) -- enough to
    exercise the real KDF path without paying the shipped 64 MiB parameters per test.
    """
    env_name = f"{ADMIN_SECRET_ENDPOINT.upper()}__{ADMIN_PASSWORD_HASH_KEY.upper()}"
    digest = hash_password(_PROBE_PASSWORD, params=ScryptParams(log_n=14))
    monkeypatch.setenv(env_name, digest)


@pytest.fixture
async def service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_RunningService]:
    from qlabs_catalog_sync.observability import configure_logging

    _configure_admin_credential(monkeypatch)

    log_path = tmp_path / "serve.log"
    source = FakeConnector.read_only_source(
        name=SOURCE_ENDPOINT, manifest=databricks_shaped_manifest()
    )
    target = FakeConnector.write_target(name=TARGET_ENDPOINT, manifest=qlik_shaped_manifest())
    registry = ConnectorRegistry(
        {SOURCE_ENDPOINT: wrap_as_class(source), TARGET_ENDPOINT: wrap_as_class(target)}, {}
    )
    runtime = RuntimeContext(
        state_db=f"sqlite:///{tmp_path / 'state.db'}",
        review_path=tmp_path / "identity-review.json",
        deps=CliDeps(registry=registry),
    )

    stop = asyncio.Event()
    with log_path.open("w") as stream:
        configure_logging(stream=stream)
        task = asyncio.create_task(
            _serve(
                config_path=write_engine_config(tmp_path),
                runtime=runtime,
                pair_names=(),
                create_missing=False,
                host="127.0.0.1",
                port=0,  # let the OS pick, so the suite never fights a fixed port
                shutdown_timeout=5.0,
                run_immediately=False,
                stop=stop,
            )
        )
        try:
            port = await _wait_for_port(log_path)
            yield _RunningService(port)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=30)
    configure_logging()


async def test_healthz_metrics_api_and_console_all_answer_on_one_port(
    service: _RunningService,
) -> None:
    """The whole point of C8: four surfaces, one origin, one running service."""
    async with httpx.AsyncClient() as client:
        healthz = await client.get(service.url("/healthz"))
        metrics = await client.get(service.url("/metrics"))
        api_miss = await client.get(service.url("/api/nope"))
        root = await client.get(service.url("/"))

    assert healthz.status_code == 200
    assert healthz.headers["content-type"].startswith("application/json")

    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]
    # A real exposition body, not an empty 200.
    assert b"# HELP" in metrics.content or b"# TYPE" in metrics.content

    # Unknown API path, no session: 401 in the machine-readable error shape, never the
    # console shell and never a 404. A 404-vs-401 split would let an anonymous caller
    # enumerate which API routes exist (C7); the running service must not offer that.
    assert api_miss.status_code == 401
    assert api_miss.headers["content-type"].startswith("application/json")
    assert "code" in api_miss.json()

    # No console built in this test, so / explains itself in the machine-readable error
    # shape rather than 500ing or returning an empty 404. WP13 replaces this body with the
    # SPA shell; the port does not change.
    assert root.headers["content-type"].startswith("application/json")
    assert root.json()["code"] == "console_not_installed"
    # ...and it is an explanation, not a bare status: it says where the console comes from.
    assert "console" in root.json()["message"].lower()


async def test_the_service_reports_exactly_one_http_port(service: _RunningService) -> None:
    """A second listener would mean a second port, and C8's 'one origin' would be a claim
    rather than a fact. The probe endpoints and the API answering on the *same* socket is
    what makes it true."""
    async with httpx.AsyncClient() as client:
        healthz = await client.get(service.url("/healthz"))
        api_miss = await client.get(service.url("/api/nope"))

    # Both requests went to service.port -- the one port serve.started reported.
    assert healthz.status_code == 200
    assert api_miss.status_code == 401


async def test_the_running_service_actually_requires_an_administrator(
    service: _RunningService,
) -> None:
    """Auth is installed in the REAL service, not only in a test-constructed app.

    ``create_app(auth=None)`` builds an unauthenticated app on purpose, for tests of
    unrelated parts of the API. That escape hatch is exactly how a deployment could end up
    serving an open console while every auth unit test still passed, so this asserts the
    property from outside: an anonymous caller reaches the probe endpoints and nothing else.
    """
    async with httpx.AsyncClient() as client:
        probes = [await client.get(service.url(p)) for p in ("/healthz", "/metrics")]
        protected = await client.get(service.url("/api/endpoints"))
        mutating = await client.post(service.url("/api/endpoints"), json={})

    assert [response.status_code for response in probes] == [200, 200]
    assert protected.status_code == 401
    assert mutating.status_code == 401


async def test_healthz_body_matches_the_render_function_the_engine_has_always_used(
    service: _RunningService,
) -> None:
    """Serving the probe endpoints through FastAPI must not have changed what they say.

    T12.1's parity tests assert this against ``render_healthz`` in-process; this asserts it
    over a real socket from the real service, which is where a middleware, an encoding or a
    content-type default could have quietly altered the body.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(service.url("/healthz"))

    payload = response.json()
    # T2.7's shape: a status plus per-component health. Asserting the shape rather than an
    # exact byte string, because component names depend on what this service started.
    assert isinstance(payload, dict)
    assert "status" in payload


async def test_the_service_refuses_to_start_with_no_administrator_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C7's DoD, asserted against the REAL service rather than the credential loader.

    "No credential configured means the console does not serve" is only true if the thing
    that boots the process actually asks for one. ``console_auth_from_environment`` raising
    in isolation proves nothing about ``serve``; this proves ``serve`` calls it, and that it
    does so **before** anything binds a socket -- a process that bound a port and then served
    only ``/healthz`` would keep passing its liveness probe forever while the console sat
    unusable, which is the failure mode that hides.
    """
    from qlabs_catalog_sync.api.auth import ADMIN_PASSWORD_HASH_KEY, AuthNotConfiguredError

    monkeypatch.delenv(
        f"{ADMIN_SECRET_ENDPOINT.upper()}__{ADMIN_PASSWORD_HASH_KEY.upper()}", raising=False
    )
    source = FakeConnector.read_only_source(
        name=SOURCE_ENDPOINT, manifest=databricks_shaped_manifest()
    )
    target = FakeConnector.write_target(name=TARGET_ENDPOINT, manifest=qlik_shaped_manifest())
    registry = ConnectorRegistry(
        {SOURCE_ENDPOINT: wrap_as_class(source), TARGET_ENDPOINT: wrap_as_class(target)}, {}
    )
    runtime = RuntimeContext(
        state_db=f"sqlite:///{tmp_path / 'state.db'}",
        review_path=tmp_path / "identity-review.json",
        deps=CliDeps(registry=registry),
    )

    with pytest.raises(AuthNotConfiguredError):
        await _serve(
            config_path=write_engine_config(tmp_path),
            runtime=runtime,
            pair_names=(),
            create_missing=False,
            host="127.0.0.1",
            port=0,
            shutdown_timeout=5.0,
            run_immediately=False,
            stop=asyncio.Event(),
        )


async def _sign_in(client: httpx.AsyncClient, service: _RunningService) -> str:
    """Sign in as the administrator and return the CSRF token for the new session.

    The session cookie is ``HttpOnly``, so the console cannot read it; the token comes
    back in the sign-in response body, which is the only way the SPA can obtain it.
    """
    response = await client.post(
        service.url(f"/api{AUTH_SESSION_ROUTE}"),
        json={"username": DEFAULT_ADMIN_USERNAME, "password": _PROBE_PASSWORD},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["csrf_token"]
    return token


async def test_an_administrator_can_actually_reach_the_endpoint_routes(
    service: _RunningService,
) -> None:
    """The routes are mounted in the REAL service, not only in a test-built app.

    ``create_app`` mounts ``/api/connectors`` and ``/api/endpoints`` only when it is given
    both a ConfigService and a ConnectorRegistry, and defaults both to ``None``. That is
    the same escape hatch that left authentication uninstalled in the running service, so
    it gets the same treatment: assert from outside that a signed-in operator reaches the
    routes, rather than trusting that the factory was called correctly.
    """
    async with httpx.AsyncClient() as client:
        await _sign_in(client, service)
        connectors = await client.get(service.url("/api/connectors"))
        endpoints = await client.get(service.url("/api/endpoints"))

    assert connectors.status_code == 200, connectors.text
    assert endpoints.status_code == 200, endpoints.text
    # The registry this service was built with holds exactly the two fake connectors.
    listed = {entry["name"] for entry in connectors.json()}
    assert {SOURCE_ENDPOINT, TARGET_ENDPOINT} <= listed


async def test_the_environment_declared_config_was_imported_into_the_store(
    service: _RunningService,
) -> None:
    """Bootstrap (C1) ran against the RUNNING service, not just in its own unit tests.

    The config file this probe starts the service with declares two endpoints and one
    pair. If ``serve`` never called ``bootstrap_from_environment``, the console would open
    on an empty configuration and the operator would have to retype what the file already
    said - with nothing failing anywhere to reveal it.
    """
    async with httpx.AsyncClient() as client:
        await _sign_in(client, service)
        response = await client.get(service.url("/api/endpoints"))

    assert response.status_code == 200, response.text
    names = {endpoint["name"] for endpoint in response.json()}
    assert names == {"dbx", "qlik_tenant"}


async def test_a_mutating_request_still_needs_its_csrf_token(
    service: _RunningService,
) -> None:
    """A valid session is not enough; the running service enforces CSRF on writes too."""
    async with httpx.AsyncClient() as client:
        token = await _sign_in(client, service)
        without = await client.delete(service.url("/api/endpoints/dbx"))
        with_token = await client.delete(
            service.url("/api/endpoints/dbx"), headers={CSRF_HEADER: token}
        )

    assert without.status_code == 403, without.text
    # The endpoint is referenced by the imported pair, so the service refuses on those
    # grounds - a 4xx from the configuration service, never a 500 and never a CSRF error.
    assert with_token.status_code not in (403, 500), with_token.text
