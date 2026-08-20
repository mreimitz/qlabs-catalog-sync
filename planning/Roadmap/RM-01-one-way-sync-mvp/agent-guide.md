---
type: "Agent Instruction"
title: "Coding Agent Guide — QLabs Catalog Sync v1 Build"
description: "Conventions, task-board usage, how-to-add-a-connector checklist, and PR/ownership rules for coding agents building the v1 upstream sync."
tags: ["agent", "instruction", "RM-01", "build-guide"]
timestamp: "2026-08-06T13:00:00Z"
status: "active"
---

# Coding Agent Guide — QLabs Catalog Sync v1 Build

This is the entry point for coding agents building the v1 upstream metadata sync (RM-01). Read it
before picking up any task. It tells you how to claim work, what v1 does and does not include, how
the repository is structured, the conventions every package must follow, how to add a new connector,
and how to open a PR that lands cleanly.

## Start here

Work is driven by a machine-readable task board:

- The authoritative board for execution is the JSON at [/tools/agent-plan/tasks.json](/tools/agent-plan/tasks.json).
  It is the single source of truth for what exists, what depends on what, and current status.
- To see what you can pick up right now, run:

  ```
  python3 tools/agent-plan/ready_queue.py
  ```

  It prints the ready queue: every task whose `depends_on` are all `done`.
- The human-readable narrative of the same plan — work packages, waves, acceptance gates, and
  recommended models — is in [implementation-plan.md](implementation-plan.md). Read it for context;
  execute against `tasks.json`.

Rules for the board:

- A task is **ready** when every task listed in its `depends_on` has status `done`. Do not start a
  task before its dependencies are done.
- **Claim** a task by setting its `status` to `"in_progress"` in `tasks.json`. Claim exactly what you
  will work on; do not claim a wave.
- Mark a task `"done"` only after its `verify` step passes (lint, type-check, tests, and any
  task-specific criteria). If `verify` fails, the task is not done.
- Respect parallelism: tasks flagged parallel within a wave have no ordering constraints and may be
  claimed by different agents at the same time.

## v1 scope guardrails

v1 is deliberately narrow. See [decision.md](decision.md) for the full rationale. The hard limits:

- **Upstream-only.** Metadata flows from source catalogs (Databricks, Collibra, Snowflake) *into*
  Qlik. There is no reverse flow in v1.
- **Qlik is the ONLY write target.** Exactly one write connector is built (`qlabs-connector-qlik`).
- **Source connectors are read-only.** Databricks, Collibra, and Snowflake connectors implement
  read paths only. **Do not implement `create`/`update`/`delete` write paths in a source connector.**
  Declare their writable fields as `ro` (read-only) or `na` (not applicable) in the manifest.
- **No two-way sync.** Bidirectional reconciliation and the full conflict engine are deferred to
  RM-02. The only Qlik-side conflict handling in v1 is the manual-edit policy (source-wins overwrite,
  configurable to preserve local edits).
- **No access-control sync.** Access and authorization are entirely out of v1. **Do not implement any
  access/authorization sync**, and do not implement `Principal`/`AccessBinding` entities — declare
  them unsupported. Access observe-and-report is tracked separately (RM-04).
- **Owners are best-effort metadata.** Owner/contact fields are copied as plain metadata correlated on
  email, with no correctness guarantees. Do not build an identity-resolution system out of them.

If a task seems to require a write path in a source connector or any access sync, stop — it is out of
scope. Flag it rather than implementing it.

## Repo layout & package boundaries

Monorepo managed as a `uv` workspace; every package uses a `src/` layout. Packages:

```
qlabs-catalog-sync-sdk      # the contract, neutral model, shared helpers, conformance kit
qlabs-catalog-sync          # the engine: discovery, sync loop, state store, scheduler
qlabs-connector-qlik        # sole WRITE connector
qlabs-connector-databricks  # read-only source connector
qlabs-connector-collibra    # read-only source connector
qlabs-connector-snowflake   # read-only source connector
```

Hard dependency rule (enforced; do not violate):

- **Connectors depend ONLY on the SDK** (`qlabs-catalog-sync-sdk`) plus their own vendor libraries
  (e.g. `databricks-sdk`, `httpx`). A connector must never import from the engine.
- **The engine depends on the SDK** and discovers connectors at runtime via the
  `qlabs_catalog_sync.connectors` entry-point group. The engine never imports a connector directly.
- **The SDK depends on neither the engine nor any connector.** It is the public surface; it re-exports
  the neutral model types so connectors import them from one place.

This lets each package be built, tested, versioned, and shipped on its own cadence.

## Conventions

Every package must follow these:

- **Async throughout.** Use `async`/`await` for all I/O. Tests use `pytest-asyncio`.
- **Error handling via SDK typed exceptions.** Raise the SDK exception types
  (`TransientError`, `AuthError`, `NotFound`, `ConflictError`, `CapabilityError`) so the engine reacts
  uniformly (retry vs skip vs fail). Do not invent per-connector exception hierarchies.
- **Structured logging with `structlog`.** Use the context-bound logger from `ConnectorContext`; bind
  context (endpoint, tenant, entity) rather than formatting it into message strings.
- **No secrets in logs or state.** Secrets are redacted by the SDK logger and are never written to the
  state store. Never log tokens, keys, or credential material.
- **Config via `pydantic-settings`.** Each connector subclasses `ConnectorConfig` to declare its
  required config and secrets; the engine binds and injects a validated instance. Connectors must not
  read the environment directly.
- **Tests: `respx` for unit, `vcrpy` for recorded.** Mock HTTP with `respx` in unit tests; use
  `vcrpy` cassettes for tests against recorded real responses. Every connector must pass the SDK
  conformance kit.
- **`ruff` and `mypy` (strict) must pass.** Lint and strict type-checking are gates. A task is not
  `done` while either fails.

## How to add a connector (checklist)

Follow these steps to add `qlabs-connector-<name>` (mirrors RS-08 section 11):

1. **Create the package.** Add `qlabs-connector-<name>` to the workspace with a `src/` layout and a
   `pyproject.toml`. Its only first-party dependency is `qlabs-catalog-sync-sdk`.
2. **Declare the entry point** in `pyproject.toml` under the
   `[project.entry-points."qlabs_catalog_sync.connectors"]` group. The entry-point name is the stable
   endpoint key used in config, the IdentityMap, and logs.
3. **Subclass `Connector`.** Set `name` (matching the entry-point name) and `ConfigModel`
   (a `ConnectorConfig` subclass declaring config and secrets).
4. **Implement `capabilities()` plus the six methods:** `setup`, `healthcheck`, `list_changed`,
   `read`, `create`, `update`, `delete`. For a **read-only source connector**, implement `read` and
   `list_changed` (plus `setup`/`healthcheck`), and declare writable fields as `ro`/`na` in the
   manifest; the write methods stay unimplemented for the entity types you mark non-writable (the
   conformance kit checks capability honesty rather than a live write).
5. **Write the capability manifest** in `capabilities()`: declare each supported entity, its
   `identity_keys`, per-field `mode` (`rw`/`ro`/`na`), `partial_update`, and the connector's
   `concurrency` (`etag`/`revision`/`none`). Be honest — the engine plans strictly from the manifest.
6. **Pass the SDK conformance kit.** Run the reusable pytest suite (contract, round-trip, idempotency,
   HTTP behavior, capability honesty) with `respx` unit mocks and `vcrpy` cassettes. A connector is
   only "certified" once the kit passes.

Minimal connector skeleton:

```
# qlabs_connector_example/__init__.py
from qlabs_catalog_sync_sdk import (
    Connector, CapabilityManifest, EntityCapability, FieldCapability,
    EntityType, ConnectorConfig, HttpEndpoint,
)

class ExampleConfig(ConnectorConfig):
    base_url: str
    api_key: str

class ExampleConnector(Connector):
    name = "example"
    ConfigModel = ExampleConfig

    async def setup(self, ctx):
        self.http = HttpEndpoint(ctx.config.base_url, auth=("Bearer", ctx.config.api_key))
        self.log = ctx.logger

    def capabilities(self):
        return CapabilityManifest(entities={
            EntityType.DATASET: EntityCapability(
                supported=True,
                identity_keys=["id"],
                fields={
                    "name": FieldCapability(mode="ro"),
                    "description": FieldCapability(mode="ro"),
                    "tags": FieldCapability(mode="ro"),
                    "lineage": FieldCapability(mode="na"),
                },
            )
        }, concurrency="none")

    async def healthcheck(self): ...
    async def list_changed(self, entity_type, since): ...
    async def read(self, ref): ...
    async def create(self, entity): ...   # source connectors leave write paths unimplemented
    async def update(self, ref, diff): ...
    async def delete(self, ref): ...
```

Entry-point declaration:

```
# pyproject.toml
[project.entry-points."qlabs_catalog_sync.connectors"]
example = "qlabs_connector_example:ExampleConnector"
```

Installing the package is all it takes for the engine to discover and use `example`.

## PR, branch & ownership rules

- **One package per agent where possible.** Keep a task's changes inside the package it owns. This
  keeps the parallel streams (engine, Qlik writer, and the three source connectors) independent.
- **Branch naming:** `wp<N>/<task-id>-<slug>` — for example `wp4/t4-4-databricks-read`.
- **PR gate:** a PR must pass the work package's acceptance gate — `ruff` lint, `mypy` (strict) type
  check, and `pytest` — plus any WP-specific criteria, before it can merge.
- **Reference the task id** (e.g. `T4.4`) in the PR title or description so it maps back to the board.
- **Do not edit another task's `owns_paths`.** If your work needs a change in a path another task
  owns, coordinate or open a dependency rather than editing it directly. When your task lands, update
  its status to `done` in `tasks.json`.

## Escalation

Some tasks depend on things a solo agent cannot fully verify — chiefly the open items that need a
live Qlik tenant (the RS-02 tenant-test items: PATCH path enum, change-status body, links payload,
qri stability, role strings). For those:

- **Build mock-first.** Implement against `respx` unit mocks and `vcrpy` cassettes recorded from the
  best-known real responses. Do not block waiting on a live tenant.
- **Flag the uncertainty.** Mark the task and note in the PR exactly which behavior is unverified
  against a real tenant, so it can be confirmed during the WP8 pilot rather than silently assumed.
