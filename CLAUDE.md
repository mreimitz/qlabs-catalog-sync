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
uv run ruff check packages     # lint — never `ruff check .`, that would lint planning/
uv run mypy                    # strict type-check (scoped to packages/*/src)
```

A change is not done while `ruff`, `mypy` (strict), or `pytest` fails.

**A work package is not done until the root `README.md` matches it.** When the last task of
a work package lands (check with
`python3 planning/tools/agent-plan/ready_queue.py --all --wp WP<N>`), update `README.md` in
the same PR: its status table, the "What works today" and "What does not exist yet" lists,
and any part of the *What it will do* section the WP made real or changed — packages, CLI
commands, config keys, supported entities, capability behavior. The root `README.md` is the
only README in this repository; never create one under `planning/`.

## What ships first

**The MVP is a one-way Databricks-to-Qlik metadata sync** (Track A: WP0-WP4, WP7-WP9). Collibra,
Snowflake, and the Qlik glossary write path are Track B and start only after v0.1 is tagged; their
tasks sit on the board as `blocked`. The mappings the MVP depends on are locked in
`planning/Roadmap/RM-01-one-way-sync-mvp/decision-databricks-to-qlik-mvp.md` — read it before
touching a connector.

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

- Plan: `planning/Roadmap/RM-01-one-way-sync-mvp/implementation-plan.md`
- Guide: `planning/Roadmap/RM-01-one-way-sync-mvp/agent-guide.md`
- Scope decision: `planning/Roadmap/RM-01-one-way-sync-mvp/decision.md`
- Task board: `planning/tools/agent-plan/tasks.json`
  (ready queue: `python3 planning/tools/agent-plan/ready_queue.py`)

## `planning/` is a strict OKF bundle — do not hand-edit it

`planning/` is a separately-governed Open Knowledge Format bundle with its own hooks,
generators, and conformance tooling. **Never hand-edit its concepts.** Change it only
through its own commands (e.g. its `/new-research`, `/new-roadmap` skills and its
`planning/.claude/scripts/okf.py`). This root code project's tooling (ruff, mypy,
pytest, the root `.claude` hooks) must not touch `planning/`, and OKF validation is
scoped to the bundle.
