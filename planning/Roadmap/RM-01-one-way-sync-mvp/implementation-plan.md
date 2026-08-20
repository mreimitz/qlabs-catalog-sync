---
type: "Research Output"
title: "v1 Implementation Plan — Work Packages, Tasks & Model Recommendations"
description: "Executable build plan for the Databricks-to-Qlik upstream sync MVP, with locked mapping decisions, per-task dependencies and file ownership, parallelization waves, acceptance gates, and a recommended model per task."
tags: ["roadmap", "RM-01", "implementation-plan", "work-packages", "v1"]
timestamp: "2026-08-20T11:05:00Z"
status: "draft"
---

# v1 Implementation Plan — Work Packages, Tasks & Model Recommendations

This is the executable plan for RM-01. It is structured as Work Packages (WPs), each with discrete
tasks a coding agent can pick up. Tasks carry dependencies, an owned file set, a definition of done,
a runnable verify command, and a recommended model.

## What the MVP is

**The MVP is a one-way Databricks-to-Qlik metadata sync.** One source (Databricks Unity Catalog),
one target (Qlik Cloud), upstream only. Everything needed to ship that — and nothing else — is
**Track A**, and Track A *is* RM-01. Collibra and Snowflake are **Track B**: they start only after
Track A has shipped v0.1, and they must not be staffed while Track A is open. Track B is now its own
roadmap item, [RM-05](/Roadmap/RM-05-track-b-connectors-glossary/item.md), on its own board — so
RM-01 completes at the moment the software ships rather than waiting on work that begins afterwards.

| | Track A — RM-01 (the MVP, ships v0.1) | Track B — RM-05 (after v0.1) |
| --- | --- | --- |
| Board | `tools/agent-plan/tasks.json`, 52 tasks | `tools/agent-plan/tasks-rm-05.json`, 15 tasks |
| Work packages | WP0, WP1, WP2, WP3, WP4, WP7, WP8, WP9 | WP5 (Collibra), WP6 (Snowflake), Qlik glossary write path |
| Source connectors | Databricks (read-only) | Collibra, Snowflake (read-only) |
| Entities | DataProduct, Dataset, Party, Tag | + GlossaryTerm, Category |
| Pilots | T8.1 Databricks -> Qlik | T8.2 Collibra glossary, T8.3 Snowflake |

Every task on the RM-05 board is `blocked`, so none of them surfaces in the ready queue while Track A
is in flight. Their dependencies still point back into this board; the ready queue loads both and
resolves across them. Scope the queue with `--roadmap RM-01` while building the MVP.

The v1 scope guardrails from the [scope decision](decision.md) are unchanged and absolute: upstream
only, Qlik is the sole writer, source connectors are read-only, no two-way sync, no access-control
sync, owners are best-effort metadata.

## How coding agents should use this plan

- The executable board is [/tools/agent-plan/tasks.json](/tools/agent-plan/tasks.json). This document
  is the narrative; the board is the source of truth for status and readiness.
- A task is ready when every task in its `depends_on` is `done`. Run
  `python3 planning/tools/agent-plan/ready_queue.py` to see the ready set.
- **Never write outside your task's `owns_paths`.** Every task owns its source files *and* its test
  directory, so parallel agents in separate worktrees never touch the same file. If your work needs a
  change in someone else's path, stop and raise it — do not edit it.
- Do not mark a task `done` until its `verify` command passes **and** the repository gate passes:

  ```bash
  uv sync --all-packages
  uv run ruff check packages
  uv run mypy
  uv run pytest -q
  ```

- The neutral model (RS-03), architecture (RS-07), connector SDK (RS-08), and the vendor references
  (RS-01 Databricks, RS-02 Qlik plus its sync-readiness note) are the source of truth for behavior.
  Where this plan and a research document disagree, this plan wins — the differences are deliberate
  and recorded under "Locked mapping decisions" below.

## Recommended-model legend

- **Opus** — high-reasoning tasks: foundational contracts, concurrency/ordering, tricky vendor write
  semantics, identity/diff correctness. Mistakes here are expensive and ripple.
- **Sonnet** — standard implementation: connector read paths, state store, config, scheduler,
  mapping, capability manifests, tests. The default for most tasks.
- **Haiku** — mechanical/boilerplate only: doc stubs, Dockerfile, generated docs. Anything that feeds
  engine planning (capability manifests) is Sonnet, not Haiku, because a dishonest manifest silently
  corrupts every write plan.

## Locked mapping decisions (MVP)

These close the gaps that made the previous revision unbuildable. They are binding for Track A.

**D1 — A Unity Catalog schema is the data product.** `catalog.schema` maps to one Qlik data product;
the tables and views inside it map to that product's datasets. Databricks has no first-class data
product object, and UC schemas are always present, carry `comment` + `owner` + tags, and need no
Delta Sharing or Marketplace setup. Which schemas sync is a config selector (a list of
`catalog.schema` glob patterns per pair). Delta shares and Marketplace listings are Track B or later.

**D2 — The connector never creates Qlik datasets.** A Qlik dataset is a Qlik-native resource bound to
a QRI and a Qlik data connection; it cannot be conjured for an arbitrary Databricks table. The Qlik
writer resolves a product's `datasetIds` against datasets that **already exist** in the target space —
first through the IdentityMap, then by name match within the space. Members that do not resolve are
omitted from the payload and listed in the run report as unresolved. They are never invented.

**D3 — `keyContacts` needs a Qlik `userId`, not an email.** Databricks owners arrive as emails or
service-principal application IDs. The Qlik connector resolves them through the users API
(`GET /api/v1/users` filtered by email) and caches the mapping. No match means the contact is dropped
and reported. This stays best-effort metadata and must not grow into an identity system.

**D4 — v1 never deletes in Qlik.** A source object that disappears is reported as an orphan, not
deleted or deactivated. The Qlik connector still implements `delete()` and the lifecycle actions so
the contract and the conformance kit are complete, but the engine has no code path that calls them
in v1. Removing this safety catch is an RM-02 decision.

**D5 — Glossary is out of the MVP.** Databricks has no native glossary, so a Databricks-to-Qlik sync
has nothing to put in one. The Qlik glossary write path (terms, categories, relations, links,
change-status) moves to Track B where Collibra gives it a real source.

**D6 — UC tags require SQL, and the manifest must say so.** Object tags are readable only through
`INFORMATION_SCHEMA.*_TAGS` over the Statement Execution API. The Databricks connector declares
`tags` as a supported read **only when** a `sql_warehouse_id` is configured; otherwise the manifest
declares `tags` as `na`. An honest manifest beats a silently empty field.

**D7 — Status maps to Qlik activation, and activation is opt-in.** Neutral `active` corresponds to an
activated Qlik data product (the `activate` action, managed space only); every other neutral status
leaves the product deactivated. Because activation makes a product discoverable tenant-wide, it is
off by default and enabled per pair in config.

**D8 — The connector contract is async, and `list_changed` returns a watermark.** RS-08 sketches the
ABC with synchronous methods and an `Iterable[ChangeRef]`; RS-07 shows async and a proposed next
watermark. The binding form is RS-07's: every method is `async`, and `list_changed` returns a
`ListChangedResult(changes, next_watermark)` so the engine can commit envelopes and the watermark in
one transaction. Without the returned watermark, restart safety is not achievable.

## Package topology (from RS-08)

```
qlabs-catalog-sync-sdk      # contract, neutral model, helpers, conformance kit  (WP1)
qlabs-catalog-sync          # engine: discovery, sync loop, state store, scheduler (WP2, WP7)
qlabs-connector-qlik        # sole WRITE connector                                (WP3)
qlabs-connector-databricks  # read-only source connector                          (WP4)
qlabs-connector-collibra    # read-only source connector             (WP5, RM-05 Track B)
qlabs-connector-snowflake   # read-only source connector             (WP6, RM-05 Track B)
```

## Parallelization waves

| Wave | Runs | Peak agents | Enables |
| --- | --- | --- | --- |
| 0 | WP0 — repair the gate, pin dependencies, CI | 1-2 | everything |
| 1 | WP1 to the **contract freeze** (T1.1 -> T1.2 -> T1.3) plus the parallel helpers T1.4-T1.7 | 4-5 | fan-out |
| 2 | WP1 remainder, WP2 engine, WP3 Qlik, WP4 Databricks, WP7 identity/diff — **all in parallel** | up to 9 | integration |
| 3 | WP8 integration, conformance, idempotency, tenant checklist | 4-5 | release readiness |
| 4 | WP9 packaging/deploy, v0.1 tag | 3 | **RM-01 complete** |
| 5 | RM-05 Track B: WP5 Collibra, WP6 Snowflake, Qlik glossary | 4-6 | a separate roadmap item |

Wave 4 is where RM-01 ends. Tagging v0.1 is not the last step: the delivery is recorded in `Docu/`
and the item is retired to `Roadmap/completed/` with `complete-roadmap`, which refuses while any
task on this board is unfinished.

**Critical path:** T0.5 -> T1.1 -> T1.2 -> T1.3 (contract freeze) -> T2.4 sync loop + T3.5 Qlik
update -> T8.1 pilot -> T9.4 release. Everything else hangs off that spine and should be staffed
concurrently.

---

## WP0 — Foundations & repo scaffolding

Goal: a workspace whose gate is actually green, with every third-party dependency already pinned so
no downstream agent ever has to touch `pyproject.toml` or the lock file.

**The gate is red today.** The packages, tooling config, and CI file exist, but `uv sync` does not
install the workspace members, `ruff .` lints `planning/` (which this repo's tooling must not touch),
`mypy` chokes on duplicate `test_smoke` module names, and pytest cannot collect for the same reason.
T0.5 fixes exactly that and is the first task in the build.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T0.1 | Monorepo + `uv` workspace with the six packages; shared `pyproject` conventions | — | yes | Haiku |
| T0.2 | Tooling config: `ruff`, `mypy` (strict), `pytest` + `pytest-asyncio`, `pre-commit` | T0.1 | yes | Haiku |
| T0.3 | CI pipeline: lint, type-check, test, build wheels per package | T0.5 | yes | Sonnet |
| T0.4 | Contributor docs: package boundaries, dependency rules, ADR location | T0.1 | yes | Haiku |
| T0.5 | **Repair the gate**: `uv sync --all-packages`, commit `uv.lock`, exclude `planning/` from ruff, scope mypy to the six `src` trees, set pytest `import-mode=importlib` | T0.2 | no | Sonnet |
| T0.6 | **Pin every runtime dependency up front** in all six `pyproject.toml` files and the lock, so no downstream task edits packaging metadata (removes the lock-file merge conflict between parallel worktrees) | T0.5 | no | Sonnet |

## WP1 — Connector SDK (`qlabs-catalog-sync-sdk`)

Goal: the public contract + shared helpers + conformance kit. **Contract freeze** after T1.1-T1.3
unblocks every downstream WP; treat those three as one uninterruptible chain. Gate: SDK unit tests
pass, contract version published, and the bundled fake connector is discovered by a stub loader.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T1.1 | Neutral model types (pydantic v2): DataProduct, Dataset, GlossaryTerm, Category, Party, Tag, FieldEnvelope, IdentityRef, FieldDiff | T0.6 | no (foundational) | **Opus** |
| T1.2 | Connector ABC (**all methods async**, per D8) + EntityType, Watermark, ChangeRef, `ListChangedResult`, WriteResult, HealthStatus; `FieldDiff` carries per-field full-replace vs partial intent | T1.1 | no | **Opus** |
| T1.3 | CapabilityManifest types (EntityCapability, FieldCapability mode rw/ro/na, `partial_update`, `allowed_update_paths`, concurrency) | T1.2 | no | Sonnet |
| T1.4 | HTTP helper (httpx wrapper: base URL, auth injection, timeouts, pooling) + retry/backoff (tenacity, honor 429/`Retry-After`) + cursor and offset pagination helpers | T1.1 | yes | Sonnet |
| T1.5 | Auth providers base: API key, OAuth2 M2M, JWT/key-pair; in-memory token cache with refresh-before-expiry | T1.1 | yes | Sonnet |
| T1.6 | Field envelope + canonical checksum utilities (deterministic normalization: key order, whitespace, timezone, array ordering) for stable diffing | T1.1 | yes | **Opus** |
| T1.7 | Config base (pydantic-settings) + ConnectorContext + typed exceptions (Transient/Auth/NotFound/Conflict/Capability) + secret-redacting structlog processor | T1.1 | yes | Sonnet |
| T1.8 | Conformance test kit: reusable pytest suite (contract, round-trip, idempotency, HTTP behavior, capability honesty) + respx/vcr harness | T1.10 | no | Sonnet |
| T1.9 | SDK contract version constant + compatibility gate + entry-point group + packaging docs | T1.2 | yes | Haiku |
| T1.10 | **`FakeConnector` test double** shipped from the SDK: in-memory read/write connector with a configurable manifest, used by the conformance kit and by every engine test | T1.3, T1.6 | no | Sonnet |

## WP2 — Engine core (`qlabs-catalog-sync`)

Goal: discovery, state, the upstream sync loop, scheduling, observability. Gate: the engine runs a
dry-run and an apply cycle end-to-end against `FakeConnector`, and a re-run is a byte-identical no-op.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T2.1 | Entry-point discovery + connector registry + SDK contract-version gate | T1.2, T1.9 | yes | Sonnet |
| T2.2 | State store: SQLAlchemy 2.0 models (IdentityMap, watermarks, last-known field envelopes, orphan log) + Alembic init on SQLite, WAL mode | T1.1 | yes | Sonnet |
| T2.3 | Config & secrets loading **including the sync-pair schema**: source endpoint, UC `catalog.schema` selector patterns, target Qlik space, entity types, cadence, manual-edit policy, activation opt-in | T1.7 | yes | Sonnet |
| T2.4 | Upstream sync loop: poll source -> read to envelopes -> identity resolve -> checksum diff -> write to Qlik -> persist envelopes + advance watermark in **one transaction**; idempotent skip on unchanged checksums | T1.2, T1.10, T2.2, T2.3, T7.1, T7.2 | no | **Opus** |
| T2.5 | Manual-edit-on-Qlik policy (source-wins overwrite by default, preserve-local configurable per field/entity) | T2.4 | no | Sonnet |
| T2.6 | Scheduler: APScheduler 3.11 `AsyncIOScheduler`, per-pair cadence, jitter, `max_instances=1` | T2.4 | yes | Sonnet |
| T2.7 | Observability: structlog (context-bound), prometheus_client metrics, `/healthz` + `/metrics` HTTP surface | T2.3 | yes | Sonnet |
| T2.8 | CLI + dry-run mode: compute the planned write set, emit it as a **machine-readable JSON plan file** plus human log, apply zero mutations | T2.4 | yes | Sonnet |
| T2.9 | **Orphan policy (D4)**: a source object that vanishes is recorded in the orphan log and surfaced in the run report; the engine has no delete path in v1 | T2.4 | yes | Sonnet |

## WP3 — Qlik write connector (`qlabs-connector-qlik`) — CRITICAL PATH, sole writer

Goal: data-product CRUD into Qlik plus the reference resolution the writer depends on. Gate: passes
the SDK conformance kit in write mode against respx mocks and hand-authored cassettes. Glossary is
Track B (D5).

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T3.1 | Qlik auth (OAuth2 M2M) + tenant base-URL config | T1.4, T1.5 | yes | Sonnet |
| T3.2 | Qlik capability manifest: data product + dataset rw, ETag concurrency, product arrays `partial_update=false`, the closed 8-path PATCH enum as `allowed_update_paths`, glossary declared unsupported in v1 | T1.3 | yes | Sonnet |
| T3.3 | `read()` Qlik data products + items/datasets into neutral envelopes (Items API paging, `secureQri` from `resourceAttributes`) | T3.1 | no | Sonnet |
| T3.4 | `create()` data products from neutral entities (name/description/readMe/tags/spaceId/datasetIds/keyContacts, subset and cap rules enforced) | T3.2, T3.3, T3.9 | no | **Opus** |
| T3.5 | `update()` via JSON Patch: replace-only, closed path enum, full-replace arrays, ETag `if-match`, max-8-op batching, 412 -> re-read/re-diff | T3.4 | no | **Opus** |
| T3.7 | Lifecycle actions (activate/deactivate/move) + `delete()` for contract completeness; **not called by the engine in v1** (D4) | T3.4 | yes | Sonnet |
| T3.8 | Conformance tests with respx (unit) + vcr cassettes | T1.8, T3.5, T3.7, T3.9 | no | Sonnet |
| T3.9 | **Reference resolution (D2, D3)**: resolve `datasetIds` against existing Qlik datasets (IdentityMap first, then name-within-space), resolve owner emails to Qlik `userId` via the users API, cache both, report every miss | T3.3 | no | Sonnet |

## WP4 — Databricks read connector (`qlabs-connector-databricks`) — read-only

Goal: read UC schemas as data products and their tables/views as datasets (D1). Gate: conformance kit
in read mode; the manifest matches what the connector can actually do.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T4.1 | Databricks auth (OAuth M2M) + `databricks-sdk` client config (workspace host, `sql_warehouse_id` optional) | T1.4, T1.5 | yes | Sonnet |
| T4.2 | Capability manifest: read-only; identity `full_name` + object id; glossary `na`; concurrency `none`; `tags` supported only when a SQL warehouse is configured (D6) | T1.3 | yes | Sonnet |
| T4.3 | `list_changed`: UC `updated_at` on schemas/tables plus snapshot-checksum comparison, returning `ListChangedResult` with the proposed next watermark | T4.1 | no | Sonnet |
| T4.4 | `read()`: UC schema -> DataProduct, its tables/views -> Dataset, with paging over `/schemas` and `/tables` | T4.1 | no | Sonnet |
| T4.5 | Source-to-neutral mapping: `comment` -> description, `owner` -> Party (email/SP id), `full_name` -> physicalRef, `properties` -> customAttributes | T4.4 | no | Sonnet |
| T4.6 | Conformance tests (respx + vcr) | T1.8, T4.3, T4.5, T4.7 | no | Sonnet |
| T4.7 | **Tag read path (D6)**: `INFORMATION_SCHEMA.SCHEMA_TAGS` / `TABLE_TAGS` over the Statement Execution API, config-gated on `sql_warehouse_id`; absent warehouse means the manifest reports `tags` as `na` | T4.4 | no | Sonnet |

## WP5 — Collibra read connector — **RM-05, blocked until v0.1 ships**

Goal: read data products + business terms as the clean glossary source for Qlik. Tasks T5.1-T5.6 are
unchanged; they now live on the RM-05 board with status `blocked`.

## WP6 — Snowflake read connector — **RM-05, blocked until v0.1 ships**

Goal: read objects + listings/shares into neutral entities. Tasks T6.1-T6.6 are on the RM-05 board
and stay `blocked`.

## WP7 — Identity & mapping (engine cross-cutting)

Goal: correct identity resolution and minimal diffs. Gate: bootstrap match plus re-run stability
tests pass against `FakeConnector`.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T7.1 | IdentityMap store + bootstrap matching by natural key (name + type + parent path) with an explicit confirmation step exposed as a CLI subcommand | T2.2 | no | **Opus** |
| T7.2 | Field diff engine: full-replace vs partial-patch driven by the capability manifest; minimal-mutation output; widens a partial diff to a full array when `partial_update=false` | T1.3, T1.6 | yes | **Opus** |
| T7.3 | Owner/Party best-effort email correlation (metadata only; explicitly not an identity system) | T1.1 | yes | Sonnet |
| T7.4 | Neutral status -> Qlik activation reconciliation (D7), activation opt-in per pair | T3.7 | yes | Sonnet |

## WP8 — Integration, E2E & release readiness

Goal: prove the Databricks-to-Qlik flow without a live tenant. **No live Databricks workspace or Qlik
tenant is available during this build**, so every test runs against respx mocks, `FakeConnector`, and
hand-authored cassettes derived from the documented payloads in RS-01 and RS-02. Behavior that only a
real tenant can confirm is registered, not assumed.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T8.1 | Databricks -> Qlik end-to-end pilot against mocks: one UC schema becomes one Qlik data product, dry-run then apply, asserted against an expected target state | WP2, WP3, WP4, T7.1, T7.2 | no | **Opus** |
| T8.4 | Idempotency + restart-safety tests: re-run is a no-op, watermark resumes after a simulated crash, a mid-cycle failure commits nothing | T8.1 | yes | Sonnet |
| T8.5 | VCR contract tests for the Qlik and Databricks connectors against recorded responses | T8.1 | yes | Sonnet |
| T8.6 | **Tenant-verification package** (replaces "resolve the open items"): a `TENANT_UNVERIFIED` registry in code marking each assumption, a runnable `scripts/tenant_probe.py` that exercises them against a real tenant, and a checklist doc a human runs before production | T3.5, T3.7 | yes | Sonnet |

Track B pilots T8.2 (Collibra glossary) and T8.3 (Snowflake) moved to the RM-05 board and stay
`blocked`.

## WP9 — Packaging, deployment & ops

Goal: ship v0.1. Gate: the container runs the service against config; the runbook is validated.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T9.1 | Containerize (Dockerfile), single long-running service run shape | T8.1, T8.4, T8.5, T8.6 | yes | Haiku |
| T9.2 | Config + secret templates per tenant; secret-backend wiring | T8.1, T8.4, T8.5, T8.6 | yes | Sonnet |
| T9.3 | Scheduling cadence config + per-endpoint rate-limit budgeting defaults | T8.1, T8.4, T8.5, T8.6 | yes | Sonnet |
| T9.4 | Runbook + capability-matrix docs generated from manifests + tag v0.1 | T9.1, T9.2, T9.3 | no | Haiku |

## Known-unverified behavior

These ship in the MVP as documented assumptions, each marked in code and listed in the T8.6
checklist. None of them blocks the build.

- The glossary-term PATCH path enum, the `change-status` request body key, and the POST `/links`
  payload shape (Track B relevance only, but registered now).
- Whether the Data Products PATCH endpoint honors `if-match`/ETag — undocumented; the writer sends it
  and tolerates its absence.
- `qri`/`secureQri` preservation across a space move and across tenants.
- The exact custom-role permission strings a Qlik sync service account needs.
- Databricks rate-limit behavior under the chosen poll cadence.

## Model-mix summary

- **Opus (7 Track A tasks):** T1.1, T1.2, T1.6, T2.4, T3.4, T3.5, T7.1, T7.2, T8.1 — the contract,
  determinism, the sync loop, Qlik write semantics, identity, diffing, and the pilot.
- **Sonnet (the majority):** connector read/write paths, engine plumbing, capability manifests,
  mapping, resolution, tests, packaging config.
- **Haiku (4 tasks):** doc stubs, the Dockerfile, and the generated capability matrix.

## Suggested staffing

Wave 0 and the T1.1-T1.3 contract chain are single-threaded by nature — do not try to parallelize
them. Once the contract is frozen, run eight to ten agents concurrently: the SDK remainder, the
engine, the Qlik writer (critical path), the Databricks reader, and the WP7 identity/diff pair, each
in its own worktree confined to its `owns_paths`. Converge on WP8, then WP9. Finish by documenting
the delivery and retiring RM-01. Start RM-05 only after v0.1 is tagged.

# Citations

* [Decision: v1 scope — upstream-only, no access control](decision.md) — the scope this plan implements.
* [Decision: MVP is Databricks-to-Qlik, and how the two models map](decision-databricks-to-qlik-mvp.md) — the locked mapping decisions D1-D8.
* [Neutral Metadata Model Specification (v1)](/Research/RS-03-neutral-metadata-model/outputs/neutral-metadata-model-spec.md) — entities, envelopes, identity map.
* [Standalone Python Sync Service — Architecture & Tech Stack](/Research/RS-07-architecture-techstack-references/outputs/architecture-and-techstack.md) — engine, state store, and stack choices.
* [Connector Plugin SDK — Design Specification (v1)](/Research/RS-08-connector-plugin-sdk/outputs/connector-sdk-spec.md) — SDK contract, discovery, conformance kit.
* [Databricks Unity Catalog & Data Products — API Reference](/Research/RS-01-databricks-catalog-api/outputs/databricks-catalog-api-reference.md) — UC objects, tags via INFORMATION_SCHEMA, Statement Execution API.
* [Qlik Cloud Catalog & Data Products — API Reference](/Research/RS-02-qlik-catalog-api/outputs/qlik-catalog-api-reference.md) — data-product and items payloads, identity keys.
* [Qlik Two-Way Sync Readiness — Gaps Closed](/Research/RS-02-qlik-catalog-api/notes/qlik-two-way-sync-readiness.md) — Qlik write payloads, ETag concurrency, tenant-test items.
* [Reference Implementations — Data-Product Sync & Catalog Metadata Automation](/Research/RS-07-architecture-techstack-references/outputs/reference-projects.md) — patterns to borrow.
