---
type: "Decision"
title: "Decision: configuration lives in the state store, and selection is an ordered rule set"
description: "Moves endpoints, sync pairs and sync scope out of environment variables into the state store so a console can edit them, keeps credentials outside it as named references, and replaces the MVP's flat glob selector with an ordered include/exclude rule set evaluated by a single shared evaluator."
tags: ["decision", "RM-06", "console", "selection", "configuration"]
timestamp: "2026-08-20T12:40:00Z"
status: "accepted"
---

# Decision: configuration lives in the state store, and selection is an ordered rule set

## Context

RM-01 configures the engine the way a headless service is usually configured: environment variables
read once at process start, bound through `pydantic-settings`, with the set of objects a pair syncs
expressed as a list of `catalog.schema` glob patterns (RM-01 decision D1). That is a coherent design
for a service an engineer deploys, and an incoherent one for a service an operator *runs*.

Two things break. First, a browser cannot edit an environment variable, so a console needs a writable
source of truth that does not exist yet. Second, a flat list of globs cannot express the scope
question people actually ask — "everything in the analytics catalog **except** the staging schemas,
but keep `analytics.prod_staging`" — and offers no way to see which rule caused an object to be in or
out.

There is also a naming trap. Connectors are Python packages registered through the
`qlabs_catalog_sync.connectors` entry-point group, so "install an endpoint" could plausibly mean
installing a package into the running interpreter. That is remote code execution by design and is not
what an operator needs.

These decisions are numbered **C1-C8** to keep them distinct from RM-01's D1-D8.

## Decision

**C1 — Configuration lives in the state store; the environment becomes bootstrap.** Endpoints, sync
pairs and selection rules become tables in the existing SQLite/PostgreSQL state database alongside
`identity_map`, `watermarks` and `field_envelopes`. On first start the engine seeds them from any
environment-declared pairs; from then on the database is authoritative. RM-01's T2.3 is unchanged and
keeps owning secret backends and environment loading.

**C2 — Credentials are never stored by the console.** An endpoint holds a *named secret reference* —
`env:QLIK_ACME`, later `vault:kv/qlabs/qlik-acme` — resolved at `setup()` through a pluggable
`SecretBackend`. The environment backend ships in v0.1 and follows the SDK's existing
`ConnectorConfig.for_endpoint` prefix convention. The console reports whether a reference resolves and
whether `healthcheck()` passes; it never displays, accepts or persists a secret value.

**C3 — Selection is an ordered include/exclude rule set, and it supersedes D1.** Each pair holds an
ordered list of rules; evaluation runs top to bottom and the **last** matching rule decides, with
per-object overrides pinned by stable identifier beating every rule. D1's flat glob list is the
degenerate case of one include rule per pattern, so nothing in the locked Databricks-to-Qlik mapping
changes — the selector is widened, not replaced.

**C4 — One evaluator, shared by the preview and the sync.** The evaluator is a pure function over
`(rules, overrides, candidates)` returning, per object, whether it is included **and which rule
decided**. The sync loop and the console's preview call the same implementation. A preview that can
disagree with the run it predicts is worse than no preview, so there is exactly one code path.

**C5 — Selection has two scopes; field-level selection is out of v0.1.** Object scope decides which
`catalog.schema` become data products, dataset scope decides which tables and views inside a selected
schema become that product's members. Entity types stay a field on the pair, as RM-01 already
specifies. Choosing individual fields per pair is deliberately excluded: the engine already plans
fields strictly from the capability manifest, and a second, human-authored field filter on top is a
new class of surprise for little gain.

**C6 — "Install an endpoint" means registering an instance of a connector that is already present.**
The console lists what entry-point discovery found in the running image, and installing an endpoint
means naming one, pointing it at a tenant, binding a secret reference, running a healthcheck, reading
its capability manifest and enabling it. No package is fetched, installed or executed. Installing
connector *packages* from the browser stays out of scope.

**C7 — Single administrator, credential supplied by the environment, failing closed.** One operator
identity, a credential provided through the environment or a secret manager, a hashed comparison, an
`HttpOnly` `SameSite` session cookie, and a CSRF token on every mutating request. If no credential is
configured the console does not serve. OIDC, multiple users and a role model are deferred.

**C8 — The console ships inside the engine container, and v0.1 waits for it.** The SPA is built to
static assets and served by the same process that exposes the REST API, `/healthz` and `/metrics` —
one artifact, one origin, one version, no CORS and no possibility of the console drifting from the
engine it configures. RM-01's release task depends on this item's final task.

## Consequences

- The state store gains a configuration schema, an append-only configuration change log, and run
  history. Run history did not exist before: RM-01 persists envelopes, watermarks and orphans only.
- The engine gains an HTTP API. RM-01's T2.7 planned `/healthz` and `/metrics`; those endpoints stay
  exactly as specified and the API mounts alongside them.
- Configuration changes take effect without a restart. Every write bumps a generation counter, the
  scheduler reconciles its job set against the database on a short interval, and a cycle already in
  flight keeps the configuration it started with. `max_instances=1` per pair is unchanged.
- Two RM-06 tasks edit files RM-01 owns — the sync loop calls the evaluator, and the run recorder is
  invoked from the loop. RM-06 starts only after those RM-01 tasks are done, so the two boards never
  contend for a file.
- The repository gains a Node toolchain and a second CI gate. The Python gate is unaffected.
- Configuration export and import is **not** built in v0.1; recovery is a database backup, and the
  change log answers "when did this schema start syncing".

# Citations

* [Standalone Python Sync Service — Architecture & Tech Stack](/Research/RS-07-architecture-techstack-references/outputs/architecture-and-techstack.md) — the single-process service, state store and scheduler this decision extends.
* [Connector Plugin SDK Specification](/Research/RS-08-connector-plugin-sdk/outputs/connector-sdk-spec.md) — the entry-point discovery and capability manifest that make C6 a registration problem rather than an installation problem.
* [Neutral Metadata Model Specification (v1)](/Research/RS-03-neutral-metadata-model/outputs/neutral-metadata-model-spec.md) — the entities selection scopes are expressed over.
* [Databricks Unity Catalog Metadata API Reference](/Research/RS-01-databricks-catalog-api/outputs/databricks-catalog-api-reference.md) — the catalog/schema/table hierarchy the source tree browses and the tag surface the tag matcher depends on.
* [Decision: MVP is Databricks-to-Qlik, and how the two models map](/Roadmap/RM-01-one-way-sync-mvp/decision-databricks-to-qlik-mvp.md) — D1, whose glob selector C3 widens, and D6, which gates tag matching on a configured SQL warehouse.
