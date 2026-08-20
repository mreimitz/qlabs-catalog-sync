# QLabs Catalog Sync

Keeps **data-product metadata** consistent across data catalogs — Databricks, Qlik,
Snowflake, Collibra — so the same description, owner list and tags do not have to be
maintained by hand in each one.

> **Status: pre-implementation.** The research, the contracts and the build plan are
> settled; the sync itself is not built yet. Nothing described under *What it will do*
> runs today. See [Current state](#current-state) for exactly what exists.

---

## What it will do

### The problem

One data product usually lives in several catalogs at once — the tables in Databricks
Unity Catalog, the governed product and glossary in Qlik Cloud, listings in Snowflake
Horizon, the business definitions in Collibra. Each catalog holds its own copy of the
description, the owners, the tags and the links between them, and each copy drifts as
soon as someone edits one of them. Keeping four catalogs in step by hand does not happen.

QLabs Catalog Sync reads that metadata from a source catalog, translates it through a
catalog-agnostic model, and writes only what actually differs into the target catalog —
on a schedule, repeatably, and without touching the data itself.

One finding shapes the whole design: Qlik Cloud publishes no webhooks or audit events for
items, datasets, data products or glossaries. There is nothing to subscribe to, so the
engine polls sources on a configured cadence and detects change by comparing checksums
rather than by listening for events.

### How the sync will work

Every sync pair runs the same cycle:

1. **Poll** the source for what changed since the last run (`list_changed`).
2. **Read** each changed object into neutral **field envelopes**.
3. **Resolve identity** — map the source object to its counterpart in the target through
   the IdentityMap.
4. **Diff** by checksum against the last-known envelope; unchanged fields produce no work.
5. **Apply the write policy** for values a human edited in the target.
6. **Write** the minimal native mutation the target supports, guarded by its own
   concurrency mechanism (ETag, revision, or none) as the connector declares it.
7. **Persist** the new envelopes and advance the watermark **in a single transaction**, so
   a crash mid-cycle commits nothing and the next run resumes from the last good point.
8. **Skip** anything whose checksum is unchanged.

The consequence worth stating plainly: re-running the sync over an unchanged source is a
no-op — no API writes, no state churn.

Engine state is small and inspectable — an `identity_map`, per-pair `watermarks`, and the
last-known `field_envelopes` — on SQLite (WAL mode) by default, with the same schema on
PostgreSQL when more than one worker is needed.

### The neutral metadata model

Catalogs never map to each other directly. Everything passes through a neutral model, so
adding a catalog means writing one connector rather than N–1 translations.

Entities: **DataProduct**, **Dataset**, **GlossaryTerm**, **Category** — plus the
**Party**, **Tag** and **IdentityRef** value types reused across them.

Every synced field carries its own provenance:

```json
{
  "value": "<field value>",
  "sourceEndpoint": "qlik|databricks|snowflake|collibra",
  "sourceRevision": "<etag | revision counter | updatedAt>",
  "lastModifiedAt": "<RFC3339 from source>",
  "lastSyncedAt": "<RFC3339 engine time>",
  "checksum": "<hash of normalized value>"
}
```

That envelope is what makes field-level decisions possible: which catalog last set this
value, when, and whether it really changed or was merely rewritten identically.

Explicitly outside the model: data movement, lineage, data-quality and profiling metrics,
access policies and grants, and the query/metric semantic layers.

### Connectors and the capability manifest

A catalog is attached by installing a connector package. Registration is the entry point
in its `pyproject.toml` — there is no registry to edit and no engine code to change:

```toml
[project.entry-points."qlabs_catalog_sync.connectors"]
example = "qlabs_connector_example:ExampleConnector"
```

Each connector implements an async contract — `capabilities()` plus `setup`,
`healthcheck`, `list_changed`, `read`, `create`, `update` and `delete`. `list_changed`
returns both the changes and the next watermark, so envelopes and watermark can be
committed together.

`capabilities()` returns a **capability manifest** that describes, per entity and per
field, what this catalog can actually do:

- **field mode** — `rw`, `ro` or `na`
- **`partial_update`** — whether a field can be patched, or must be replaced wholesale
- **`allowed_update_paths`** — the exact set of paths the target's API accepts
- **`concurrency`** — `etag`, `revision` or `none`

The engine plans strictly from the manifest. It never writes a field a connector declares
`ro` or `na`, and it only sends `if-match` where the connector claims ETag support. A
dishonest manifest is therefore a correctness bug, not a documentation bug — which is why
the SDK ships a **conformance kit**. A connector is "certified" once it passes the
contract, round-trip, idempotency, HTTP-behavior and capability-honesty suites.

### What you will configure, and what you will run

Configuration is a set of **sync pairs**. A pair names its source endpoint, which objects
to select (for Databricks, glob patterns over `catalog.schema`), the target Qlik space,
which entity types to sync, the poll cadence, the policy for values edited by hand in the
target, and whether product activation is enabled. Credentials come from environment
variables or a secret manager; connectors never read the environment themselves, and
secrets are redacted from logs and never written to state.

The service runs as a single long-lived process — one container — exposing `/healthz` and
`/metrics` (Prometheus) and emitting structured JSON logs. Per-pair jobs run on their own
cadence with jitter, and a pair never overlaps itself.

There will also be a CLI, whose most important mode is **dry-run**: it computes the full
planned write set, emits it as a machine-readable plan file plus a human-readable log, and
applies zero mutations. (Command names are not fixed yet — see
[Current state](#current-state).)

### Scope of v1 — upstream only

v1 is deliberately the smallest safe version of the idea:

- **Upstream only.** Metadata flows from source catalogs into Qlik. There is no reverse
  flow.
- **Qlik is the only write target.** Exactly one write connector.
- **Source connectors are read-only.** No create, update or delete paths.
- **No two-way sync.** The only conflict handling is the manual-edit policy: source-wins
  overwrite by default, configurable to preserve local edits.
- **No access-control sync.** Access and authorization are entirely out of v1.
- **Owners are best-effort metadata**, correlated on email. This is not an identity system
  and must not become one.

The **MVP (Track A)** is one source and one target: Databricks Unity Catalog → Qlik Cloud.
A UC schema becomes one Qlik data product; its tables and views become that product's
datasets. **Track B** — Collibra, Snowflake and the Qlik glossary write path — is a separate
roadmap item (RM-05) on its own board; it starts only after the MVP ships, and its tasks sit as
`blocked` until then.

Two safety properties hold for v1, by design:

- **The sync never creates Qlik datasets.** A product's dataset members are resolved
  against datasets that already exist in the target space; anything unresolved is left out
  of the payload and listed in the run report. The alternative — fabricating resources in
  the target — is worse than an incomplete product.
- **The sync never deletes in Qlik.** A source object that disappears is reported as an
  orphan, not removed. A misconfiguration is therefore recoverable by editing config
  rather than by restoring data.

### Beyond v1

Planned, not scheduled:

- **Two-way sync with conflict resolution** — reconciling edits in both directions without
  data loss. This is the project's core long-term promise.
- **A pluggable endpoint framework** — attaching a new catalog with no change to the
  engine at all.
- **Access observe-and-report** — reading authorization state from every catalog into a
  neutral, read-only access graph to report cross-catalog access and drift. Read-only by
  intent; syncing access is explicitly not the goal.

---

## Current state

**Nothing syncs yet.** The repository holds a complete design, a task board, and a
six-package skeleton in which every module is a documented stub. Roughly 330 non-blank
lines of Python exist across 49 files, and the only definitions in them are four
placeholder connector classes and one CLI entry function.

### Status at a glance

As of 2026-08-20 — 67 tasks on the board, 2 done.

| Work package | Scope | Done | Status |
|---|---|---|---|
| WP0 | Workspace, tooling, dependency pinning, CI | 2 / 6 | In progress |
| WP1 | Connector SDK — model, contract, manifest, conformance kit | 0 / 10 | Not started |
| WP2 | Engine — discovery, state store, sync loop, scheduler, CLI | 0 / 9 | Not started |
| WP3 | Qlik write connector (sole writer) | 0 / 9 | Not started |
| WP4 | Databricks read connector | 0 / 7 | Not started |
| WP5 | Collibra read connector | 0 / 6 | Blocked (Track B, RM-05) |
| WP6 | Snowflake read connector | 0 / 6 | Blocked (Track B, RM-05) |
| WP7 | Identity map, field diff, owner correlation | 0 / 4 | Not started |
| WP8 | Integration, end-to-end pilot, release readiness | 0 / 6 | Not started |
| WP9 | Packaging, deployment, runbook, v0.1 tag | 0 / 4 | Not started |

Regenerate this picture at any time:

```bash
python3 planning/tools/agent-plan/ready_queue.py --all --roadmap RM-01
```

### What works today

- The uv workspace resolves; `uv sync --all-packages` installs all six packages editable.
- All four connector entry points resolve — `collibra`, `databricks`, `qlik`, `snowflake`
  — so the discovery mechanism is real even though nothing consumes it yet.
- The SDK exports its two contract constants: `CONTRACT_VERSION = "0.1.0"` and the
  entry-point group name `qlabs_catalog_sync.connectors`.
- `uv run ruff check packages` passes.

That is the complete list.

### What does not exist yet

- **No SDK.** No `Connector` base class, no neutral model types, no capability-manifest
  types, no HTTP or auth helpers, no conformance kit.
- **No engine.** No connector discovery, no state store or migrations, no sync loop, no
  scheduler, no diff engine, no identity map.
- **No connector implementations.** The four `Connector` classes are placeholders with a
  `name` attribute and no methods.
- **No configuration format.** No config schema, no example config, no `.env` template.
- **No usable CLI.** The `qlabs-catalog-sync` console script installs and then raises
  `NotImplementedError`.
- **No real tests.** Six import smoke tests, no conformance suite, no cassettes.

### The build gate is currently red

`ruff` passes, but `mypy` and `pytest` both fail on duplicate `test_smoke` module
basenames across packages. Repairing the gate is the first task on the board (`T0.5`); no
implementation work starts before it is green.

### Known-unverified behavior

The build runs without access to a live Databricks workspace or Qlik tenant, so connectors
are written against mocked HTTP and hand-authored cassettes derived from the API research.
These points are documented assumptions until a real tenant confirms them:

- whether the Qlik data-products `PATCH` endpoint honors `if-match`/ETag (undocumented —
  the writer sends it and tolerates its absence)
- the glossary-term patch path enum, the change-status request body, and the link payload
  shape
- whether `qri`/`secureQri` survive a space move and differ across tenants
- the exact custom-role permissions a Qlik sync service account needs
- Databricks rate-limit behavior at the chosen poll cadence

They are tracked as an explicit pre-production checklist, not left implicit.

---

## Repository layout

A [uv](https://docs.astral.sh/uv/) workspace; every package uses a `src/` layout.

```
packages/
  qlabs-catalog-sync-sdk/       # the public contract, neutral model, helpers, conformance kit (WP1)
  qlabs-catalog-sync/           # the engine: discovery, sync loop, state store, scheduler (WP2, WP7)
  qlabs-connector-qlik/         # sole WRITE connector                                       (WP3)
  qlabs-connector-databricks/   # read-only source connector                                 (WP4)
  qlabs-connector-collibra/     # read-only source connector                (WP5, Track B, RM-05)
  qlabs-connector-snowflake/    # read-only source connector                (WP6, Track B, RM-05)
planning/                       # design, research & plan — a separately-governed OKF bundle
```

**Dependency rule:** connectors depend only on the SDK (plus their vendor libraries); the
engine depends on the SDK and discovers connectors at runtime via the
`qlabs_catalog_sync.connectors` entry-point group. Nothing depends on a connector
directly.

## Quickstart (for developers)

There is nothing to install as a user yet. To work on the code:

```bash
uv sync --all-packages         # install every workspace member + dev group
uv run ruff check packages     # lint
uv run mypy                    # strict type-check
uv run pytest -q               # tests
```

Note that `mypy` and `pytest` currently fail — see [above](#the-build-gate-is-currently-red).

## Working on it

The task board is machine-readable. To see everything ready to pick up right now (all
dependencies `done`):

```bash
python3 planning/tools/agent-plan/ready_queue.py --roadmap RM-01
```

Then read [`AGENTS.md`](AGENTS.md) for how to claim and land a task, and
[`CLAUDE.md`](CLAUDE.md) for the dependency rule and scope guardrails.

## Design, research and the plan

Everything that justifies the design above lives under [`planning/`](planning/) — a
separately-governed Open Knowledge Format (OKF) bundle with its own tooling and
conformance rules. **Do not hand-edit it**; change it only through its own commands.

Start here:

- [`planning/Roadmap/RM-01-one-way-sync-mvp/`](planning/Roadmap/RM-01-one-way-sync-mvp/) —
  the v1 scope decision, the Databricks-to-Qlik mapping decisions, the implementation plan
  and the agent guide.
- [`planning/Roadmap/roadmap.md`](planning/Roadmap/roadmap.md) — RM-01 through RM-05.
- [`planning/Docu/`](planning/Docu/) — what has actually been built, one subject per part
  of the system. Empty until the first roadmap item completes.

Roadmap items are retired, not deleted: when one finishes, its delivery is recorded in
`planning/Docu/` and the item moves to `planning/Roadmap/completed/`. That is the only
sanctioned way to finish work — see the implementation lifecycle in
[`CLAUDE.md`](CLAUDE.md).

The API and design research behind it:

| Topic | Document |
|---|---|
| Neutral metadata model | [`RS-03`](planning/Research/RS-03-neutral-metadata-model/outputs/neutral-metadata-model-spec.md) |
| Architecture and tech stack | [`RS-07`](planning/Research/RS-07-architecture-techstack-references/outputs/architecture-and-techstack.md) |
| Connector plugin SDK | [`RS-08`](planning/Research/RS-08-connector-plugin-sdk/outputs/connector-sdk-spec.md) |
| Databricks Unity Catalog API | [`RS-01`](planning/Research/RS-01-databricks-catalog-api/outputs/databricks-catalog-api-reference.md) |
| Qlik Cloud catalog API | [`RS-02`](planning/Research/RS-02-qlik-catalog-api/outputs/qlik-catalog-api-reference.md) |
| Snowflake Horizon API | [`RS-05`](planning/Research/RS-05-snowflake-catalog-api/outputs/snowflake-catalog-api-reference.md) |
| Collibra API | [`RS-06`](planning/Research/RS-06-collibra-catalog-api/outputs/collibra-catalog-api-reference.md) |
| Access-control sync options | [`RS-09`](planning/Research/RS-09-access-control-sync/outputs/access-control-sync-options.md) |
