---
type: "Research Output"
title: "Standalone Python Sync Service — Architecture & Tech Stack"
description: "A buildable architecture and recommended Python tech stack for the QLabs Catalog Sync standalone service, built around the RS-03 neutral model and poll-based change detection."
tags: ["research", "RS-07", "architecture", "tech-stack", "python"]
timestamp: "2026-08-06T10:30:00Z"
status: "draft"
---

# Standalone Python Sync Service — Architecture & Tech Stack

This document specifies a concrete, buildable architecture and a recommended Python tech stack for
QLabs Catalog Sync: a standalone service that two-way syncs data-product metadata across catalogs.
Databricks and Qlik ship first; Snowflake and Collibra follow. The design is anchored to the RS-03
neutral metadata model (DataProduct, Dataset, GlossaryTerm, Category, plus Party/Tag), the
IdentityMap, and per-field envelopes, and to the fact that change detection is poll-based because
Qlik Cloud exposes no webhooks or audit events for the relevant object types.

The bias throughout is toward the lightest components that satisfy the requirement, with an explicit
graduation path. A single async process with a SQLite state store and an in-process scheduler is the
starting shape; Postgres, worker fan-out, and an orchestrator are documented as later steps, not
day-one dependencies.

## 1. Architecture overview

The service is a single long-running Python process (one container) composed of a small set of
cooperating parts. The core is deliberately endpoint-agnostic: everything catalog-specific lives
behind the endpoint adapter contract, so adding Snowflake or Collibra is a new adapter plus config,
not a change to the engine.

Components:

- **Sync engine core** — orchestrates one sync cycle per direction/pair: pull changes, materialize
  envelopes, resolve identity, diff, invoke the conflict hook, emit minimal native writes, persist,
  advance watermarks. It knows nothing about HTTP or SQL specifics.
- **Endpoint adapters** — one per catalog (Databricks, Qlik, later Snowflake, Collibra), each
  implementing the same contract: `capabilities()`, `list_changed(watermark)`, `read()`, `create()`,
  `update(diff)`, `delete()`/lifecycle. Adapters own their SDK/REST client, auth, pagination, write
  translation (JSON Patch, SQL DDL, Import API), and rate-limit handling.
- **IdentityMap and state store** — persistence for the neutral<->native key mapping, per-endpoint
  watermarks, and per-field last-known envelopes. SQLite first, Postgres later, behind SQLAlchemy.
- **Scheduler** — triggers sync cycles on a per-pair cadence. In-process (APScheduler AsyncIO
  scheduler or a plain asyncio loop) for the standalone service.
- **Config and secrets** — declarative tenant/endpoint config plus per-tenant credential resolution
  from environment or a secret manager, validated at startup with pydantic-settings.
- **Observability** — structured logs (structlog), Prometheus metrics endpoint, optional
  OpenTelemetry traces/log correlation.

```
                         +--------------------------------------------------+
                         |                 Sync Service (1 process)         |
                         |                                                  |
  cron/cadence  -------> |  +------------+        +----------------------+  |
                         |  | Scheduler  | -----> |   Sync Engine Core   |  |
                         |  |(APScheduler|        |  poll->read->resolve |  |
                         |  | / asyncio) |        |  ->diff->conflict->   |  |
                         |  +------------+        |  write->persist       |  |
                         |                        +----------+-----------+  |
                         |                                   |              |
                         |        +--------------------------+-----------+  |
                         |        |            |             |           |  |
                         |   +----v----+  +----v----+   +----v----+  +---v-+ |
                         |   |Databricks|  |  Qlik   |   |Snowflake|  |Coll.| |  <- Endpoint Adapters
                         |   | adapter  |  | adapter |   | adapter |  |adptr| |     (contract)
                         |   +----+-----+  +----+----+   +----+----+  +--+--+ |
                         |        |             |             |          |    |
                         |   +----v-------------v-------------v----------v--+ |
                         |   |  State store: IdentityMap / watermarks /     | |
                         |   |  field envelopes (SQLAlchemy -> SQLite/PG)   | |
                         |   +----------------------------------------------+ |
                         |                                                  |
                         |   config/secrets (pydantic-settings)   obs:      |
                         |                                        structlog |
                         |                                        prometheus|
                         +--------------------------------------------------+
                              |                |             |          |
                         Databricks UC     Qlik Cloud    Snowflake   Collibra
                         REST + SQL        REST (poll)   SQL DDL     Core REST + Import
```

Data flow inside a cycle is strictly one-way per invocation. Two-way sync is two scheduled
directions over the same identity map, not a single bidirectional pass; this keeps conflict handling
and idempotency tractable and restart-safe.

## 2. The sync loop

One cycle syncs one ordered pair (source endpoint -> target endpoint) for one entity type. The engine
runs many such cycles across the scheduled matrix. The canonical steps:

1. **Poll / listChanged** — call the source adapter's `list_changed(watermark)`. For Qlik this is a
   scheduled list/scan filtered by the stored watermark (modified-since or a page cursor), because no
   webhooks/audit events exist. For Databricks it is a UC listing plus modified-time comparison. The
   adapter returns candidate native records plus a *proposed next watermark*.
2. **Read to envelopes** — for each candidate, `read()` the full native record and normalize it into
   the RS-03 neutral model. Every synced field becomes an envelope
   `{value, sourceEndpoint, sourceRevision, lastModifiedAt, lastSyncedAt, checksum}`. The checksum is
   computed over the canonicalized value so equality is cheap and stable.
3. **Identity resolve** — look up the `neutralId` for the source native key in the IdentityMap. If
   absent, this is a create candidate; allocate a `neutralId` and record the source-side mapping.
   Resolution keys are endpoint-specific: Databricks `full_name`+`id`, Qlik `secureQri`/term UUID,
   Snowflake FQN/listing global name, Collibra UUID.
4. **Diff** — compare the freshly read source envelopes against the last-known envelopes for the
   target side of this pair (stored in the state store). Field-level diff via checksum yields the set
   of changed fields. If nothing changed since `lastSyncedAt`, short-circuit (idempotent skip).
5. **Conflict hook (RS-04)** — before writing, hand any field where *both* sides changed since the
   last sync to the conflict resolver. The engine detects concurrency by comparing each side's
   `sourceRevision`/`lastModifiedAt` against the last-synced envelope; the RS-04 policy
   (source-wins, target-wins, newest-wins, or manual/quarantine) returns the winning value. This hook
   is a pluggable callable so RS-04 strategy can evolve without touching the loop.
6. **Minimal native write with concurrency guard** — translate the resolved diff into the smallest
   native mutation the target adapter supports and apply it with an optimistic-concurrency guard where
   available. Qlik glossary writes carry an ETag / `if-match` and revision counter, so the adapter
   sends the last-seen ETag and treats a `412 Precondition Failed` as a conflict-retry signal (re-read,
   re-diff, re-resolve). Endpoints without ETags rely on `sourceRevision` comparison plus a re-read
   guard.
7. **Persist envelopes + advance watermark** — on write success, update the target-side envelopes with
   the new value, `sourceRevision`, `lastSyncedAt`, and checksum, and commit the proposed watermark.
   State mutation and watermark advance happen in one transaction so a crash cannot advance the
   watermark past unpersisted work.
8. **Idempotent skip** — because equality is checksum-based and writes are guarded, replaying a cycle
   after a crash re-reads, finds no diff, and no-ops. There are no side effects from a repeated run.

**Full-replace vs partial-patch per adapter.** The engine always computes a *field-level* diff; each
adapter decides how to express it natively:

- **Qlik data-product update** — JSON Patch, `replace`-only, against a *closed path enum*, with arrays
  handled as *full replace*. The adapter maps changed neutral fields onto allowed paths; for any
  array-valued field (for example tags or member lists) it emits the entire array even if only one
  element changed, and it rejects/skips paths outside the enum.
- **Qlik glossary** — JSON Patch plus ETag/`if-match` and revision counter for optimistic concurrency.
- **Databricks** — UC REST `PATCH`/update for container-level metadata (catalog, schema, comments,
  tags), and SQL DDL via the Statement Execution API for table/column-level changes (`COMMENT ON`,
  `ALTER TABLE ... SET TAGS`). The adapter emits only the columns/objects that changed.
- **Snowflake (later)** — SQL-DDL-first: `COMMENT`, `ALTER ... SET TAG`, listing updates; minimal
  statements per changed object.
- **Collibra (later)** — Core REST v2 for targeted attribute/relation updates, Import API v2 for bulk
  or full-replace synchronization where per-field REST would be too chatty.

The contract exposes `supports_partial_update` and `array_semantics` capability flags so the engine
knows when it must widen a partial diff into a full-array replace before calling `update()`.

## 3. Endpoint adapter design

Every adapter implements one abstract interface. Neutral payloads and diffs are pydantic v2 models so
validation is uniform across catalogs. Capabilities are declared, not inferred, so the engine can adapt
its write strategy per endpoint.

```python
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import Enum
from pydantic import BaseModel

# --- RS-03 neutral model (excerpt) ---

class Envelope(BaseModel):
    value: object | None
    source_endpoint: str
    source_revision: str | None        # ETag, revision counter, or version tag
    last_modified_at: str | None        # ISO-8601 UTC from the source
    last_synced_at: str | None
    checksum: str                        # hash over canonicalized value

class NeutralRecord(BaseModel):
    neutral_id: str | None               # None until IdentityMap allocates
    entity_type: str                     # DataProduct | Dataset | GlossaryTerm | Category | ...
    native_key: dict[str, str]           # endpoint-specific identity keys
    fields: dict[str, Envelope]          # field name -> envelope
    etag: str | None = None              # last-seen concurrency token

class FieldChange(BaseModel):
    field: str
    new_value: object | None

class Diff(BaseModel):
    neutral_id: str
    entity_type: str
    changes: list[FieldChange]

class ArraySemantics(str, Enum):
    FULL_REPLACE = "full_replace"
    ELEMENT_PATCH = "element_patch"

class Capabilities(BaseModel):
    endpoint: str
    entity_types: list[str]
    supports_partial_update: bool
    array_semantics: ArraySemantics
    supports_optimistic_concurrency: bool   # ETag / if-match / revision
    supports_delete: bool
    change_detection: str                    # "poll" | "events"
    allowed_update_paths: list[str] | None   # closed path enum (Qlik)
    write_mechanism: str                     # "json_patch" | "rest+ddl" | "ddl" | "rest+import"

class Watermark(BaseModel):
    endpoint: str
    entity_type: str
    cursor: str | None                       # modified-since ts, page token, or opaque cursor


class EndpointAdapter(ABC):
    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    async def list_changed(
        self, watermark: Watermark
    ) -> AsyncIterator[tuple[dict[str, str], Watermark]]:
        """Yield (native_key, proposed_next_watermark) for candidates since the watermark."""

    @abstractmethod
    async def read(self, native_key: dict[str, str]) -> NeutralRecord: ...

    @abstractmethod
    async def create(self, record: NeutralRecord) -> dict[str, str]:
        """Create the object; return the assigned native_key."""

    @abstractmethod
    async def update(self, diff: Diff, etag: str | None) -> str | None:
        """Apply minimal native mutation; return new etag/revision if any."""

    @abstractmethod
    async def delete(self, native_key: dict[str, str]) -> None:
        """Delete or lifecycle-retire depending on capabilities."""
```

How the two first-class endpoints map onto this contract:

| Concern | Databricks adapter | Qlik adapter |
| --- | --- | --- |
| Client | Databricks SDK for Python (`WorkspaceClient`) + Statement Execution API for SQL DDL | httpx async client with OAuth2 M2M (optionally the `qlik-sdk` Platform SDK); REST-direct |
| `change_detection` | `poll` (UC listing + modified-time) | `poll` (no webhooks/audit for items/datasets/data-products/glossaries) |
| `write_mechanism` | `rest+ddl` (UC REST for containers, SQL DDL for table/column) | `json_patch` (replace-only, closed path enum) |
| `array_semantics` | element patch where UC allows; else full set | `full_replace` for arrays |
| Optimistic concurrency | `sourceRevision` + re-read guard | ETag / `if-match` + revision counter on glossary writes |
| Identity keys | `full_name` + `id` | `secureQri` / term UUID |

Snowflake (`ddl`, FQN/listing global name) and Collibra (`rest+import`, UUID) slot in the same way
when they are built.

## 4. State model

The state store is the durability boundary. It holds three things: the identity map, per-endpoint
watermarks, and the last-known field envelopes that make diffing and idempotency possible. Modeled with
SQLAlchemy 2.0 so the same code runs on SQLite (standalone) and Postgres (scaled) with Alembic-managed
migrations.

```sql
-- Neutral <-> native identity, one row per (neutral_id, endpoint, entity_type)
CREATE TABLE identity_map (
    neutral_id     TEXT NOT NULL,
    endpoint       TEXT NOT NULL,          -- databricks | qlik | snowflake | collibra
    entity_type    TEXT NOT NULL,          -- DataProduct | Dataset | GlossaryTerm | ...
    native_key     TEXT NOT NULL,          -- JSON: {full_name,id} / {secureQri} / {fqn} / {uuid}
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (neutral_id, endpoint, entity_type)
);
CREATE UNIQUE INDEX ux_identity_native
    ON identity_map (endpoint, entity_type, native_key);

-- Per-endpoint, per-entity poll position
CREATE TABLE watermarks (
    endpoint       TEXT NOT NULL,
    entity_type    TEXT NOT NULL,
    cursor         TEXT,                    -- modified-since ts / page token / opaque
    last_run_at    TEXT,
    last_status    TEXT,                    -- ok | partial | error
    PRIMARY KEY (endpoint, entity_type)
);

-- Last-known field envelope per (neutral_id, endpoint, field): source of truth for diffing
CREATE TABLE field_envelopes (
    neutral_id     TEXT NOT NULL,
    endpoint       TEXT NOT NULL,
    entity_type    TEXT NOT NULL,
    field          TEXT NOT NULL,
    value_json     TEXT,
    source_endpoint TEXT,
    source_revision TEXT,                   -- ETag / revision counter / version
    last_modified_at TEXT,
    last_synced_at   TEXT,
    checksum        TEXT NOT NULL,
    PRIMARY KEY (neutral_id, endpoint, entity_type, field)
);

-- Optional: durable conflict/quarantine queue for RS-04 manual resolution
CREATE TABLE conflicts (
    id             TEXT PRIMARY KEY,
    neutral_id     TEXT NOT NULL,
    entity_type    TEXT NOT NULL,
    field          TEXT NOT NULL,
    left_value_json  TEXT,
    right_value_json TEXT,
    detected_at    TEXT NOT NULL,
    resolution     TEXT,                    -- pending | source_wins | target_wins | manual
    resolved_at    TEXT
);
```

Design notes:

- The `field_envelopes` table doubles as the diff baseline and the idempotency ledger; a cycle only
  writes when a computed checksum differs from the stored one.
- Watermark advance and envelope writes for a cycle commit in a single transaction, so a restart never
  loses or double-applies work (see restart safety, section 6).
- SQLite runs in WAL mode with `synchronous=NORMAL` for the single-process case. On Postgres the same
  schema gains real concurrent-writer support and row-level locking for multi-worker fan-out.
- `native_key` is stored as JSON text (SQLite) / `JSONB` (Postgres) so heterogeneous key shapes across
  catalogs share one column.

## 5. Recommended tech stack

Opinionated, current (mid-2026) choices. The theme is: official vendor SDK where it is genuinely
maintained, thin httpx-based REST where it is not, and the lightest orchestration that works.

| Library / tool | Purpose | Why this choice |
| --- | --- | --- |
| `databricks-sdk` | Databricks UC REST + auth; SQL via Statement Execution API | Official, maintained SDK; `WorkspaceClient` covers UC objects and the SQL Statement Execution API for DDL |
| `snowflake-connector-python` (+ `snowflake-snowpark-python` if needed) | Snowflake DDL execution (later) | Official DB-API 2.0 connector; DDL-first workload needs the connector, not Snowpark. Plan key-pair auth (password single-factor is blocked) |
| `httpx` | Async HTTP for Qlik and Collibra REST | Modern async client with connection pooling, HTTP/2, timeouts; the right base when no heavy SDK exists |
| `qlik-sdk` (optional) | Qlik Cloud OAuth2 M2M + REST convenience | Official Platform SDK exists; use it for auth/plumbing, but REST-direct over httpx stays viable and keeps write control explicit |
| `tenacity` | Retries/backoff, honoring 429/`Retry-After` | Composable async-aware decorators; exponential backoff with jitter and predicate-based retry on 429/5xx |
| `pydantic` (v2) | Neutral model, envelopes, diffs, capabilities | Fast v2 core, strict validation, clean serialization for canonical checksums |
| `pydantic-settings` | Typed config + per-tenant credential loading | Type-safe env/secret binding, validated at startup |
| `SQLAlchemy` (2.0) | State store ORM/Core over SQLite and Postgres | One data layer for both engines; 2.0 typed API; async support available |
| `alembic` | Schema migrations | Standard companion to SQLAlchemy; needed the moment the schema evolves |
| **SQLite** (state store, v1 default) | Embedded state database: IdentityMap, watermarks, field envelopes | Zero-ops single-file DB fits a single-process poll service; ACID/WAL is enough for one writer; no server to run |
| **PostgreSQL** (state store, scale-out) | Same state store when concurrent workers / managed backups / multi-tenant isolation are needed | Drop-in via the same SQLAlchemy layer; adds row-level locking / advisory locks for multi-worker safety and operational durability |
| `APScheduler` (3.11.x) | In-process scheduling of sync cycles | Stable line; `AsyncIOScheduler` runs coroutine jobs on the event loop. (4.0 is still pre-release as of mid-2026 — do not ship it) |
| `structlog` | Structured JSON logging with context | Processor pipeline for context binding (tenant, endpoint, neutral_id); integrates with OTel |
| `prometheus_client` | Metrics endpoint (cycle latency, writes, conflicts, 429s) | De-facto Python Prometheus library; simple `/metrics` exposure |
| `opentelemetry-sdk` (+ exporters) | Optional traces and log/trace correlation | Add when a collector exists; correlate a cycle across adapters |
| `uv` | Env, dependency, and lock management | The 2026-converged, fastest packager; reads existing pyproject; strong backing |
| `ruff` | Lint + format | One Rust tool replacing black/isort/flake8 |
| `mypy` | Static type checking (CI source of truth) | Mature; pin one type checker as authoritative (Astral's `ty` is emerging but not yet the CI default) |
| `pytest` + `pytest-asyncio` | Test runner for async code | Standard |
| `respx` | Mock httpx calls in unit tests | Purpose-built httpx mocking with a pytest fixture/marker |
| `vcrpy` | Record/replay real catalog HTTP for contract tests | Captures real Qlik/Collibra responses once, replays deterministically in CI |

Notable non-choices: no Airflow/Dagster/Prefect on day one (too heavy for a poll loop), no Celery/Redis
(no distributed queue needed at one process), and no ORM-heavy web framework (this is a worker, not an
API — a tiny health/metrics HTTP surface is enough).

The database is deliberately staged: the service starts on SQLite (embedded, no infrastructure) and graduates to PostgreSQL only when concurrent writers, managed backups, or multi-tenant isolation demand it. Because all state access goes through SQLAlchemy 2.0 + Alembic, the engine swap is a configuration and migration change, not a code rewrite.

## 6. Deployment & ops

**Run shape.** One container image, one long-running process. Entry point starts the async event loop,
loads and validates config, opens the state store, registers per-pair sync jobs with the scheduler, and
exposes a small HTTP surface for `/healthz` and `/metrics`. Ships as a single Kubernetes Deployment (or
a systemd unit / Docker service) with one replica in the standalone phase.

**Scheduling cadence.** Because change detection is poll-based, cadence is a first-class knob, set per
endpoint/entity in config. Sensible defaults: glossary and data-product metadata every 5-15 minutes,
larger dataset/table catalogs every 30-60 minutes, with jitter so all pairs do not fire simultaneously.
Cadence is bounded below by the tightest endpoint rate limit (section 8). APScheduler's
`AsyncIOScheduler` with `max_instances=1` per job prevents a slow cycle from overlapping itself; a plain
asyncio loop with a semaphore is an equally valid, dependency-free alternative.

**Secret handling per tenant.** Config declares tenants and endpoints; credentials are resolved
indirectly. pydantic-settings binds secrets from environment variables (`QLIK__<TENANT>__CLIENT_SECRET`,
Databricks host/token or OAuth) or from a secret manager (Vault, cloud KMS/Secrets Manager) via a small
provider abstraction. Secrets are never written to the state store or logs (structlog redaction
processor drops known secret keys). Qlik uses OAuth2 client-credentials (M2M); tokens are cached in
memory with refresh-before-expiry, never persisted.

**Idempotency and restart safety.** Every cycle is idempotent by construction: reads are pure, writes
are checksum-gated and concurrency-guarded, and watermark advance is committed in the same transaction
as the envelope updates it depends on. On crash/restart the service reloads watermarks and simply
re-runs; unadvanced watermarks cause a bounded re-scan that no-ops on already-synced records. A cycle
that fails mid-way commits nothing and leaves the watermark where it was.

**Rate-limit budgeting.** Each adapter tracks its remaining request budget and backs off on
`429`/`Retry-After` via tenacity. The scheduler treats a persistent `429` as a signal to lengthen that
endpoint's effective cadence (adaptive backoff). Batch reads and paginate at the largest page size the
API allows to minimize request count per cycle.

## 7. Scaling & evolution

The architecture is intentionally a ladder; climb only when a real limit is hit.

- **SQLite -> Postgres.** Move when concurrent writers are needed (multi-worker), when the state
  outgrows comfortable single-file operation, or when operational needs (HA, backups, point-in-time
  recovery) demand a managed DB. Because everything goes through SQLAlchemy + Alembic, this is a
  connection-string and migration change, not a rewrite. `native_key`/`value_json` become `JSONB`.
- **Poll -> event.** If a catalog later exposes webhooks or an audit/event stream (Qlik does not
  today), an adapter can flip its `change_detection` capability to `events` and feed the same
  `list_changed` pipeline from a subscription instead of a scan. The engine is unchanged; only the
  candidate-source inside the adapter differs. Poll remains the fallback for endpoints without events.
- **Single process -> workers.** When the pair matrix or per-endpoint volume exceeds one event loop,
  partition work by (tenant, endpoint-pair) across multiple processes/replicas. This requires the
  Postgres state store (row-level locking / advisory locks so two workers never process the same
  neutral_id concurrently). A durable job/lease table or a light queue can hand out cycles.
- **In-process scheduler -> orchestrator.** Graduate to Prefect/Dagster/Airflow only when you need
  DAG dependencies, backfills, retries-with-visibility, or a run UI across many flows. For a periodic
  poll loop these add operational weight without payoff; the trigger is cross-flow orchestration, not
  scheduling itself. APScheduler 4.0 (once stable) with a shared data store is an intermediate step
  before a full orchestrator.
- **Adding new endpoints.** Implement the `EndpointAdapter` contract, declare `capabilities()`, add
  config and secret bindings, and register the new pairs. No engine change. Contract tests
  (respx/vcrpy) plus a capabilities conformance test gate the new adapter.

## 8. Risks & open questions

- **Poll frequency vs API rate limits.** Tighter cadence lowers sync latency but risks `429`s and
  budget exhaustion, especially with many tenants sharing a limit. Open question: is the Qlik/Databricks
  rate budget per-tenant or per-OAuth-client, and does it allow the desired freshness at scale? Adaptive
  cadence mitigates but does not eliminate this; needs measurement against real tenant limits.
- **Credential management at multi-tenant scale.** Per-tenant OAuth clients and tokens multiply secret
  surface. Rotation, expiry, and least-privilege scoping across Databricks, Qlik, Snowflake, and
  Collibra need a coherent secret-provider story before onboarding many tenants.
- **Partial-failure and resume.** A cycle that writes to the target but crashes before committing
  envelopes will re-read and re-diff on restart; the concurrency guard (ETag/revision) must reliably
  detect that the target already holds the new value so the retry no-ops rather than double-writing.
  Endpoints *without* optimistic concurrency (some Databricks paths) rely on read-after-write, which
  is only as good as the source's read-your-writes consistency — a risk to validate per endpoint.
- **Schema drift.** The neutral model, the closed Qlik path enum, and native schemas evolve
  independently. An unexpected native field or a removed Qlik patch path can silently drop data or
  fail writes. Mitigation: capability declarations plus strict pydantic validation surface drift as
  errors, and contract tests recorded with vcrpy catch API-shape changes; still an ongoing maintenance
  cost.
- **Two-way convergence and oscillation.** Independent normalization on each side can make a value look
  "changed" on every pass (for example whitespace, ordering, or timezone normalization), causing
  write ping-pong. Canonicalization before checksum is essential; edge cases need explicit test
  coverage.
- **APScheduler 4.0 timing.** 4.0 remains pre-release in mid-2026. If multi-process scheduling with a
  shared data store is needed before it stabilizes, plan an interim (Postgres advisory locks + 3.11 or
  a custom asyncio loop) rather than shipping a pre-release scheduler.

# Citations

- https://databricks-sdk-py.readthedocs.io/ — Databricks SDK for Python documentation (WorkspaceClient, auth).
- https://databricks-sdk-py.readthedocs.io/en/stable/workspace/sql/statement_execution.html — Databricks SDK Statement Execution API for running SQL DDL.
- https://pypi.org/project/snowflake-connector-python/ — Official Snowflake Connector for Python (DB-API 2.0).
- https://www.python-httpx.org/ — httpx async HTTP client documentation.
- https://tenacity.readthedocs.io/ — Tenacity retry/backoff library (async-aware, jitter, predicates).
- https://qlik.dev/authenticate/oauth/getting-started-oauth-m2m/ — Qlik Cloud OAuth2 machine-to-machine authentication guide.
- https://qlik.dev/toolkits/platform-sdk/ — Qlik Platform SDK (Python) overview.
- https://qlik.dev/apis/rest/ — Qlik Cloud REST API reference.
- https://developer.collibra.com/api/references/import — Collibra Import REST API (Version 2) reference.
- https://developer.collibra.com/tutorials/tut_rest-api-client-create — Collibra REST API client guidance.
- https://docs.pydantic.dev/latest/ — Pydantic v2 documentation.
- https://docs.pydantic.dev/latest/concepts/pydantic_settings/ — pydantic-settings for typed config and secrets.
- https://docs.sqlalchemy.org/en/20/ — SQLAlchemy 2.0 documentation.
- https://alembic.sqlalchemy.org/ — Alembic database migrations.
- https://apscheduler.readthedocs.io/ — APScheduler documentation (AsyncIOScheduler; 4.0 pre-release status).
- https://github.com/agronholm/apscheduler/issues/465 — APScheduler 4.0 progress tracking (pre-release as of 2026).
- https://www.structlog.org/ — structlog structured logging.
- https://github.com/prometheus/client_python — prometheus_client Python metrics library.
- https://opentelemetry.io/docs/languages/python/ — OpenTelemetry Python SDK (traces, metrics, log correlation).
- https://docs.astral.sh/uv/ — uv Python packaging and environment manager.
- https://docs.astral.sh/ruff/ — Ruff linter and formatter.
- https://mypy.readthedocs.io/ — mypy static type checker.
- https://docs.pytest.org/ — pytest testing framework.
- https://lundberg.github.io/respx/ — RESPX httpx mocking for tests.
- https://vcrpy.readthedocs.io/ — VCR.py record/replay of HTTP interactions.
