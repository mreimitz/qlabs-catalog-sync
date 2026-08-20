---
type: "Research Output"
title: "v1 Implementation Plan — Work Packages, Tasks & Model Recommendations"
description: "Complete WP-structured build plan for the upstream metadata sync MVP, with per-task dependencies, parallelization waves, acceptance criteria, and a recommended model per task."
tags: ["roadmap", "RM-01", "implementation-plan", "work-packages", "v1"]
timestamp: "2026-08-06T13:15:00Z"
status: "draft"
---

# v1 Implementation Plan — Work Packages, Tasks & Model Recommendations

This is the executable plan for RM-01: the upstream-only metadata sync MVP (source catalogs to Qlik,
Qlik as sole writer, no two-way sync, no access-control sync — see the v1 scope decision). It is
structured as Work Packages (WPs), each with discrete tasks a coding agent can pick up. Tasks are
labeled with dependencies and a "parallel" flag so multiple agents can work concurrently, and each
task carries a recommended model.

## How coding agents should use this plan

- A task is ready when every task in its "Depends on" column is complete.
- Tasks marked **Parallel: yes** within a wave have no ordering constraints between them and can be
  claimed by different agents simultaneously.
- Each WP has an acceptance gate; do not mark a WP done until its gate passes (lint, type-check,
  tests, and the WP-specific criteria).
- The neutral model (RS-03), architecture (RS-07), connector SDK (RS-08), and vendor references
  (RS-01/02/05/06 plus the RS-02 sync-readiness note) are the source of truth for behavior.

## Recommended-model legend

- **Opus** — high-reasoning tasks: foundational contracts, concurrency/ordering, tricky vendor write
  semantics, identity/diff correctness. Mistakes here are expensive and ripple.
- **Sonnet** — standard implementation: connector read paths, state store, config, scheduler,
  mapping, tests. The default for most tasks.
- **Haiku** — mechanical/boilerplate: scaffolding, config files, capability manifests from a known
  matrix, doc stubs.

## Package topology (from RS-08)

```
qlabs-catalog-sync-sdk      # contract, neutral model, helpers, conformance kit  (WP1)
qlabs-catalog-sync          # engine: discovery, sync loop, state store, scheduler (WP2, WP7)
qlabs-connector-qlik        # sole WRITE connector                                (WP3)
qlabs-connector-databricks  # read-only source connector                          (WP4)
qlabs-connector-collibra    # read-only source connector                          (WP5)
qlabs-connector-snowflake   # read-only source connector                          (WP6)
```

## Parallelization waves (the big picture)

| Wave | Runs | Enables |
| --- | --- | --- |
| 0 | WP0 (repo + tooling) | everything |
| 1 | WP1 SDK — up to the **contract freeze** (T1.1-T1.3) | fan-out |
| 2 | WP1 remainder, WP2 engine, WP3 Qlik, WP4 Databricks, WP5 Collibra, WP6 Snowflake, WP7 start — **all in parallel** | integration |
| 3 | WP7 finish + WP8 integration/pilot | release readiness |
| 4 | WP9 packaging/deploy | v0.1 release |

**Critical path:** WP0 -> WP1 contract freeze -> (WP2 engine + WP3 Qlik writer) -> WP8 pilot -> WP9.
The four connectors (WP3-WP6) are five independent streams once the contract is frozen; staffing them
in parallel is where most wall-clock time is saved.

---

## WP0 — Foundations & repo scaffolding

Goal: a working monorepo with tooling and CI. Gate: `uv sync`, `ruff`, `mypy`, and an empty `pytest`
all pass in CI.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T0.1 | Monorepo + `uv` workspace with the six packages above; shared `pyproject` conventions | — | yes | Haiku |
| T0.2 | Tooling config: `ruff`, `mypy` (strict), `pytest` + `pytest-asyncio`, `pre-commit` | T0.1 | yes | Haiku |
| T0.3 | CI pipeline: lint, type-check, test, build wheels per package | T0.1 | yes | Sonnet |
| T0.4 | Contributor docs: package boundaries, dependency rules (connectors depend only on SDK), ADR location | T0.1 | yes | Haiku |

## WP1 — Connector SDK (`qlabs-catalog-sync-sdk`)

Goal: the public contract + shared helpers + conformance kit. **Contract freeze** after T1.1-T1.3
unblocks all downstream WPs. Gate: SDK unit tests pass; contract version published; a trivial example
connector is discovered by a stub loader.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T1.1 | Neutral model types (pydantic v2): DataProduct, Dataset, GlossaryTerm, Category, Party, Tag, FieldEnvelope, IdentityRef, FieldDiff | T0.2 | no (foundational) | **Opus** |
| T1.2 | Connector ABC: capabilities/setup/healthcheck/list_changed/read/create/update/delete + EntityType, Watermark, ChangeRef, WriteResult, HealthStatus | T1.1 | no | **Opus** |
| T1.3 | CapabilityManifest types (EntityCapability, FieldCapability mode rw/ro/na, partial_update, concurrency) | T1.2 | no | Sonnet |
| T1.4 | HTTP helper (httpx wrapper: base URL, auth injection, timeouts, pooling) + retry/backoff (tenacity, honor 429/Retry-After) + pagination helpers | T1.1 | yes | Sonnet |
| T1.5 | Auth providers base: API key, OAuth2 M2M, JWT/key-pair; in-memory token cache w/ refresh | T1.1 | yes | Sonnet |
| T1.6 | Field envelope + canonical checksum utilities (deterministic normalization for stable diffing) | T1.1 | yes | **Opus** |
| T1.7 | Config base (pydantic-settings) + ConnectorContext + typed exceptions (Transient/Auth/NotFound/Conflict/Capability) | T1.1 | yes | Sonnet |
| T1.8 | Conformance test kit: reusable pytest suite (contract, round-trip, idempotency, capability-honesty) + respx/vcr harness | T1.3, T1.6 | no | Sonnet |
| T1.9 | SDK contract version constant + compatibility gate + entry-point group + packaging docs | T1.2 | yes | Haiku |

## WP2 — Engine core (`qlabs-catalog-sync`)

Goal: discovery, state, the upstream sync loop, scheduling, observability. Gate: engine runs a
dry-run cycle end-to-end against a fake connector; idempotent no-op on re-run.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T2.1 | Entry-point discovery + connector registry + SDK version gate | T1.2, T1.9 | yes | Sonnet |
| T2.2 | State store: SQLAlchemy 2.0 models (IdentityMap, watermarks, last-known field envelopes) + Alembic init on SQLite | T1.1 | yes | Sonnet |
| T2.3 | Config & secrets loading (tenants/endpoints, secret backends via pydantic-settings) | T1.7 | yes | Sonnet |
| T2.4 | Upstream sync loop: poll source -> read to envelopes -> identity resolve -> checksum diff -> write to Qlik -> persist -> advance watermark (one transaction); idempotent skip | T2.2, T2.3, T1.2 | no | **Opus** |
| T2.5 | Manual-edit-on-Qlik policy (source-wins overwrite vs preserve-local, configurable per field/entity) | T2.4 | no | Sonnet |
| T2.6 | Scheduler: APScheduler 3.11 AsyncIOScheduler (or asyncio loop), per-source cadence, jitter, max_instances=1 | T2.4 | yes | Sonnet |
| T2.7 | Observability: structlog (context-bound), prometheus_client metrics, /healthz + /metrics HTTP surface | T2.3 | yes | Sonnet |
| T2.8 | Dry-run mode: compute and log planned writes without applying | T2.4 | yes | Sonnet |

## WP3 — Qlik write connector (`qlabs-connector-qlik`) — CRITICAL PATH, sole writer

Goal: full CRUD into Qlik. Gate: passes the SDK conformance kit (write mode) with recorded Qlik
responses. See the RS-02 reference + sync-readiness note for exact payloads.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T3.1 | Qlik auth (OAuth2 M2M) + HttpEndpoint config (tenant base URL) | T1.4, T1.5 | yes | Sonnet |
| T3.2 | Qlik capability manifest (data product, dataset/item, glossary term/category; rw; ETag concurrency; product arrays partial_update=false) | T1.3 | yes | Haiku |
| T3.3 | read() Qlik entities -> neutral envelopes (needed to diff existing target state) | T3.1 | no | Sonnet |
| T3.4 | create() data products + datasets/items (neutral -> Qlik payloads) | T3.3 | no | **Opus** |
| T3.5 | update() via JSON Patch replace-only, closed path enum, full-replace arrays, ETag if-match, max-8-ops batching | T3.4 | no | **Opus** |
| T3.6 | Glossary terms/categories create+update, term relations/links, change-status action | T3.3 | yes | Sonnet |
| T3.7 | delete()/lifecycle actions (deactivate/move) | T3.4 | yes | Sonnet |
| T3.8 | Conformance tests with respx (unit) + vcr cassettes (recorded real responses) | T3.5, T3.6 | no | Sonnet |

## WP4 — Databricks read connector (`qlabs-connector-databricks`) — read-only

Goal: read UC objects + shares/listings into neutral entities. Gate: conformance kit (read mode).

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T4.1 | Databricks auth (OAuth M2M) + client config (databricks-sdk) | T1.4, T1.5 | yes | Sonnet |
| T4.2 | Capability manifest (ro; identity full_name+object_id; glossary=na; concurrency none) | T1.3 | yes | Haiku |
| T4.3 | list_changed via updated_at + snapshot/checksum comparison | T4.1 | no | Sonnet |
| T4.4 | read() UC catalogs/schemas/tables + shares/listings -> neutral entities with envelopes | T4.1 | no | Sonnet |
| T4.5 | Source-to-neutral mapping (comments/descriptions, tags, owners) | T4.4 | no | Sonnet |
| T4.6 | Conformance tests (respx + vcr) | T4.5 | no | Sonnet |

## WP5 — Collibra read connector (`qlabs-connector-collibra`) — read-only, glossary win

Goal: read data products + business terms (clean glossary source for Qlik). Gate: conformance kit
(read mode); term relations preserved.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T5.1 | Collibra auth (Basic/JWT) + config | T1.4, T1.5 | yes | Sonnet |
| T5.2 | Capability manifest (ro; asset UUID keys; rich glossary + data product) | T1.3 | yes | Haiku |
| T5.3 | list_changed via lastModifiedOn / GraphQL reads | T5.1 | no | Sonnet |
| T5.4 | read() assets, data products, business terms -> neutral (terms, categories) | T5.1 | no | Sonnet |
| T5.5 | Source-to-neutral mapping including term relations/links (relation-graph mapping) | T5.4 | no | **Opus** |
| T5.6 | Conformance tests (respx + vcr) | T5.5 | no | Sonnet |

## WP6 — Snowflake read connector (`qlabs-connector-snowflake`) — read-only

Goal: read objects + listings/shares into neutral entities. Gate: conformance kit (read mode).

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T6.1 | Snowflake auth (key-pair JWT) + connector-python / SQL REST config | T1.4, T1.5 | yes | Sonnet |
| T6.2 | Capability manifest (ro; FQN + listing global name; tags/comments) | T1.3 | yes | Haiku |
| T6.3 | list_changed via SHOW / INFORMATION_SCHEMA / ACCOUNT_USAGE (note ~2h lag) | T6.1 | no | Sonnet |
| T6.4 | read() objects + listings/shares -> neutral entities | T6.1 | no | Sonnet |
| T6.5 | Source-to-neutral mapping (comments, tags) | T6.4 | no | Sonnet |
| T6.6 | Conformance tests (respx + vcr) | T6.5 | no | Sonnet |

## WP7 — Identity & mapping (engine cross-cutting)

Goal: correct identity resolution and diffing. Gate: bootstrap match + re-run stability tests pass.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T7.1 | IdentityMap store + bootstrap matching by natural key (name+type+parent path) with confirmation workflow | T2.2 | no | **Opus** |
| T7.2 | Field diff engine: full-replace vs partial-patch per capability manifest; minimal-mutation output | T1.6, T2.4 | no | **Opus** |
| T7.3 | Owner/Party best-effort email mapping (metadata only; explicitly not an identity system) | T2.4 | yes | Sonnet |
| T7.4 | Neutral status/enum reconciliation (neutral status <-> Qlik lifecycle) | T3.6 | yes | Sonnet |

## WP8 — Integration, E2E & pilot

Goal: prove real upstream flows. Gate: a real data product syncs source->Qlik in dry-run then apply,
idempotent on re-run, resumes from watermark after restart.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T8.1 | Databricks -> Qlik end-to-end pilot (dry-run then apply) on one data product | WP2, WP3, WP4, T7.1, T7.2 | no | **Opus** |
| T8.2 | Collibra -> Qlik glossary end-to-end pilot | WP2, WP3, WP5, T7.1 | yes | Sonnet |
| T8.3 | Snowflake -> Qlik pilot | WP2, WP3, WP6, T7.1 | yes | Sonnet |
| T8.4 | Idempotency + restart-safety tests (re-run no-op, watermark resume) | T8.1 | yes | Sonnet |
| T8.5 | VCR contract tests across all connectors against recorded real responses | T8.1 | yes | Sonnet |
| T8.6 | Resolve RS-02 tenant-test open items (Qlik PATCH path enum, change-status body, links payload, qri stability, role strings) | T3.5 | yes | Sonnet |

## WP9 — Packaging, deployment & ops

Goal: ship v0.1. Gate: container runs the service against config; runbook validated.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T9.1 | Containerize (Dockerfile), single long-running service run shape | WP8 | yes | Haiku |
| T9.2 | Config + secret templates per tenant; secret-backend wiring | WP8 | yes | Sonnet |
| T9.3 | Scheduling cadence config + per-endpoint rate-limit budgeting defaults | WP8 | yes | Sonnet |
| T9.4 | Runbook + capability-matrix docs (generated from manifests) + tag v0.1 release | T9.1, T9.2, T9.3 | no | Haiku |

## Model-mix summary

- **Opus (10 tasks):** T1.1, T1.2, T1.6, T2.4, T3.4, T3.5, T5.5, T7.1, T7.2, T8.1 — the contract,
  determinism, sync loop, Qlik write semantics, relation mapping, identity, and the first pilot.
- **Sonnet (majority):** connector read/write paths, engine plumbing, mapping, tests, packaging.
- **Haiku (7 tasks):** scaffolding, tooling config, capability manifests from a known matrix, docs.

## Suggested staffing (agents in parallel)

Once the WP1 contract is frozen, run up to five streams concurrently: one agent on WP2 (engine), one
on WP3 (Qlik writer — critical path), and one each on WP4/WP5/WP6 (source connectors). WP7 tasks
attach to the engine agent as their dependencies land. Converge on WP8, then WP9.

# Citations

* [Decision: v1 scope — upstream-only, no access control](decision.md) — the scope this plan implements.
* [Neutral Metadata Model Specification (v1)](/Research/RS-03-neutral-metadata-model/outputs/neutral-metadata-model-spec.md) — entities, envelopes, identity map.
* [Standalone Python Sync Service — Architecture & Tech Stack](/Research/RS-07-architecture-techstack-references/outputs/architecture-and-techstack.md) — engine, state store, and stack choices.
* [Connector Plugin SDK — Design Specification (v1)](/Research/RS-08-connector-plugin-sdk/outputs/connector-sdk-spec.md) — SDK contract, discovery, conformance kit.
* [Qlik Two-Way Sync Readiness — Gaps Closed](/Research/RS-02-qlik-catalog-api/notes/qlik-two-way-sync-readiness.md) — Qlik write payloads, ETag concurrency, tenant-test items.
* [Reference Implementations — Data-Product Sync & Catalog Metadata Automation](/Research/RS-07-architecture-techstack-references/outputs/reference-projects.md) — patterns to borrow.
