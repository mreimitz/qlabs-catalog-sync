# AGENTS.md — Coding Agent Entry Point

Building the QLabs Catalog Sync v1 upstream metadata sync. Start here, then execute
against the task board.

## 1. Read first

- `planning/Roadmap/RM-01-one-way-sync-mvp/agent-guide.md` — conventions, how to add a
  connector, PR/ownership rules.
- `planning/Roadmap/RM-01-one-way-sync-mvp/implementation-plan.md` — work packages,
  tasks, dependency waves, recommended models.
- `planning/Roadmap/RM-06-sync-console/implementation-plan.md` — the operator console and
  the selection rule engine (WP10-WP14), the other half of the MVP.
- `CLAUDE.md` (root) — dependency rule and v1 scope guardrails.

## 2. Find ready work

```bash
python3 planning/tools/agent-plan/ready_queue.py --roadmap RM-01
```

Prints every task whose `depends_on` are all `done`. Three boards exist and the queue loads all
of them so cross-item dependencies resolve, so always pass a `--roadmap` and never pick up a task
it did not show you:

- `tasks.json` (RM-01) — the engine, WP0-WP4 and WP7-WP9.
- `tasks-rm-06.json` (RM-06) — the operator console and selection, WP10-WP14. Also part of the
  MVP: v0.1 is not tagged until both boards are finished.
- `tasks-rm-05.json` (RM-05) — Track B connectors, blocked until v0.1 ships.

```bash
python3 planning/tools/agent-plan/ready_queue.py --roadmap RM-06
```

Three RM-06 tasks edit files RM-01 owns — `sync/loop.py`, `scheduler.py` and the `Dockerfile`.
Each depends on the RM-01 task that owns the file, so the boards never contend for it; the
ownership rule below is unchanged.

## 3. Claim a task

- Claim by setting the task's `status` to `"in_progress"` on its own board —
  `planning/tools/agent-plan/tasks.json` for RM-01, `tasks-rm-06.json` for RM-06. Claim
  exactly what you will work on — not a whole wave.
- Mark it `"done"` only after its `verify` passes (lint, type-check, tests, plus any
  task-specific criteria).

## 4. Conventions

- **Async throughout** (`async`/`await` for all I/O); tests use `pytest-asyncio`.
- **Pydantic v2** for models; **pydantic-settings** for config (`ConnectorConfig`
  subclasses; never read the environment directly).
- **HTTP via the SDK helper** (`httpx` + `tenacity` retry/backoff honoring
  429/Retry-After); do not call `httpx` directly.
- **Errors via SDK typed exceptions** (`TransientError`, `AuthError`, `NotFound`,
  `ConflictError`, `CapabilityError`); no per-connector hierarchies.
- **Structured logging with `structlog`** (context-bound); never log secrets.
- **Tests:** `respx` for unit mocks, `vcrpy` cassettes for recorded responses; every
  connector must pass the SDK conformance kit.
- **Gates:** `uv run ruff check`, `uv run mypy` (strict), `uv run pytest` must pass.

## 5. Dependency rule

Connectors depend **only** on the SDK (plus vendor libs); the engine depends on the SDK
and discovers connectors via the `qlabs_catalog_sync.connectors` entry-point group.
Nothing imports a connector directly. See `CLAUDE.md`.

## 6. PR / branch / ownership

- **One package per agent** where possible; keep a task's changes inside the package it
  owns.
- **Branch naming:** `wp<N>/<task-id>-<slug>` — e.g. `wp4/t4-4-databricks-read`.
- **Reference the task id** (e.g. `T4.4`) in the PR title/description.
- **Do not edit another task's `owns_paths`.** Coordinate or open a dependency instead.
- **PR gate:** ruff + mypy (strict) + pytest, plus the WP's acceptance criteria.
- **WP completion:** if your task is the last one open in its work package, refresh the
  root `README.md` in the same PR — status table, what works today, and any planned
  behavior the WP made real. Confirm with
  `python3 planning/tools/agent-plan/ready_queue.py --all --roadmap RM-01 --wp WP<N>`.

## 7. Scope

v1 is upstream-only, Qlik is the sole writer, source connectors are read-only, no
two-way sync, no access-control sync. If a task seems to need a source-connector write
path or any access sync, stop and flag it — it is out of scope.

## 8. Implementation lifecycle (hard rule)

Work is built from the roadmap, documented on delivery, and only then retired:

1. It is an `RM-NN` roadmap item. Anything else does not get built.
2. It gets built against that item's board; a task is `done` only after `verify` passes.
3. Its delivery is recorded in `planning/Docu/`, which is organized by subject — one
   folder per part of the system — as what shipped versus what was planned.
4. The item is retired with the bundle's `/complete-roadmap`, which moves it into
   `planning/Roadmap/completed/` in the same transaction.

Never flip a roadmap item to `done` and never move its folder by hand; the bundle's
pre-write hook rejects both. `complete-roadmap` refuses while any task on the board is
unfinished — so marking your task `done` honestly is what eventually unblocks the
release. Completing an item moves paths that this file and `README.md` reference; the
command prints the exact edits to apply.

## 9. `planning/` is off-limits to hand-edits

`planning/` is a strict OKF bundle with its own tooling. Edit it only via its own
commands; never hand-edit its concepts. The only routine change is claiming/completing
a task in `planning/tools/agent-plan/tasks.json`.
