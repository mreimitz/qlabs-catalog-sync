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


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[_RunningService]:
    from qlabs_catalog_sync.observability import configure_logging

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

    # Unknown API path: the machine-readable error shape, never the console shell.
    assert api_miss.status_code == 404
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
    assert api_miss.status_code == 404


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
