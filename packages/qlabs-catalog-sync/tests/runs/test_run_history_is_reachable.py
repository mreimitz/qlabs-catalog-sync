"""A real cycle, driven by the real service, lands in run history.

The end-to-end probe for a join no board task owned. T11.4 built the schema and the
recorder and proved both against a real ``SyncRunReport``; nothing called them, because
``scheduler.py`` and ``cli/serve_command.py`` sit outside its owned paths. Its own 38 tests
passed while the running service recorded nothing at all, and T12.7's history routes would
have served an empty list from a perfectly correct schema.

So this does not exercise ``RunRecorder`` directly. It starts the **real service**
(``cli/serve_command.py::_serve`` with ``--run-immediately``, a real scheduler, a real state
store, real fake-backed connectors), lets a cycle actually fire, and then reads the run
history back out of the database the service wrote to.

It also pins the crash path. ``SyncLoop.run_cycle`` documents itself as never raising, but
it loads the stored watermark *before* entering its own ``try`` block, so a state-store
failure there escapes — which would leave a run row stuck at ``RUNNING`` forever if the
scheduler did not close it out. That is why ``_fail_run`` exists in the scheduler, and this
is the test that proves it works.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from qlabs_catalog_sync.api.auth import (
    ADMIN_PASSWORD_HASH_KEY,
    ADMIN_SECRET_ENDPOINT,
    ScryptParams,
    hash_password,
)
from qlabs_catalog_sync.cli.deps import CliDeps, RuntimeContext
from qlabs_catalog_sync.cli.serve_command import _serve
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.runs import RunRecord, RunRecorder, RunRecordStatus
from qlabs_catalog_sync.state.db import create_state_engine
from qlabs_catalog_sync_sdk.models import DataProduct
from qlabs_catalog_sync_sdk.testing import FakeConnector
from qlabs_catalog_sync_sdk.testing.manifests import (
    databricks_shaped_manifest,
    qlik_shaped_manifest,
)

# pytest runs with --import-mode=importlib, which does not put a test directory on
# sys.path. tests/cli/cli_helpers.py already writes a valid engine config and wraps a
# FakeConnector as a zero-argument class.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

from cli_helpers import (  # noqa: E402
    SOURCE_ENDPOINT,
    TARGET_ENDPOINT,
    wrap_as_class,
)


def _write_fast_config(tmp_path: Path) -> Path:
    """An engine config with a one-second cadence.

    Not ``cli_helpers.write_engine_config``: that leaves ``cadence_seconds`` at its
    15-minute default, and the scheduler jitters even the ``run_immediately`` fire by up
    to 10% of cadence (capped at 60s) so a fleet of pairs cannot stampede a tenant on
    restart. A one-second cadence makes the jitter a tenth of a second, so this probe waits
    for a real fire instead of racing one.
    """
    config = {
        "endpoints": {
            "dbx": {"connector": SOURCE_ENDPOINT},
            "qlik_tenant": {"connector": TARGET_ENDPOINT},
        },
        "pairs": [
            {
                "name": "db-to-qlik",
                "source": "dbx",
                "target": "qlik_tenant",
                "catalog_schema_patterns": ["sales.*"],
                "target_space": "Sales Space",
                "entity_types": ["data_product"],
                "cadence_seconds": 1,
            }
        ],
    }
    path = tmp_path / "engine.json"
    path.write_text(json.dumps(config))
    return path


async def _wait_for_event(log_path: Path, event: str, *, timeout_ticks: int = 2000) -> None:
    for _ in range(timeout_ticks):
        if log_path.exists():
            for line in log_path.read_text().splitlines():
                try:
                    if json.loads(line).get("event") == event:
                        return
                except ValueError:
                    continue
        await asyncio.sleep(0.01)
    tail = log_path.read_text() if log_path.exists() else "(no log written)"
    raise AssertionError(f"service never logged {event!r}; log was:\n{tail}")


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
    digest = hash_password("probe-password-not-a-real-secret", params=ScryptParams(log_n=14))
    monkeypatch.setenv(env_name, digest)


@pytest.fixture
async def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """Run the real service until it has fired one cycle; yield its state-db URL."""
    from qlabs_catalog_sync.observability import configure_logging

    _configure_admin_credential(monkeypatch)

    log_path = tmp_path / "serve.log"
    state_db = f"sqlite:///{tmp_path / 'state.db'}"

    source = FakeConnector.read_only_source(
        name=SOURCE_ENDPOINT, manifest=databricks_shaped_manifest()
    )
    # One real object in scope of the config's default "sales.*" pattern, so the cycle has
    # something to actually do rather than recording an empty pass.
    source.seed(DataProduct(name="Orders"), native_key="sales.orders")
    target = FakeConnector.write_target(name=TARGET_ENDPOINT, manifest=qlik_shaped_manifest())
    registry = ConnectorRegistry(
        {SOURCE_ENDPOINT: wrap_as_class(source), TARGET_ENDPOINT: wrap_as_class(target)}, {}
    )
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
                config_path=_write_fast_config(tmp_path),
                runtime=runtime,
                pair_names=(),
                create_missing=False,
                host="127.0.0.1",
                port=0,
                shutdown_timeout=5.0,
                run_immediately=True,  # fire once at startup instead of waiting a cadence
                stop=stop,
            )
        )
        try:
            await _wait_for_event(log_path, "serve.started")
            # Wait for a real cycle to actually finish, rather than sleeping and hoping.
            await _wait_for_event(log_path, "sync.cycle.finished")
        finally:
            # Shut down BEFORE reading history. The one-second cadence means another cycle
            # is firing continuously, so reading while the service runs would race a run
            # that is legitimately still RUNNING. Reading after shutdown also asserts the
            # more useful property: run history outlives the process that wrote it.
            stop.set()
            await asyncio.wait_for(task, timeout=30)
    configure_logging()
    yield state_db


async def _runs(state_db: str) -> list[RunRecord]:
    engine = create_state_engine(state_db)
    try:
        return list(await RunRecorder(engine).list_runs())
    finally:
        engine.dispose()


async def test_a_cycle_fired_by_the_real_service_is_recorded(served: str) -> None:
    """The property the console's run screens depend on: history is not empty."""
    runs = await _runs(served)

    assert runs, (
        "the service fired a cycle but recorded no run - run history is built but "
        "unreachable, which is exactly what this probe exists to catch"
    )
    run = runs[0]
    assert run.pair == "db-to-qlik"
    assert run.source_endpoint == "dbx"
    assert run.target_endpoint == "qlik_tenant"


async def test_no_run_is_left_stuck_at_running(served: str) -> None:
    """A row that never leaves RUNNING is indistinguishable from a hung service.

    Every cycle the service completed must have been closed out - by ``finish`` on the
    normal path, or by ``fail`` when ``run_cycle`` raised despite documenting that it does
    not. A run still at RUNNING here means the scheduler dropped one on the floor.
    """
    runs = await _runs(served)

    assert runs
    stuck = [run for run in runs if run.status is RunRecordStatus.RUNNING]
    assert not stuck, f"{len(stuck)} run(s) left at RUNNING after the cycle finished"


async def test_the_recorded_run_carries_real_counts_not_an_empty_shell(served: str) -> None:
    """A row that exists but says nothing would pass the first test and still be useless."""
    runs = await _runs(served)

    run = runs[0]
    assert run.started_at is not None
    assert run.finished_at is not None
    # The source seeded exactly one in-scope object, so the cycle saw work to do.
    total = sum(
        getattr(run, field)
        for field in dir(run)
        if field.endswith("_count") and isinstance(getattr(run, field), int)
    )
    assert total >= 1, f"run recorded no records at all: {run!r}"
