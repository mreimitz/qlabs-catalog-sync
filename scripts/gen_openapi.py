#!/usr/bin/env python3
"""Export the FastAPI OpenAPI document that ``console/src/api/generated``'s TypeScript
client is generated from (WP12/T12.8).

Usage::

    uv run python scripts/gen_openapi.py            # write console/src/api/generated/openapi.json
    uv run python scripts/gen_openapi.py --check     # exit 1 if the committed file is stale
    uv run python scripts/gen_openapi.py --out PATH  # write somewhere else

``--check`` is the CI gate ``scripts/gen_api_client.sh`` and the task's own verify
command both depend on: it rebuilds the document in memory and diffs it against what is
committed, without touching the file, so an API change that outran regeneration fails the
build instead of shipping a console with a client that silently disagrees with the server.

The trap this module exists to route around
--------------------------------------------

``qlabs_catalog_sync.api.app.create_app`` takes **optional** dependencies that default to
``None``, and each ``None`` silently *removes a whole route group from the application* --
see that module's own docstring. An exporter that calls ``create_app`` the way T12.1's own
test helper does (``health``/``metrics_registry`` only, everything else ``None``) would
produce a schema that is internally consistent, passes its own drift check, and is
*missing every connector, endpoint, pair, selection, preview, history and run-control
route* -- exactly the shape of bug that has bitten this build before: built, tested, and
reachable by nothing.

:func:`build_fully_wired_app` is the fix: it constructs every optional dependency
``create_app`` accepts -- a real, migrated, disposable SQLite state store; a real
``ConfigService``, ``IdentityResolver`` and ``RunRecorder`` sharing that store's engine
(mirrors ``cli/serve_command.py``'s own construction); an empty-but-present
``ConnectorRegistry`` (no connector needs to be *installed* for the schema to be complete
-- the routes are static, only their data varies with what is registered); and a
``ConsoleAuth`` built from a throwaway, export-only credential that is never used to sign
in anything. None of these test doubles' *behaviour* matters here, only their *presence*:
the schema this module cares about is shaped by which routers ``create_app`` mounts, not
by what any of them would do if called. ``tests/api/test_openapi_contract.py`` proves the
resulting document actually has every route group present, checked against this same
function's own app instance -- not a hand-written list this module could drift from.

Why this never touches HTTP
----------------------------

``qlabs_catalog_sync.api.auth`` makes ``/openapi.json``, ``/docs`` and ``/redoc``
deliberately **non-public**: :func:`~qlabs_catalog_sync.api.auth.schema_and_docs_paths`
reads those three URLs off the app and :func:`~qlabs_catalog_sync.api.auth.install_auth`
carves them back out of the "any safe-method request outside the API prefix gets the
console shell" allowance, specifically so an anonymous browser cannot read the
administrative API's shape. A build-time exporter that fetched the schema by starting a
server and issuing a GET to ``/openapi.json`` would therefore get a ``401`` the moment a
real ``ConsoleAuth`` is wired in, and would need to forge a session to work around a
control this codebase installed on purpose. There is no reason to: ``FastAPI.openapi()``
is a plain in-process method that renders the schema from the route table it already
built, no ASGI transport involved, so this module calls it directly on the application
object :func:`build_fully_wired_app` returns.

Determinism
-----------

:func:`render` serializes with ``sort_keys=True``, fixed indentation and a trailing
newline, so the committed file is byte-stable across machines and runs regardless of any
dict-insertion-order accident in FastAPI's or Pydantic's own schema builders. ``title`` and
``version`` are fixed string literals (:data:`APP_TITLE`, :data:`APP_VERSION`) passed
explicitly to ``create_app`` -- never read from the environment or a package version that
drifts between a developer's machine and CI.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from prometheus_client import CollectorRegistry

from qlabs_catalog_sync.api.app import create_app
from qlabs_catalog_sync.api.auth import AdminCredential, ConsoleAuth, ScryptParams, hash_password
from qlabs_catalog_sync.configstore.service import ConfigService
from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync.observability import HealthRegistry
from qlabs_catalog_sync.runs.recorder import RunRecorder
from qlabs_catalog_sync.state.migrate import upgrade_to_head
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync_sdk.config import NullMetrics

#: Where the committed document lives -- inside the directory the generated TypeScript
#: client sits beside, so both are regenerated by the same pair of commands (T12.8's own
#: task boundary; see that module's docstring for why this is not under ``docs/`` or the
#: Python package).
DEFAULT_OUTPUT: Path = (
    Path(__file__).resolve().parent.parent
    / "console"
    / "src"
    / "api"
    / "generated"
    / "openapi.json"
)

#: Fixed, explicit ``title``/``version`` for :func:`~qlabs_catalog_sync.api.app.create_app`.
#: Never read from the environment or ``importlib.metadata`` -- either would make the
#: committed document depend on what happens to be installed when someone regenerates it,
#: which is exactly the kind of accidental drift this script exists to catch, not cause.
APP_TITLE: str = "QLabs Catalog Sync API"
APP_VERSION: str = "0.1.0"

#: The export-only administrator identity :func:`build_fully_wired_app` signs ``ConsoleAuth``
#: up with. Never used to authenticate anything -- ``FastAPI.openapi()`` does not touch the
#: auth middleware at all (see the module docstring) -- it exists purely so ``auth`` is not
#: ``None`` and the sign-in/session routes therefore mount. The password is a fixed literal
#: (not read from the environment or a secret backend): nothing about it is a secret, and a
#: fresh random one on every run would still leave the schema identical since neither the
#: password nor the credential ever appears in the OpenAPI document.
_EXPORT_USERNAME: str = "openapi-export"
_EXPORT_PASSWORD: str = "openapi-export-schema-only-not-a-real-credential"
#: The scrypt cost floor (``log_n=14``, ``auth.py``'s ``ScryptParams`` minimum) rather than
#: the production default (``log_n=16``) -- this hash is derived on every invocation of this
#: script, including every ``--check`` run in CI, and its cost buys nothing here since the
#: credential is never verified against anything.
_EXPORT_SCRYPT_PARAMS = ScryptParams(log_n=14)


def build_fully_wired_app(
    store: StateStore, *, registry: ConnectorRegistry | None = None
) -> FastAPI:
    """Build a :func:`~qlabs_catalog_sync.api.app.create_app` instance with every optional
    dependency supplied, so every router it can mount, does.

    ``store`` is a caller-owned, already-migrated :class:`StateStore` -- this function
    neither migrates nor disposes it, mirroring every ``build_<name>_router`` factory in
    ``api/routes/`` (explicit dependencies, no hidden lifetime). ``registry`` defaults to an
    empty-but-present :class:`~qlabs_catalog_sync.discovery.ConnectorRegistry`: no connector
    needs to be *installed* for the schema to be complete, since every route is static and
    only its *data* varies with what is registered.

    See the module docstring for why every dependency is supplied and why none of the test
    doubles' *behaviour* matters here, only their presence.
    """
    resolved_registry = registry if registry is not None else ConnectorRegistry({}, {})
    config_service = ConfigService(store.engine, resolved_registry)
    resolver = IdentityResolver(
        store, review_path=Path(tempfile.gettempdir()) / "gen-openapi-identity-review.json"
    )
    recorder = RunRecorder.from_store(store)
    credential = AdminCredential.from_password_hash(
        hash_password(_EXPORT_PASSWORD, params=_EXPORT_SCRYPT_PARAMS),
        username=_EXPORT_USERNAME,
    )
    auth = ConsoleAuth(credential=credential)

    return create_app(
        health=HealthRegistry(),
        metrics_registry=CollectorRegistry(),
        static_dir=None,
        auth=auth,
        config_service=config_service,
        registry=resolved_registry,
        store=store,
        resolver=resolver,
        recorder=recorder,
        metrics=NullMetrics(),
        title=APP_TITLE,
        version=APP_VERSION,
    )


def export_schema() -> dict[str, Any]:
    """The exported OpenAPI document, built from a fresh, fully-wired app instance over a
    throwaway state database that is created, migrated, used and disposed within this call.

    ``FastAPI.openapi()`` is a plain in-process method -- see the module docstring for why
    this never starts a server or issues an HTTP request to get the schema.
    """
    with tempfile.TemporaryDirectory(prefix="gen-openapi-") as tmp_dir:
        db_url = f"sqlite:///{Path(tmp_dir) / 'state.db'}"
        upgrade_to_head(db_url)
        store = StateStore.from_url(db_url)
        try:
            app = build_fully_wired_app(store)
            schema: dict[str, Any] = app.openapi()
        finally:
            store.engine.dispose()
    return schema


def render(schema: dict[str, Any]) -> str:
    """``schema`` as the exact bytes this script writes/compares -- stable, sorted keys,
    fixed indentation, a trailing newline. See the module docstring's "Determinism"
    section."""
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _summarize_diff(old_text: str, new_text: str) -> str:
    """A short, useful description of what changed between the committed document and a
    fresh export -- named top-level paths and component schemas added/removed/changed,
    not just "files differ". Falls back to a truncated unified diff when the JSON does not
    parse (should not happen for either side, but a diff summary must never itself raise)."""
    try:
        old_obj = json.loads(old_text)
        new_obj = json.loads(new_text)
    except ValueError:
        return _unified_diff_snippet(old_text, new_text)

    lines: list[str] = []
    lines.extend(_summarize_section(old_obj, new_obj, section="paths", label="route"))
    lines.extend(
        _summarize_section(
            old_obj.get("components", {}),
            new_obj.get("components", {}),
            section="schemas",
            label="component schema",
        )
    )
    if not lines:
        lines.append(
            "the two documents differ, but not in their top-level `paths` or "
            "`components.schemas` keys -- an existing route or model's contents changed "
            "(a field, a type, a description, a response code). Full unified diff:"
        )
        lines.append(_unified_diff_snippet(old_text, new_text))
    return "\n".join(lines)


def _summarize_section(
    old_obj: dict[str, Any], new_obj: dict[str, Any], *, section: str, label: str
) -> list[str]:
    old_keys = set(old_obj.get(section, {}) if isinstance(old_obj, dict) else {})
    new_keys = set(new_obj.get(section, {}) if isinstance(new_obj, dict) else {})
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    lines: list[str] = []
    for name in added:
        lines.append(f"  + {label} added: {name}")
    for name in removed:
        lines.append(f"  - {label} removed: {name}")
    for name in sorted(old_keys & new_keys):
        old_section = old_obj.get(section, {}) if isinstance(old_obj, dict) else {}
        new_section = new_obj.get(section, {}) if isinstance(new_obj, dict) else {}
        if old_section.get(name) != new_section.get(name):
            lines.append(f"  ~ {label} changed: {name}")
    return lines


def _unified_diff_snippet(old_text: str, new_text: str, *, max_lines: int = 60) -> str:
    import difflib

    diff = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile="committed openapi.json",
            tofile="freshly exported openapi.json",
            lineterm="",
        )
    )
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... ({len(diff) - max_lines} more lines)"]
    return "\n".join(diff)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gen_openapi.py",
        description=(
            "Export the OpenAPI document from a fully-wired FastAPI app (every optional "
            "create_app dependency supplied, so every route group mounts) to "
            "console/src/api/generated/openapi.json. Use --check in CI to fail on drift."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Where to write the document (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Do not write anything. Exit 1 (with a diff summary) if the regenerated "
            "document differs from what is already at --out, exit 0 if it matches exactly."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    rendered = render(export_schema())

    if args.check:
        if not args.out.exists():
            print(f"[gen_openapi] {args.out} does not exist -- run without --check.")
            return 1
        on_disk = args.out.read_text(encoding="utf-8")
        if on_disk == rendered:
            print(f"[gen_openapi] {args.out} matches the live FastAPI app.")
            return 0
        print(
            f"[gen_openapi] {args.out} is STALE -- it does not match what the live, "
            "fully-wired FastAPI app exports right now. Run "
            "`uv run python scripts/gen_openapi.py` (no --check), regenerate the "
            "TypeScript client with `scripts/gen_api_client.sh`, and commit both.",
            file=sys.stderr,
        )
        print(_summarize_diff(on_disk, rendered), file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"[gen_openapi] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
