# QLabs Catalog Sync — Code Project Operating Rules

This file governs the **code monorepo at the repository root**. It is a plain Claude
Code / agent instruction file, **not** an OKF concept — the root is not under OKF
governance. (The `planning/` bundle is; see the last section.)

## Repository layout

uv workspace, `src/` layout, six packages under `packages/`:

```
qlabs-catalog-sync-sdk       # the contract, neutral model, shared helpers, conformance kit (WP1)
qlabs-catalog-sync           # the engine: discovery, sync loop, state store, scheduler     (WP2, WP7)
qlabs-connector-qlik         # sole WRITE connector                                          (WP3)
qlabs-connector-databricks   # read-only source connector                                   (WP4)
qlabs-connector-collibra     # read-only source connector                                   (WP5)
qlabs-connector-snowflake    # read-only source connector                                   (WP6)
```

`console/` — the operator console SPA (WP13) — is a top-level sibling of `packages/`, outside the
uv workspace, with its own Node toolchain.

## Hard dependency rule (do not violate)

- **Connectors depend ONLY on the SDK** (`qlabs-catalog-sync-sdk`) plus their own vendor
  libraries (e.g. `databricks-sdk`, `httpx`). A connector must never import from the
  engine or from another connector.
- **The engine depends on the SDK** and discovers connectors at runtime via the
  `qlabs_catalog_sync.connectors` entry-point group. The engine never imports a
  connector directly.
- **Nothing depends on a connector directly.** The SDK depends on neither the engine
  nor any connector; it is the public surface.

## Build / test / lint

```bash
uv sync --all-packages         # install every workspace member + dev group (plain `uv sync` does NOT)
uv run pytest -q               # tests (pytest-asyncio; respx for unit, vcrpy for recorded)
uv run ruff check packages scripts   # lint — never `ruff check .`, that would lint planning/
uv run mypy                    # strict type-check (scoped to packages/*/src)
```

Once `console/` exists (WP13), it carries its own gate — `pnpm -C console typecheck`, `lint`,
`test` and `a11y` — and a change is not done while either gate fails.

A change is not done while `ruff`, `mypy` (strict), or `pytest` fails.

**A work package is not done until the root `README.md` matches it.** When the last task of
a work package lands (check with
`python3 planning/tools/agent-plan/ready_queue.py --all --roadmap RM-01 --wp WP<N>`), update `README.md` in
the same PR: its status table, the "What works today" and "What does not exist yet" lists,
and any part of the *What it will do* section the WP made real or changed — packages, CLI
commands, config keys, supported entities, capability behavior. The root `README.md` is the
only README in this repository; never create one under `planning/`.

## What ships first

**The MVP is a one-way Databricks-to-Qlik metadata sync plus the console that configures it.**
It is two roadmap items on two boards, and v0.1 is not tagged until both are finished:

- **RM-01** — the engine (WP0-WP4, WP7-WP9), 52 tasks on `planning/tools/agent-plan/tasks.json`.
- **RM-06** — the operator console and the selection rule engine (WP10-WP14), 28 tasks on
  `tasks-rm-06.json`. RM-01's T9.4 (tag v0.1) depends on RM-06's last task.

Collibra, Snowflake, and the Qlik glossary write path are RM-05, on their own board
`tasks-rm-05.json`, and start only after v0.1 is tagged; every task there sits as `blocked` until
then. `ready_queue.py` loads every `tasks*.json` and resolves dependencies across all of them, so
always scope with `--roadmap RM-01` or `--roadmap RM-06`.

The mappings the MVP depends on are locked in
`planning/Roadmap/completed/RM-01-one-way-sync-mvp/decision-databricks-to-qlik-mvp.md` (D1-D8) — read it
before touching a connector — and the console's own decisions in
`planning/Roadmap/RM-06-sync-console/decision-console-config-and-selection.md` (C1-C8). C3 widens
D1's glob selector into an ordered rule set; nothing else in D1-D8 changes.

## v1 scope guardrails (upstream-only)

These are hard limits — flag, do not implement, anything that breaks them:

- **Upstream-only.** Metadata flows source catalogs -> Qlik. No reverse flow.
- **Qlik is the ONLY write target.** Exactly one write connector: `qlabs-connector-qlik`.
- **Source connectors are read-only.** Databricks, Collibra, Snowflake implement read
  paths only — no `create`/`update`/`delete`. Declare writable fields `ro`/`na` in the
  capability manifest.
- **No two-way sync.** The full bidirectional conflict engine is deferred to RM-02. The
  only Qlik-side conflict handling in v1 is the manual-edit policy (source-wins
  overwrite, configurable to preserve local edits).
- **No access-control sync.** Access/authorization is entirely out of v1. Do not
  implement any access/authorization sync, and do not implement `Principal`/
  `AccessBinding` entities — declare them unsupported.
- **Owners are best-effort metadata**, correlated on email; never an identity system.

## Where the authoritative plan / board / guide live

All under `planning/` (read them; execute against the board):

- Plan: `planning/Roadmap/completed/RM-01-one-way-sync-mvp/implementation-plan.md`
- Guide: `planning/Roadmap/completed/RM-01-one-way-sync-mvp/agent-guide.md`
- Scope decision: `planning/Roadmap/completed/RM-01-one-way-sync-mvp/decision.md`
- Task board: `planning/tools/agent-plan/tasks.json`
  (ready queue: `python3 planning/tools/agent-plan/ready_queue.py --roadmap RM-01`)
- Console plan: `planning/Roadmap/RM-06-sync-console/implementation-plan.md`
- Console decision: `planning/Roadmap/RM-06-sync-console/decision-console-config-and-selection.md`
- Console board: `planning/tools/agent-plan/tasks-rm-06.json` (RM-06; part of the MVP)
- Track B board: `planning/tools/agent-plan/tasks-rm-05.json` (RM-05; blocked until v0.1)

## Implementation lifecycle (HARD RULE)

Every piece of implementation work follows the same path, and an item is not finished
until all four steps have happened:

1. **It is on the roadmap.** Work that is not an `RM-NN` roadmap item does not get
   built. Create it with the bundle's `/new-roadmap`.
2. **It gets built** against that item's task board, `planning/tools/agent-plan/tasks.json`.
   A task is `done` only after its `verify` command passes.
3. **Its delivery is documented** in `planning/Docu/`, which is organized by subject
   (one folder per part of the system, e.g. the SDK, the Qlik connector, the engine) and
   records **what shipped versus what was planned**: the delivery, how it differed from
   the plan, where the code lives, what was deliberately left out. Create a missing
   subject with the bundle's `/new-docu`.
4. **The roadmap item is retired** with the bundle's `/complete-roadmap`, which moves it
   into `planning/Roadmap/completed/` in the same transaction that records the increment.

Never mark a roadmap item done by hand and never move its folder yourself — the bundle's
pre-write hook rejects both. `complete-roadmap` refuses while any task on the item's
board is unfinished, so keeping `tasks.json` honest is what makes completion possible.

Completing an item moves its folder, which invalidates paths in this file, `README.md`,
`AGENTS.md` and the board's `inputs` entries. The command prints exactly what to fix and
never edits outside the bundle; apply the edits, then confirm with
`python3 planning/.claude/scripts/okf.py --root planning check-references --tag RM-NN`.

## `planning/` is a strict OKF bundle — do not hand-edit it

`planning/` is a separately-governed Open Knowledge Format bundle with its own hooks,
generators, and conformance tooling. **Never hand-edit its concepts.** Change it only
through its own commands (e.g. its `/new-research`, `/new-roadmap`, `/new-docu` and
`/complete-roadmap` skills and its `planning/.claude/scripts/okf.py`). This root code
project's tooling (ruff, mypy, pytest, the root `.claude` hooks) must not touch
`planning/`, and OKF validation is scoped to the bundle.
