# Contributing to QLabs Catalog Sync

This guide covers the repository structure, hard dependency rules, coding conventions, and the build gate that all contributors must follow.

## Repository layout

This is a `uv` workspace with six packages under `packages/`, each with a `src/` layout:

- **`qlabs-catalog-sync-sdk`** — the public contract, neutral model, shared helpers, and conformance kit (WP1)
- **`qlabs-catalog-sync`** — the engine: discovery, upstream sync loop, state store, scheduler, observability (WP2, WP7)
- **`qlabs-connector-qlik`** — the sole WRITE connector, writes metadata to Qlik Cloud (WP3)
- **`qlabs-connector-databricks`** — read-only source connector, reads from Databricks Unity Catalog (WP4)
- **`qlabs-connector-collibra`** — read-only source connector, reads from Collibra (WP5)
- **`qlabs-connector-snowflake`** — read-only source connector, reads from Snowflake (WP6)

## Hard dependency rule

This rule is strictly enforced and non-negotiable:

- **Connectors depend ONLY on the SDK** (`qlabs-catalog-sync-sdk`) plus their own vendor libraries (e.g. `databricks-sdk`, `httpx`). A connector must never import from the engine or from another connector.
- **The engine depends on the SDK** and discovers connectors at runtime via the `qlabs_catalog_sync.connectors` entry-point group. The engine never imports a connector directly.
- **The SDK depends on neither the engine nor any connector.** It is the public surface; it re-exports the neutral model types so connectors import them from one place.

This architecture lets each package be built, tested, versioned, and shipped on its own cadence.

## Coding conventions

Every package must follow these:

- **Async throughout.** Use `async`/`await` for all I/O. Tests use `pytest-asyncio`.
- **Error handling via SDK typed exceptions.** Raise the SDK exception types (`TransientError`, `AuthError`, `NotFound`, `ConflictError`, `CapabilityError`) so the engine reacts uniformly. Do not invent per-connector exception hierarchies.
- **Structured logging with `structlog`.** Use the context-bound logger from `ConnectorContext`; bind context (endpoint, tenant, entity) rather than formatting it into message strings. Do not log secrets.
- **No secrets in logs or state.** Secrets are redacted by the SDK logger and are never written to the state store. Never log tokens, keys, or credential material.
- **Config via `pydantic-settings`.** Each connector subclasses `ConnectorConfig` to declare its required config and secrets; the engine binds and injects a validated instance. Connectors must not read the environment directly.
- **Tests: `respx` for unit, `vcrpy` for recorded.** Mock HTTP with `respx` in unit tests; use `vcrpy` cassettes for tests against recorded real responses. Every connector must pass the SDK conformance kit.

## Build gate

Run these commands from the repository root before marking anything done. All four must pass:

```bash
uv sync --all-packages         # install every workspace member + dev group
uv run ruff check packages scripts   # lint (never `ruff check .` — planning/ is out of scope)
uv run mypy                    # strict type-check (scoped to packages/*/src)
uv run pytest -q               # tests (pytest-asyncio; respx for unit, vcrpy for recorded)
```

All runtime dependencies are pinned up front. **Contributors must not edit `pyproject.toml` or `uv.lock`** — if you believe you need a new dependency, stop and report it instead of adding it yourself.

## Planning and documentation

`planning/` is a separately-governed Open Knowledge Format bundle with its own hooks, generators, and conformance tooling. **Never hand-edit its concepts.** It contains:

- Research topics under `Research/` (tagged `RS-NN`)
- Roadmap items under `Roadmap/` (tagged `RM-NN`)
- Documentation subjects under `Docu/` (tagged `DC-NN`)
- Agent controls and validation scripts under `.claude/`

Change `planning/` only through its own commands (e.g. `/new-research`, `/new-roadmap`, `/new-docu`, `/complete-roadmap`) and its `planning/.claude/scripts/okf.py` validation tool. The root code project's tooling (ruff, mypy, pytest) must not touch `planning/`, and OKF validation is scoped to the bundle.

The authoritative architectural decisions and mapping rules for v1 live in `planning/Roadmap/completed/RM-01-one-way-sync-mvp/` (decisions D1–D8). Read them before implementing any connector or engine feature.

## Architectural Decision Records (ADRs)

This repository uses ADRs to document technical decisions. ADRs live in `docs/adr/`.

**For architectural decisions already made:** The MVP scope and the eight mapping decisions (D1–D8) that bind the first release are locked in the governed `planning/` bundle at:

- `planning/Roadmap/completed/RM-01-one-way-sync-mvp/decision-databricks-to-qlik-mvp.md` (D1–D8)
- `planning/Roadmap/completed/RM-01-one-way-sync-mvp/decision.md` (v1 scope guardrails)

**For new ADRs:** If you need to document a technical decision taken during implementation, create one in `docs/adr/` following the template at `docs/adr/0000-template.md`. Read `docs/adr/README.md` for the numbering and filing convention.

## v1 scope guardrails (hard limits)

These are the boundaries for the first release. Flag, don't implement, anything that breaks them:

- **Upstream-only.** Metadata flows from source catalogs (Databricks in the MVP) into Qlik. No reverse flow.
- **Qlik is the ONLY write target.** Exactly one write connector: `qlabs-connector-qlik`.
- **Source connectors are read-only.** Databricks, Collibra, and Snowflake implement read paths only — no `create`/`update`/`delete`. Declare writable fields as `ro` (read-only) or `na` (not applicable) in the capability manifest.
- **No two-way sync.** Bidirectional reconciliation is deferred to v2. The only Qlik-side conflict handling in v1 is the manual-edit policy (source-wins overwrite, configurable to preserve local edits).
- **No access-control sync.** Access and authorization are entirely out of v1. Do not implement any access/authorization sync, and do not implement `Principal` or `AccessBinding` entities.
- **Owners are best-effort metadata.** Owner/contact fields are copied as plain metadata correlated on email, with no correctness guarantees. Do not build an identity system.

## Worktree and branch discipline

If you are a coding agent:

- Work is dispatched into isolated git worktrees so parallel agents never share a working tree.
- Branch naming: `wp<N>/<task-id>-<slug>` — for example `wp4/t4-4-databricks-read`.
- Your task owns specific source files and test directories (`owns_paths`). Create, edit, and delete only inside those paths. If your work needs a change elsewhere, stop and report it.
- Reference the task id (e.g. `T4.4`) in the branch, commit subject, and PR description.
- The gate must pass, and you must have seen the output, before marking anything done.

## More information

- **Implementation plan:** `planning/Roadmap/completed/RM-01-one-way-sync-mvp/implementation-plan.md`
- **Agent build guide:** `planning/Roadmap/completed/RM-01-one-way-sync-mvp/agent-guide.md`
- **Connector SDK spec:** `planning/Research/RS-08-connector-plugin-sdk/outputs/connector-sdk-spec.md`
- **Task board:** `planning/tools/agent-plan/tasks.json` (run `python3 planning/tools/agent-plan/ready_queue.py` to see the ready queue)
