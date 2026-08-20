# Cadence + rate-limit budgeting defaults (T9.3)

This directory ships the default sync cadences for a Databricks -> Qlik tenant and the
arithmetic that justifies them, per RM-01's WP9. It owns exactly this path
(`deploy/defaults/`); the per-tenant config/secret template lives in `deploy/config/`
(T9.2), containerization in the repo's `Dockerfile` (T9.1), and the operator runbook in
`docs/runbook.md` (T9.4).

**Files:**

- `sync-pairs.yaml` — a loadable `EngineConfig` fragment (see "Verified" below) showing
  the recommended two-pair pattern and cadence numbers. Copy its `pairs` entries into a
  real tenant config and replace the endpoint placeholders.
- This `README.md` — the arithmetic, the rate-limit reference numbers, and an honest
  accounting of what is code-enforced today versus advisory-only.

## The one-cadence-per-pair tension, and how it's resolved

`SyncPairConfig` (`packages/qlabs-catalog-sync/src/qlabs_catalog_sync/config.py`) carries
exactly one `cadence_seconds` for the whole pair, and a pair's `entity_types` can list
several entity types that all run on that one cadence — there is no per-entity-type
cadence field. But data products and datasets want different cadences (below). Nothing
in `EngineConfig`/`SyncPairConfig` requires a pair's `(source, target,
catalog_schema_patterns)` tuple to be unique — only `name` must be — so the resolution
does not need a schema or code change: **configure two pairs over the same source and
target endpoints**, one scoped to `entity_types: [data_product]` on the fast cadence,
one scoped to `entity_types: [dataset]` on the slow cadence. `sync-pairs.yaml` ships
exactly this shape. Verified empirically (see "Verified" below): `SyncScheduler` builds
one independent APScheduler job per pair, so the two pairs' jobs run on their own
intervals without interfering with each other.

## Cadence defaults shipped

| Pair | `entity_types` | `cadence_seconds` | Band (RS-07 §6) | Jitter |
| --- | --- | --- | --- | --- |
| `databricks_to_qlik_data_products` | `[data_product]` | **900** (15 min) | 5-15 min | automatic, ~60s |
| `databricks_to_qlik_datasets` | `[dataset]` | **1800** (30 min) | 30-60 min | automatic, ~60s |

Jitter is not a config field anywhere — `SyncScheduler` applies
`min(cadence_seconds * 0.10, 60.0)` seconds of jitter to every pair's `IntervalTrigger`
automatically (`DEFAULT_JITTER_FRACTION`/`DEFAULT_JITTER_CAP_SECONDS`,
`packages/qlabs-catalog-sync/src/qlabs_catalog_sync/scheduler.py`). There is nothing an
operator needs to set for "jitter on"; it is on by construction the moment a
`SyncScheduler` is built.

## The arithmetic

Numbers below are derived from what the code actually does per cycle
(`packages/qlabs-connector-databricks/src/qlabs_connector_databricks/changes.py`,
`.../read.py`, `.../sql_tags.py`; `packages/qlabs-connector-qlik/src/qlabs_connector_qlik/write.py`,
`.../resolve.py`) against the published Qlik rate limits (RS-02 §3.4) and the qualitative
Databricks limits (RS-01 §3.6). Worked example throughout: **1 catalog, 20 schemas, 50
tables per schema (1,000 tables)** — adjust the formulas for your own tenant size.

### Databricks read cost — paid every cycle, regardless of what changed

`list_changed` has no server-side "modified since" filter (RS-01 confirms Unity
Catalog's list endpoints take no timestamp parameter), so **every cycle does a complete
catalogs -> schemas -> tables traversal**, whether or not anything actually changed
(`changes.py` module docstring, "There is no server-side 'modified since' filter").
Assuming each individual listing call (catalogs; schemas-of-a-catalog;
tables-of-a-schema) fits in one server page — reasonable at this scale, since RS-01 §3.6
only says some UC list calls "default to unbounded/large page sizes"; a much larger
tenant should re-derive this using `pages = ceil(item_count / observed_page_size)` per
listing level:

- **`data_product` stream** (`_scan_schemas`): 1 catalogs call + 1 schemas call (per
  catalog) + 1 "cheap" table-membership call **per schema** (fingerprints table
  membership into the schema's checksum, `omit_columns`/`omit_properties`/
  `omit_username`) = `1 + C + S` = `1 + 1 + 20` = **22 GETs/cycle**.
- **`dataset` stream** (`_scan_tables`): same traversal shape, full table payload
  instead of the cheap one = `1 + C + S` = **22 GETs/cycle** (heavier response bodies,
  same request count).

(`C` = catalog count, `S` = schema count.) Neither of these depends on table count per
schema — the per-schema table listing is 1 request whether the schema has 5 tables or
500, as long as it fits in one page.

RS-01 §3.6 documents Databricks rate limits only qualitatively ("per-endpoint and
per-workspace limits... 429 with `Retry-After` where applicable"), with no published
numeric ceiling for the UC list endpoints — unlike Qlik, there is no precise formula to
size Databricks-side cadence against. What *is* known, and what actually drives the
30-60 minute `dataset` cadence: `list_changed` for a `dataset` stream carries no per-row
SQL cost by itself, but every **changed** table's subsequent `read()` call does — see
next.

### Databricks SQL tag reads — the real latency cost, paid per changed object read

UC tags have no REST read-back at all; the only way to read them is
`INFORMATION_SCHEMA.SCHEMA_TAGS`/`TABLE_TAGS` over the async Statement Execution API
(`sql_tags.py`). This is **2 SQL statement submissions per catalog** (one for each
table), each requiring a `submit -> poll-until-terminal` round trip against a SQL
warehouse — the highest-latency call in the whole cycle. Critically, this is **not**
amortized across changed objects in the same catalog within one cycle: `read_schema`
and `read_dataset` (`read.py`) each call `read_catalog_tags` fresh, scoped to their own
`schema_names` filter, with no cross-call cache. A cycle that reads 200 changed tables
spread across 10 catalogs costs up to `200 * 2` = 400 statement submissions (worst case,
one changed table per catalog per call), not `10 * 2` = 20. This — not the traversal
request count above — is why the `dataset` cadence needs real headroom: a warehouse-cold
statement can take seconds to reach a terminal state, and re-triggering that cost every
5 minutes for catalogs whose tags rarely change is wasted latency and warehouse load
with no freshness benefit.

### Qlik write cost — bounded by Tier 2, and it only applies to `data_product`

**`dataset` is read-only in the Qlik connector's hands (decision D2)** — v1 never
creates or updates a Qlik object for a table/view; dataset membership is resolved via
`GET`-only calls (`resolve.py`'s `QlikReferenceResolver.resolve_datasets`, paginated,
not one call per dataset) and folded into the owning data product's `datasetIds` field.
**This means the `dataset` pair never touches Qlik's scarce Tier-2 write budget at all**
— its cost is entirely on the Databricks side (above). Qlik's Tier-2 budget is bound
almost entirely by how many **data products** change per cycle.

Per RS-02 §3.4: Tier 1 (`GET`) = 1,000 req/min/user/tenant; Tier 2
(create/update/delete) = 100 req/min/user/tenant, evaluated over a 5-minute window;
tenant aggregate = `user_rate_limit * number_of_users * 0.5`. For a sync engine running
as a single service-account "user" against a tenant, the tenant-aggregate formula
degenerates to `100 * 1 * 0.5` = **50 req/min** for Tier 2 — *below* the raw per-user
number. Since it is unclear from RS-02 whether that formula is meant to bind down to
`n=1`, this document budgets against the conservative **50 writes/min** figure and notes
the less conservative 100/min alongside it.

Every attempted `update()` costs 1 pre-read `GET` (Tier 1, idempotency check —
`write.py` point 12a) + 1 write (Tier 2). `create()` costs at least 1 write (Tier 2)
plus a few Tier-1 resolution `GET`s. Worst case for cadence sizing: every configured
data product changes in the same cycle (a cold start, or a bulk edit upstream).

**Sizing rule:** to keep a burst of `N` changed data products inside the conservative
50 writes/min ceiling without triggering sustained backoff, `cadence_seconds >= N * 60 /
50` = `N * 1.2`.

- Worked example, `N = 20` (this doc's scenario): `cadence_seconds >= 24s` — trivial.
  20 changed data products fit inside *any* cadence in the 5-15 minute band with room to
  spare; Qlik's write budget is not what forces the floor at 5 minutes for a tenant this
  size.
- Larger tenant, `N = 500` changed data products in one cycle (a big bulk upstream edit,
  or a cold-start first sync of a large catalog): `cadence_seconds >= 600s` (10 min) —
  squarely inside the 5-15 minute band, and the reason **900s (15 min) is the safer
  default** for a tenant of unknown or large size, while a small, low-churn tenant (tens
  of schemas, like the worked example) can safely tighten to `300s` (5 min) using the
  same rule.

### Recommended cadence, restated as the rule an operator can reuse

- `data_product` pair: `cadence_seconds = max(300, expected_max_changed_data_products_per_cycle * 1.2)`,
  capped at 900. Ships at **900** (the safe default for unknown/large tenants).
- `dataset` pair: **1800** (30 min) by default — sized against Databricks traversal +
  SQL-tag-read latency headroom, not Qlik's write budget (datasets never write to Qlik).
  Widen to **3600** (60 min) for multi-thousand-table catalogs, a SQL warehouse shared
  by several pairs, or observed sustained 429s from Databricks (see the gap below — this
  widening is manual today, not automatic).

## Rate-limit reference

| Endpoint | Tier | Limit | Scope | Source |
| --- | --- | --- | --- | --- |
| Qlik | Tier 1 (`GET`) | 1,000 req/min | per user, per tenant, 5-min window | RS-02 §3.4 |
| Qlik | Tier 2 (create/update/delete) | 100 req/min | per user, per tenant, 5-min window | RS-02 §3.4 |
| Qlik | Tenant aggregate | `user_rate_limit * users * 0.5` | per tenant | RS-02 §3.4 |
| Databricks | per-endpoint / per-workspace | not published numerically | — | RS-01 §3.6 |
| Databricks | Statement Execution API | own throughput limits, not published numerically | — | RS-01 §3.6 |

Both APIs return `429` with `Retry-After` on breach; `HttpEndpoint`
(`packages/qlabs-catalog-sync-sdk/src/qlabs_catalog_sync_sdk/http.py`) already honors
that header and backs off exponentially otherwise — see "Enforced vs advisory" below for
exactly what this does and does not cover.

## Per-endpoint adaptive request budget under sustained 429s: not implemented

The task this directory serves asks for "a per-endpoint request budget that lengthens
cadence under sustained 429s." **This does not exist anywhere in the codebase today**,
and this directory cannot add it: it owns only `deploy/defaults/`, a config directory,
and there is no config knob that would make a nonexistent mechanism real. Shipping a
YAML key that looks like a budget and is read by nothing would be worse than shipping
nothing (`EngineConfig`'s `extra="forbid"` actually makes this impossible by accident —
verified: an invented `rate_limit_budget` key on `EngineConfig` raises a
`pydantic.ValidationError` at load time), so no such key is in `sync-pairs.yaml`.

**What exists today, precisely, and what it does not cover:**

- `HttpEndpoint.request` (`http.py`) retries a **single call** on 429/5xx, honoring
  `Retry-After` when present, exponential backoff otherwise (`tenacity`,
  `max_attempts=5` by default). This absorbs one rate-limit hit per request; it has no
  memory across requests and no concept of a cycle or a cadence.
- `SyncLoop._call` (`packages/qlabs-catalog-sync/src/qlabs_catalog_sync/sync/loop.py`,
  around line 1761) retries a `TransientError` (which a 429 becomes, via
  `retry_after_seconds`) with its own attempt budget, and increments the
  `qlabs_sync_rate_limited_total` counter (`METRIC_RATE_LIMITED_TOTAL`,
  `observability.py`) via `_count_rate_limited` on each rate-limited attempt — but this
  counter is write-only from the loop's perspective. Nothing reads it back.
- `SyncScheduler` (`scheduler.py`) tracks **consecutive cycle failures** per pair
  (`_PairState.consecutive_failures`) and marks a pair degraded in `HealthRegistry`
  after `DEFAULT_DEGRADED_AFTER` (3) in a row — but a cycle that hit sustained 429s and
  still eventually succeeded (because `SyncLoop._call`'s retries absorbed it) is not a
  "failed" cycle by this counter's definition, so it never trips this path either. There
  is no cadence-lengthening logic anywhere in this class; `_add_job` sets each pair's
  `IntervalTrigger` once at registration time from `pair.cadence_seconds` and never
  reschedules it.
- The codebase already documents this gap as a known, shipped, unverified assumption:
  `DBX-RATE-LIMIT-CADENCE` in
  `packages/qlabs-connector-qlik/src/qlabs_connector_qlik/unverified.py` states
  verbatim: *"HttpEndpoint retries 429s with Retry-After-aware backoff (T1.4), so a
  single rate-limit hit is absorbed; nothing paces requests proactively to stay under a
  budget."* The architecture research doc itself (RS-07 §6, "Rate-limit budgeting")
  describes the adaptive-cadence behavior as the intended design — it was never built;
  RS-07 §8 lists it again as an open risk ("Adaptive cadence mitigates but does not
  eliminate this; needs measurement against real tenant limits").

**The exact change needed, and where** (not implemented here — outside this directory's
owned path, `deploy/defaults/`; flagged per this task's instructions rather than built):

1. `packages/qlabs-catalog-sync/src/qlabs_catalog_sync/sync/loop.py` — `SyncRunReport`
   needs to carry how many rate-limited (429) attempts occurred during that cycle (the
   raw count already exists locally inside `_call`'s retry loop via
   `_count_rate_limited`; it is discarded rather than threaded onto the report the
   scheduler receives).
2. `packages/qlabs-catalog-sync/src/qlabs_catalog_sync/scheduler.py` —
   `SyncScheduler._record_outcome` (or a new sibling method) needs to track a
   rate-limited-fire signal per pair the same way it already tracks
   `consecutive_failures`, and when a threshold is crossed, call
   `self._scheduler.reschedule_job(pair.name, trigger=IntervalTrigger(seconds=<lengthened>,
   jitter=..., timezone=UTC))` to lengthen that pair's interval; and ease back to
   `pair.cadence_seconds` after some number of clean fires. This is genuinely new
   scheduling logic, not a config toggle.

Until that lands, the only mitigation against sustained 429s is picking a cadence with
enough headroom up front — which is what the arithmetic above does — and watching
`qlabs_sync_rate_limited_total` operationally.

## What's enforced by code today vs advisory only

| Part of the DoD | Status | Evidence |
| --- | --- | --- |
| Per-pair `cadence_seconds` is read and applied | **Enforced** | `scheduler.py::_add_job` builds `IntervalTrigger(seconds=pair.cadence_seconds, ...)`; verified empirically against `sync-pairs.yaml` (below) — jobs report `interval_seconds=900.0` / `1800.0`. |
| Jitter is applied, `~10%` capped at 60s | **Enforced** | Same job registration; verified empirically — both jobs report `jitter=60.0` (900*0.10=90 and 1800*0.10=180 both exceed the 60s cap). |
| A cycle never overlaps itself (`max_instances=1`, `coalesce=True`) | **Enforced** | Same registration; verified empirically — `max_instances=1`, `coalesce=True` on both jobs. Covered by `tests/scheduler/test_overlap.py`. |
| `SyncScheduler` runs as part of the live service | **Not yet wired** | `SyncScheduler` is fully built and unit-tested (T2.6, `tests/scheduler/`) but is not referenced anywhere in `packages/qlabs-catalog-sync/src` outside its own module and tests — no CLI command constructs one. `cli/wiring.py`'s own docstring names this explicitly: today's `run`/`dry-run` commands call `execute_cycles` once, a single pass over every pair, not a scheduled loop. Wiring `SyncScheduler` into a long-running process is T9.1's DoD ("runs the sync service as a single long-running process"), still pending. |
| Two-pair split for different `data_product`/`dataset` cadences | **Config pattern, enforced by existing validation** | Nothing forbids two pairs sharing `(source, target, catalog_schema_patterns)`; only `name` must be unique (`EngineConfig._validate_pairs`). No code change; demonstrated in `sync-pairs.yaml`. |
| Per-endpoint request budget that lengthens cadence under sustained 429s | **Not implemented, advisory only** | See "Per-endpoint adaptive request budget" above. The cadence *numbers* in this directory are sized with headroom to make this gap less likely to bite; they do not close it. |
| Single-request 429/`Retry-After` handling | **Enforced, but distinct from the above** | `http.py::HttpEndpoint.request`, per-call only — see above for exactly what this does not cover. |

## Verified

```
$ uv run python -c "from qlabs_catalog_sync.config import EngineConfig; \
    cfg = EngineConfig.load('deploy/defaults/sync-pairs.yaml'); \
    [print(p.name, p.entity_types, p.cadence_seconds) for p in cfg.pairs]"
databricks_to_qlik_data_products [<EntityType.DATA_PRODUCT: 'data_product'>] 900
databricks_to_qlik_datasets [<EntityType.DATASET: 'dataset'>] 1800
```

And, built into a real `SyncScheduler` against stub runners carrying these two pairs:

```
databricks_to_qlik_data_products interval_seconds=900.0  jitter=60.0 max_instances=1 coalesce=True
databricks_to_qlik_datasets       interval_seconds=1800.0 jitter=60.0 max_instances=1 coalesce=True
```

## Assumptions made

- Each individual Databricks UC listing call (catalogs; schemas-of-a-catalog;
  tables-of-a-schema) returns all matching rows in a single server page at the worked
  example's scale (1 catalog, 20 schemas, 50 tables/schema). RS-01 does not publish an
  exact page size; a tenant where this does not hold should re-derive the traversal
  request count using `pages = ceil(item_count / observed_page_size)` per listing level
  and re-check the resulting count against its own Databricks rate-limit headroom.
- Qlik's tenant-aggregate formula (`user_rate_limit * number_of_users * 0.5`) is treated
  as applying down to a single service-account user, yielding a conservative 50
  writes/min Tier-2 ceiling rather than the raw 100/min per-user number. If a tenant
  confirms (e.g. via the T8.6 tenant-verification probe) that the aggregate formula does
  not bind at `n=1`, the sizing rule's denominator can move to 100 and every
  `cadence_seconds` floor above halves accordingly.
- The sync engine runs as a single OAuth/service-account "user" per tenant against both
  Qlik and Databricks. Multiple concurrent pairs or processes sharing one tenant's
  credentials would divide the same Tier-2/Tier-1 budget across themselves; the sizing
  rule above should be applied to the sum of expected changed data products across every
  pair sharing a tenant, not per pair.
- "20 schemas of 50 tables" (1,000 tables total) is used as the illustrative scenario
  per this task's instructions; it is not a claim about any real tenant's size.
