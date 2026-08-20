---
type: "Research Output"
title: "Console and selection implementation plan — Work Packages WP10-WP14"
description: "Executable build plan for the catalog sync console: the database-backed configuration store, the selection rule engine, the REST API and generated client, the SPA built on the @elabs-ai component packages, and the packaging that ships all of it inside the engine container."
tags: ["roadmap", "RM-06", "implementation-plan", "work-packages", "console", "selection"]
timestamp: "2026-08-20T16:30:20Z"
status: "draft"
---

# Console and selection implementation plan — Work Packages WP10-WP14

This is the executable plan for RM-06. It follows the same shape as the
[RM-01 plan](/Roadmap/completed/RM-01-one-way-sync-mvp/implementation-plan.md): work packages, tasks a coding
agent can pick up, dependencies, an owned file set, a definition of done, a runnable verify command,
and a recommended model. The narrative lives here; the board at
[tools/agent-plan/tasks-rm-06.json](/tools/agent-plan/tasks-rm-06.json) is the source of truth for
status and readiness.

## What this item is

RM-01 builds a sync engine that an engineer configures with environment variables and runs headless.
RM-06 builds the half an operator touches: a browser console that registers endpoints, defines sync
pairs, decides exactly which source objects are in scope, shows the planned writes before any are
applied, and reports what each run did.

It is part of the MVP. **v0.1 is not tagged until this board is finished** — RM-01's T9.4 depends on
this item's last task.

The decisions this plan implements, C1-C8, are in
[the console decision](decision-console-config-and-selection.md). The two that shape everything else:
configuration moves into the state store while credentials stay outside it as named references (C1,
C2), and selection becomes an ordered include/exclude rule set evaluated by a single evaluator shared
between the preview and the real sync (C3, C4).

## How this board relates to RM-01's

Both boards are loaded together — `ready_queue.py` globs `tasks*.json` and resolves dependencies
across every board it finds, exactly as it already does for RM-05. Scope a queue to this item with
`--roadmap RM-06`.

Dependencies are per-task, not per-item. The configuration-store tasks depend only on the state store
(T2.2) and config loading (T2.3), both already done, so WP10 can start immediately. The selection and
API work depends on the sync loop (T2.4), the scheduler (T2.6), observability (T2.7) and the dry-run
planner (T2.8), so in practice most of this item runs after RM-01's WP2 lands.

**Three tasks edit files RM-01 owns**, and each is listed as such on the board:

| Task | File | RM-01 owner | Why |
| --- | --- | --- | --- |
| T11.3 | `sync/loop.py` | T2.4 | the loop selects through the evaluator instead of a glob list |
| T12.9 | `scheduler.py` | T2.6 | the scheduler reconciles its job set against the configuration generation |
| T14.1 | `Dockerfile` | T9.1 | the image gains a Node build stage for the console |

Each of those depends on the RM-01 task that owns the file, so the two boards never contend for it.
No RM-01 task definition is rewritten by this item.

## Recommended-model legend

Same legend as RM-01. **Opus** for the evaluator, the loop integration, the scheduler reconcile, the
auth surface and the selection screen — correctness or security mistakes there are expensive.
**Sonnet** for everything else. No task here is mechanical enough for Haiku.

## Repository shape after this item

```
packages/qlabs-catalog-sync/src/qlabs_catalog_sync/
    configstore/     # endpoints, pairs, rules, secret refs, audit, bootstrap   (WP10)
    selection/       # rule model, evaluator, source-tree provider              (WP11)
    runs/            # run history model + recorder                            (WP11)
    api/             # FastAPI app, auth, routes, OpenAPI export               (WP12)
console/             # the SPA: Vite + React + @elabs-ai components            (WP13)
```

The six Python packages and the connector dependency rules are untouched. `console/` is a top-level
sibling of `packages/`, outside the uv workspace, with its own toolchain.

---

## WP10 — Configuration store

Goal: a writable, audited configuration schema in the state database, with credentials referenced
rather than stored. Gate: configuration survives a restart, every write is recorded in the change
log, and no secret value is ever persisted.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T10.1 | Configuration schema and Alembic migration: `endpoints`, `sync_pairs`, `selection_rules`, `selection_overrides`, `config_generation`, `config_changes` | T2.2 | no | Sonnet |
| T10.2 | Secret references (C2): `SecretRef` parsing, the `SecretBackend` protocol, the environment backend honoring the SDK's `for_endpoint` prefix convention, and resolve-status reporting that never returns a value | T1.7, T2.3, T10.1 | yes | Sonnet |
| T10.3 | Configuration service: CRUD over endpoints, pairs, rules and overrides; every write validates endpoint settings against the connector's own `ConfigModel`, appends to the change log and bumps the generation counter **in one transaction** | T10.1, T10.2 | no | Sonnet |
| T10.4 | Bootstrap import (C1): seed the store from environment-declared pairs on first start; the database is authoritative from then on and re-running the import is a no-op | T10.3, T2.3 | yes | Sonnet |

## WP11 — Selection engine and run history

Goal: one evaluator that decides scope, used by both the preview and the sync, plus the run history
the console reports from. Gate: a rule set produces the same decisions through the API preview and
through a real cycle, and every run is recorded with its unresolved references.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T11.1 | Rule model and evaluator (C3, C4): ordered rules, last match wins, overrides beat rules, glob/tag/owner matchers, object and dataset scope; returns per object both the decision **and the deciding rule** | T10.1 | no | **Opus** |
| T11.2 | Source-tree provider: lazy, paged enumeration of source candidates through the connector contract, each node decorated with its decision and deciding rule; tag matchers are reported unavailable when the source's manifest does not offer tags (RM-01 D6) | T11.1, T2.1 | no | Sonnet |
| T11.3 | Sync-loop integration: the loop resolves scope through the evaluator instead of a glob list, honoring the pair's entity types; a pair with no matching objects is a clean no-op | T11.1, T2.4 | no | **Opus** |
| T11.4 | Run history: `runs` and `run_items` tables, migration, and a recorder the loop drives — start, finish, per-entity counts, unresolved dataset members and owners (RM-01 D2, D3), orphans (D4) | T11.3, T2.9 | no | Sonnet |

## WP12 — REST API

Goal: a typed HTTP surface over the engine, authenticated, with a generated TypeScript client that
cannot silently drift. Gate: the API runs in the same process as `/healthz` and `/metrics`, every
route is exercised against a `FakeConnector`-backed engine, and regenerating the client produces no
diff.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T12.1 | FastAPI application mounted alongside `/healthz` and `/metrics`, shared error model, and the static-asset mount with SPA fallback that WP13 builds into | T2.7, T10.3 | no | Sonnet |
| T12.2 | Administrator authentication (C7): credential from the environment, hashed comparison, `HttpOnly` `SameSite` session cookie, CSRF token on mutating requests, and refusal to serve when no credential is configured | T12.1 | no | **Opus** |
| T12.3 | Connector and endpoint routes: list discovered connectors with their capability manifests, endpoint CRUD, healthcheck, and secret-reference resolve status | T12.2, T2.1, T10.3 | no | Sonnet |
| T12.4 | Sync-pair, rule and override routes, including rule reordering | T12.3, T11.1 | no | Sonnet |
| T12.5 | Source-tree and preview routes: browse candidates lazily, and evaluate a rule set to counts plus a sample without writing anything | T12.4, T11.2 | yes | Sonnet |
| T12.6 | Dry-run and run-control routes: return T2.8's planned write set with unresolved references, and expose run-now, pause and resume | T12.4, T2.8, T2.6 | yes | Sonnet |
| T12.7 | History routes: run list and detail, run issues (unresolved and orphaned), and the configuration change log | T12.4, T11.4 | yes | Sonnet |
| T12.8 | OpenAPI export, generated TypeScript client committed under `console/src/api/generated/`, and a CI check that regeneration is a no-op | T12.3, T12.4, T12.5, T12.6, T12.7 | no | Sonnet |
| T12.9 | Scheduler reconcile (C1): the scheduler reconciles its job set against the configuration generation counter on a short interval; a cycle in flight keeps the configuration it started with, and `max_instances=1` still holds | T12.4, T2.6, T10.3 | no | **Opus** |

## WP13 — Console SPA

Goal: the operator interface, built on the `@elabs-ai` component packages. Gate: typecheck, lint,
unit tests and the accessibility checks pass in CI, and every screen works against the real API.

The packages are public on npmjs.org at `4.0.0` — `@elabs-ai/components-{ui,data,tokens,icons}`, plus
`components-charts` if the run screens want them. No registry configuration and no token is required.
The scaffold is `brand-ui scaffold app-spec.md --write console` with `standalone: true`, which emits a
runnable Vite application with the token stylesheet, theme provider and CSS wiring already correct.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T13.1 | Write `console/app-spec.md` — screens, archetype, theme and dials — and scaffold the application from it with the brand-ui CLI in standalone mode | T12.8 | no | Sonnet |
| T13.2 | Application shell, sign-in screen, session handling against the auth routes, and the theme switcher | T13.1 | no | Sonnet |
| T13.3 | Endpoints screens: list, register from a discovered connector, edit, healthcheck, secret-reference status, capability manifest viewer | T13.2 | no | Sonnet |
| T13.4 | Sync-pair screens: list, create, edit cadence, target space, entity types, manual-edit policy and activation opt-in | T13.3 | no | Sonnet |
| T13.5 | Selection screen: lazy source tree with each node marked included or excluded **and by which rule**, the ordered rule editor, per-object overrides, and a live preview of the resulting counts | T13.4 | no | **Opus** |
| T13.6 | Dry-run screen: the planned write set grouped by product, with unresolved dataset members and unresolvable owners called out | T13.5 | yes | Sonnet |
| T13.7 | Runs screens: history, run detail with counts and issues, and the run-now, pause and resume controls | T13.4 | yes | Sonnet |
| T13.8 | JavaScript gate and CI job: `tsc --noEmit`, lint, Vitest with Testing Library, and the brand-ui accessibility checks | T13.6, T13.7 | no | Sonnet |

## WP14 — Ship it

Goal: one container that serves the engine and the console, documented, and proven on the pilot.
Gate: the Databricks-to-Qlik pilot is configured and run entirely through the console.

| Task | Description | Depends on | Parallel | Model |
| --- | --- | --- | --- | --- |
| T14.1 | Extend the container image (T9.1) with a Node build stage that builds the console and copies its assets into the Python runtime; the process serves the API, the console, `/healthz` and `/metrics` on one port | T13.8, T9.1 | no | Sonnet |
| T14.2 | Operator documentation: console setup, the administrator credential, secret references, the selection rule model, and what the console deliberately does not do | T14.1 | yes | Sonnet |
| T14.3 | Pilot through the console: register both endpoints, define the pair, narrow scope with rules, review the dry-run, apply, and confirm a re-run is a no-op | T14.1, T8.1 | no | **Opus** |

RM-01's T9.4 — the runbook and the v0.1 tag — depends on T14.3.

## Sequencing

| Wave | Runs | Enables |
| --- | --- | --- |
| A | WP10 — configuration store (ready now; T2.2 and T2.3 are done) | the API |
| B | WP11 — evaluator, source tree, loop integration, run history (after T2.4, T2.9) | preview and history |
| C | WP12 — API, auth, codegen, reconcile (after T2.6, T2.7, T2.8) | the SPA |
| D | WP13 — the console, screen by screen | the release |
| E | WP14 — image, docs, pilot | **RM-06 complete, v0.1 tagged** |

**Critical path:** T10.1 → T10.3 → T11.1 → T11.3 → T12.4 → T12.8 → T13.1 → T13.5 → T13.8 → T14.1 →
T14.3 → T9.4.

## Definition of done for the item

Every task on the board is `done` and its verify command passes, the Python gate
(`ruff`, `mypy --strict`, `pytest`) and the console gate both pass, the root `README.md` reflects
what the console does, the delivery is recorded in `Docu/`, and the item is retired with
`complete-roadmap`.

## What this item deliberately does not build

Per-field selection; installing connector packages from the browser; OIDC, multiple users or roles;
configuration export and import; browser-driven end-to-end tests; and any change to the upstream-only,
Qlik-is-the-sole-writer guardrails, which are unaffected by everything here.

# Citations

* [Standalone Python Sync Service — Architecture & Tech Stack](/Research/RS-07-architecture-techstack-references/outputs/architecture-and-techstack.md) — the single-process service, state store, scheduler and observability surface this plan extends.
* [Connector Plugin SDK Specification](/Research/RS-08-connector-plugin-sdk/outputs/connector-sdk-spec.md) — entry-point discovery and the capability manifest the endpoint screens read.
* [Neutral Metadata Model Specification (v1)](/Research/RS-03-neutral-metadata-model/outputs/neutral-metadata-model-spec.md) — the entities selection scopes are expressed over.
* [Databricks Unity Catalog Metadata API Reference](/Research/RS-01-databricks-catalog-api/outputs/databricks-catalog-api-reference.md) — the catalog/schema/table hierarchy the source tree browses.
* [v1 Implementation Plan — Work Packages, Tasks & Model Recommendations](/Roadmap/completed/RM-01-one-way-sync-mvp/implementation-plan.md) — the engine tasks this board depends on and the three files it extends.
* [Decision: configuration lives in the state store, and selection is an ordered rule set](decision-console-config-and-selection.md) — C1-C8, which this plan implements.
