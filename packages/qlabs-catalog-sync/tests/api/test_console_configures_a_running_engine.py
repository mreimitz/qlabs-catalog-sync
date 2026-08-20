"""The console configures a RUNNING engine: register, define, sync — no restart.

This is the certification probe for RM-06's central claim. Decisions C1 and C6 together
say an operator opens a browser, registers an endpoint against a connector already in the
image, defines a pair, and the engine starts syncing it — without anyone restarting the
service. Everything needed for that was built by separate tasks that could each only see
their own half:

* T12.3 built the endpoint routes, but ``serve`` passed the app no configuration service,
  so the routes were served by nothing.
* T12.9 built scheduler reconcile, but nothing turned it on, and it reported that a pair
  naming a console-registered endpoint *"will fail to build, every time"* because the
  connector pool is built from the YAML config the process started with.
* T12.6 built run control, hit the same wall, and reported that pause/resume could not
  reach a live scheduler at all.

Every one of those tasks passed its own tests. The gap only exists between them, which is
why this test drives the **real service** — ``cli/serve_command.py::_serve``, the same
function the ``serve`` command runs — over real HTTP, and asserts the whole chain.

If this test fails, the console is a web page in front of a database rather than a control
surface for the engine.
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
from qlabs_catalog_sync_sdk.models import DataProduct
from qlabs_catalog_sync_sdk.testing import FakeConnector
from qlabs_catalog_sync_sdk.testing.manifests import (
    databricks_shaped_manifest,
    qlik_shaped_manifest,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

from cli_helpers import SOURCE_ENDPOINT, TARGET_ENDPOINT, wrap_as_class  # noqa: E402

_PROBE_PASSWORD = "probe-password-not-a-real-secret"

#: The environment variables a console-registered endpoint's credentials come from. The
#: console stores the *reference* ``env:NEW_SOURCE``; the value lives only here (C2).
_NEW_SOURCE_REF = "env:NEW_SOURCE"
_NEW_TARGET_REF = "env:NEW_TARGET"


class _Service:
    def __init__(self, port: int, state_db: str) -> None:
        self.port = port
        self.state_db = state_db

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


async def _wait_for(log_path: Path, event: str, *, ticks: int = 3000) -> dict[str, object]:
    for _ in range(ticks):
        if log_path.exists():
            for line in log_path.read_text().splitlines():
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if payload.get("event") == event:
                    return dict(payload)
        await asyncio.sleep(0.01)
    tail = log_path.read_text() if log_path.exists() else "(no log written)"
    raise AssertionError(f"service never logged {event!r}; log was:\n{tail}")


def _empty_config(tmp_path: Path) -> Path:
    """A config file with endpoints but NO pairs.

    The point of the probe is that the pair is created through the console, not declared
    here. Endpoints are declared so the connector *classes* are discoverable, exactly as
    C6 describes ("an instance of a connector that is already present"); the console
    registers its own endpoint rows against them.
    """
    config = {
        "endpoints": {
            "dbx": {"connector": SOURCE_ENDPOINT},
            "qlik_tenant": {"connector": TARGET_ENDPOINT},
        },
        "pairs": [],
    }
    path = tmp_path / "engine.json"
    path.write_text(json.dumps(config))
    return path


@pytest.fixture
async def service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[_Service, FakeConnector]]:
    from qlabs_catalog_sync.observability import configure_logging

    env = f"{ADMIN_SECRET_ENDPOINT.upper()}__{ADMIN_PASSWORD_HASH_KEY.upper()}"
    monkeypatch.setenv(env, hash_password(_PROBE_PASSWORD, params=ScryptParams(log_n=14)))

    source = FakeConnector.read_only_source(
        name=SOURCE_ENDPOINT, manifest=databricks_shaped_manifest()
    )
    source.seed(DataProduct(name="Orders"), native_key="analytics.orders")
    target = FakeConnector.write_target(name=TARGET_ENDPOINT, manifest=qlik_shaped_manifest())
    registry = ConnectorRegistry(
        {SOURCE_ENDPOINT: wrap_as_class(source), TARGET_ENDPOINT: wrap_as_class(target)}, {}
    )

    log_path = tmp_path / "serve.log"
    state_db = f"sqlite:///{tmp_path / 'state.db'}"
    runtime = RuntimeContext(
        state_db=state_db,
        review_path=tmp_path / "identity-review.json",
        deps=CliDeps(registry=registry),
    )

    stop = asyncio.Event()
    with log_path.open("w") as stream:
        configure_logging(stream=stream)
        task = asyncio.create_task(
            _serve(
                config_path=_empty_config(tmp_path),
                runtime=runtime,
                pair_names=(),
                create_missing=True,
                host="127.0.0.1",
                port=0,
                shutdown_timeout=5.0,
                run_immediately=False,
                stop=stop,
            )
        )
        try:
            started = await _wait_for(log_path, "serve.started")
            yield _Service(int(started["http_port"]), state_db), target  # type: ignore[arg-type]
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=30)
    configure_logging()


async def _sign_in(client: httpx.AsyncClient, svc: _Service) -> str:
    response = await client.post(
        svc.url(f"/api{AUTH_SESSION_ROUTE}"),
        json={"username": DEFAULT_ADMIN_USERNAME, "password": _PROBE_PASSWORD},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["csrf_token"]
    return token


async def test_an_operator_can_configure_a_running_engine_from_the_api(
    service: tuple[_Service, FakeConnector], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Register two endpoints, define a pair with a rule, and see it sync — no restart.

    The service starts with NO pairs configured. Everything below is created through HTTP
    against the already-running process, exactly as the console will.
    """
    svc, target = service
    # The credentials a console-registered endpoint resolves through its secret_ref (C2:
    # the store holds the reference, never the value).
    monkeypatch.setenv("NEW_SOURCE__TOKEN", "not-a-real-token")
    monkeypatch.setenv("NEW_TARGET__TOKEN", "not-a-real-token")

    async with httpx.AsyncClient(timeout=30.0) as client:
        csrf = await _sign_in(client, svc)
        headers = {CSRF_HEADER: csrf}

        # 1. The console lists what this image actually contains (C6 - a read, not an install).
        connectors = await client.get(svc.url("/api/connectors"))
        assert connectors.status_code == 200, connectors.text
        assert {SOURCE_ENDPOINT, TARGET_ENDPOINT} <= {c["name"] for c in connectors.json()}

        # 2. Register two endpoints that exist ONLY in the store - never in the config file.
        for name, connector, role, ref in (
            ("console_source", SOURCE_ENDPOINT, "source", _NEW_SOURCE_REF),
            ("console_target", TARGET_ENDPOINT, "target", _NEW_TARGET_REF),
        ):
            created = await client.post(
                svc.url("/api/endpoints"),
                headers=headers,
                json={
                    "name": name,
                    "connector": connector,
                    "role": role,
                    "secret_ref": ref,
                    "settings": {},
                    "enabled": True,
                },
            )
            assert created.status_code in (200, 201), f"{name}: {created.text}"

        # 3. Define the pair.
        pair = await client.post(
            svc.url("/api/pairs"),
            headers=headers,
            json={
                "name": "console-pair",
                "source": "console_source",
                "target": "console_target",
                "target_space": "Console Space",
                "entity_types": ["data_product"],
                "cadence_seconds": 1,
                "enabled": True,
            },
        )
        assert pair.status_code in (200, 201), pair.text
        pair_id = pair.json()["id"]

        # 4. Narrow its scope with a rule, the way the selection screen will.
        rule = await client.post(
            svc.url(f"/api/pairs/{pair_id}/rules"),
            headers=headers,
            json={
                "scope": "object",
                "decision": "include",
                "matcher_kind": "glob",
                "pattern": "analytics.*",
                "ordinal": 0,
            },
        )
        assert rule.status_code in (200, 201), rule.text

        # 5. Run it now, through the console, against the running engine.
        ran = await client.post(
            svc.url(f"/api/pairs/{pair_id}/run-now"), headers=headers, json={}
        )

    assert ran.status_code == 200, (
        "run-now against a console-registered endpoint failed. This is the gap T12.9 "
        f"predicted: the engine could not build a connector it did not start with. {ran.text}"
    )
    payload = ran.json()
    assert payload["pair_name"] == "console-pair", payload
    assert payload["runs"], "run-now reported no cycles at all"

    # 'partial' is the correct verdict for a first sync, not a failure: RM-01 proposes
    # identity matches and binds nothing until a human confirms, so work is outstanding
    # and the watermark is deliberately held. What must NOT happen is 'failed' (nothing
    # committed) or 'skipped' (never ran) - either would mean the console-registered
    # endpoint could not be reached.
    statuses = {run["status"] for run in payload["runs"]}
    assert statuses <= {"ok", "partial"}, payload["runs"]

    # The decisive assertion: the engine actually read the source through an endpoint
    # that exists ONLY because it was registered over HTTP a moment ago. A connector was
    # built from stored configuration, set up, and used.
    assert any(run["counts"]["read"] >= 1 for run in payload["runs"]), payload["runs"]
    assert any(
        record["native_key"] == "analytics.orders"
        for run in payload["runs"]
        for record in run["records"]
    ), "the cycle ran but never saw the object the console's rule selected"


async def test_a_pair_created_through_the_api_is_picked_up_without_a_restart(
    service: tuple[_Service, FakeConnector], monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1's headline: the scheduler notices the new pair on its own, with nobody restarting.

    Distinct from run-now above, which the operator triggers by hand. This asserts the
    reconcile loop actually picked the pair up and gave it a cadence - the difference
    between "the console can poke the engine" and "the console configures the engine".
    """
    svc, target = service
    monkeypatch.setenv("NEW_SOURCE__TOKEN", "not-a-real-token")
    monkeypatch.setenv("NEW_TARGET__TOKEN", "not-a-real-token")

    async with httpx.AsyncClient(timeout=30.0) as client:
        csrf = await _sign_in(client, svc)
        headers = {CSRF_HEADER: csrf}
        for name, connector, role, ref in (
            ("console_source", SOURCE_ENDPOINT, "source", _NEW_SOURCE_REF),
            ("console_target", TARGET_ENDPOINT, "target", _NEW_TARGET_REF),
        ):
            await client.post(
                svc.url("/api/endpoints"),
                headers=headers,
                json={
                    "name": name,
                    "connector": connector,
                    "role": role,
                    "secret_ref": ref,
                    "settings": {},
                    "enabled": True,
                },
            )
        pair = await client.post(
            svc.url("/api/pairs"),
            headers=headers,
            json={
                "name": "console-pair",
                "source": "console_source",
                "target": "console_target",
                "target_space": "Console Space",
                "entity_types": ["data_product"],
                "cadence_seconds": 1,
                "enabled": True,
            },
        )
        assert pair.status_code in (200, 201), pair.text
        pair_id = pair.json()["id"]
        await client.post(
            svc.url(f"/api/pairs/{pair_id}/rules"),
            headers=headers,
            json={
                "scope": "object",
                "decision": "include",
                "matcher_kind": "glob",
                "pattern": "analytics.*",
                "ordinal": 0,
            },
        )

        # Nobody restarts anything. Wait for the scheduler to notice on its own, then for
        # a cycle it scheduled to finish, then read the run out of history.
        deadline = 40.0
        waited = 0.0
        runs: list[dict[str, object]] = []
        while waited < deadline:
            await asyncio.sleep(0.25)
            waited += 0.25
            listed = await client.get(svc.url("/api/runs"))
            if listed.status_code == 200:
                items = listed.json().get("items", [])
                runs = [run for run in items if run.get("pair") == "console-pair"]
                if runs:
                    break

    assert runs, (
        "the scheduler never picked up a pair created through the API. Reconcile (C1) is "
        "not reaching the console's configuration - see serve_command's config_source."
    )
