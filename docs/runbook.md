# Operator runbook — QLabs Catalog Sync

This is the document to read when the sync is misbehaving, or before you point it at a
real tenant for the first time. It covers deploying the service, configuring one
source-to-Qlik pair, dry-running a change before it lands, confirming identity so writes
never guess, reading what the sync actually did, and diagnosing a red healthcheck.

Everything below describes **RM-01: a one-way Databricks-to-Qlik metadata sync.** Qlik
is the only write target. Nothing here ever deletes anything in Qlik, and nothing binds
a source object to a Qlik object without a human confirming it first (or, on an empty
target space, an explicit `--create-missing`).

## Read this before you point it at a real tenant

**This software has never run against a live Qlik tenant or a live Databricks
workspace.** It was built and tested entirely against mocks — respx-mocked HTTP, an
SDK-provided fake connector, and hand-authored cassettes. Every wire-level assumption
(whether Qlik enforces the ETag it is sent, whether a filter parameter means what the
docs imply, the exact shape of a paginated response) was inferred from API reference
documentation, not observed against a real system.

That is not a defect to apologize for — it is exactly why
**[`docs/tenant-verification.md`](tenant-verification.md)** and its probe script,
`scripts/tenant_probe.py`, exist. Run that checklist against a real tenant — read-only
first, then the write-path checks against a disposable space — before this connector
ever touches a production catalog, and again after any change to the Qlik or Databricks
connector's read/write logic. A passing test suite proves the connector does what its
own code says; it does not prove Qlik and Databricks actually behave the way the code
assumes. Do not skip this. Treat it as the pre-production gate it is, not an optional
extra.

The ["Known gaps"](#known-gaps) section at the end of this document names the two
concrete operational risks that follow from that unverified status, in addition to the
tenant-verification checklist itself.

---

## 1. Deploy

The image (`Dockerfile`, repo root) runs one process: `serve` by default. `serve` builds
one `SyncLoop` per configured pair, hands them to the scheduler, and runs each pair on
its own cadence for as long as the process lives — this is the shape a deployment wants
(a Kubernetes `Deployment`, not a `Job`).

```
docker run -d \
  -v ./deploy/config/acme.yaml:/etc/qlabs-catalog-sync/config.yaml:ro \
  -v qlabs-data:/data \
  --env-file deploy/config/.env \
  -p 8080:8080 \
  qlabs-catalog-sync
```

What that gets you:

- **One process, one job per pair, on its own cadence.** Each pair's `cadence_seconds`
  (from its config, see [Configure a pair](#2-configure-a-pair)) drives its own
  APScheduler interval job, with ~10% jitter (capped at 60s) so pairs sharing an
  endpoint don't all fire in lockstep, and `max_instances=1` so a slow cycle is never
  double-run.
- **`/healthz` and `/metrics` on port 8080.** `/healthz` returns `200` while every
  configured endpoint is healthy (or before any cycle has run yet), `503` the moment one
  is quarantined — see [Respond to a red healthcheck](#6-respond-to-a-red-healthcheck).
  `/metrics` is Prometheus text exposition format. The container's own `HEALTHCHECK`
  probes `/healthz` already.
- **Migrations run automatically.** Every CLI command (`run`, `dry-run`,
  `identity-confirm`, `serve`) migrates the state database to head before doing
  anything else. There is no separate "run migrations" step.
- **Non-root.** The container runs as a fixed, non-root `qlabs:qlabs` (uid/gid 1000).
- **State lives on a mounted volume.** The SQLite state database
  (`QLABS_STATE_DB`, default `sqlite:////data/qlabs-catalog-sync.db`) and the identity
  review file (`QLABS_IDENTITY_REVIEW_FILE`, default `/data/identity-review.json`) both
  live under `/data`. **Mount a real, persistent volume there** — a named Docker volume,
  a host bind mount, or a Kubernetes `PersistentVolumeClaim`, writable by `1000:1000`.
  Anything written there is lost the moment the container is removed if you do not.

Graceful shutdown: `SIGTERM` pauses the scheduler immediately, then gives a cycle
already in flight up to `--shutdown-timeout` (default 30s) to finish before abandoning
it — a cycle that finishes has already paid for its API budget against Qlik/Databricks,
and throwing that away on every deploy/rollout is waste. Past the timeout the cycle is
abandoned rather than waited on forever; the engine's single-transaction commit (see
[Known gaps](#known-gaps)) means an abandoned cycle never leaves the state store
half-written.

Creation of new Qlik objects (`--create-missing`) is **off by default on `serve`**, same
as on `run`/`dry-run` — see [Configure a pair](#2-configure-a-pair) and
[Confirm identity matches](#4-confirm-identity-matches) for why, and pass
`--create-missing` explicitly on the `serve` invocation if this tenant's first sync
needs it (an empty Qlik space has nothing to bind identity against).

Running one pass and exiting instead of staying up (e.g. for a Kubernetes `CronJob`, or
a manual check) is what `run`/`dry-run` are for — the image can run either instead of
`serve` by overriding the container command:

```
docker run --rm \
  -v ./deploy/config/acme.yaml:/etc/qlabs-catalog-sync/config.yaml:ro \
  -v qlabs-data:/data --env-file deploy/config/.env \
  qlabs-catalog-sync run --config /etc/qlabs-catalog-sync/config.yaml
```

## 2. Configure a pair

Start from the two committed templates:

- **`deploy/config/tenant.example.yaml`** — one Databricks source endpoint, one Qlik
  target endpoint, one sync pair. Copy it, replace the `acme` tenant slug and every
  `CHANGE_ME` placeholder. It loads and validates as-is (before you touch it) so you can
  prove the shape is right before you have real credentials.
- **`deploy/config/tenant.env.example`** — the matching secret template. Copy to `.env`
  next to your config copy and fill in real values there; **never put a secret in the
  YAML file itself.** `secrets:` entries in the YAML are references
  (`<ENDPOINT>__<KEY>`), resolved from the environment at startup — nothing sensitive is
  ever written to the config file.

Validate structure (no secrets needed for this step):

```
uv run python -c \
  "from qlabs_catalog_sync.config import EngineConfig; \
   print(EngineConfig.load('deploy/config/acme.yaml'))"
```

Cadence defaults and the arithmetic behind them (why 900s for data products, 1800s for
datasets, and when to widen either) live in `deploy/defaults/README.md` and
`deploy/defaults/sync-pairs.yaml` — read that before hand-picking a cadence for a large
or unusually rate-limited tenant.

### Settings whose consequences are not obvious

- **`catalog_schema_patterns` (on the pair) decides what is synced at all.** This is the
  one setting most worth being deliberate about. During internal testing, a pattern that
  silently failed to match anything behaved as "sync everything" rather than "sync
  nothing" — so prefer an explicit list of `catalog.schema` globs over a broad `"*.*"`
  until a `dry-run` (below) has shown you exactly which `catalog.schema` pairs it
  selects.
- **`activation_opt_in` is off by default, and that is deliberate (decision D7).**
  Flipping a Qlik data product to "active" makes it discoverable **tenant-wide** in
  Qlik's catalog. It never happens as a side effect of a sync; a pair must opt in
  explicitly, and only once you have confirmed every product this pair would activate
  is actually meant to be publicly discoverable.
- **A missing `sql_warehouse_id` on the Databricks endpoint means Unity Catalog tags are
  not read at all — not read as empty (decision D6).** Unity Catalog tags have no REST
  read-back; they can only be read via `INFORMATION_SCHEMA.*_TAGS` over the Statement
  Execution API, which needs a SQL warehouse to run queries against. Omit
  `sql_warehouse_id` and the capability manifest declares `tags` (and, through the same
  tag surface, `classifications`) `na` for that endpoint — a deliberate, honest "cannot
  read this" rather than a silent empty value that looks like "there are no tags."
  `docs/capability-matrix.json` (below) shows both shapes side by side.
- **`target_space` (on the pair) must equal the Qlik endpoint's `space_id`, exactly, or
  config load fails.** The connector always writes into its *own* configured space; a
  pair naming a different space is a config error, not a runtime one — catching it at
  load time (rather than letting every write silently land in the wrong space while the
  run still reports green) is the point. The error names both values and both places
  they came from.

### Generated capability matrix

`docs/capability-matrix.json` is generated straight from each connector's live
`CapabilityManifest` — never hand-written, so it cannot drift from what the code
actually declares. It shows, per entity type and field, whether Qlik/Databricks
can write (`rw`), only ever return (`ro`), or has no way to express (`na`) that field —
including both Databricks shapes (`sql_warehouse_configured` / `no_sql_warehouse`) side
by side, since which one applies depends on that endpoint's own config. Regenerate it
with:

```
uv run python scripts/gen_capability_matrix.py
```

`uv run python scripts/gen_capability_matrix.py --check` regenerates it in memory and
diffs against what is committed, exiting `1` if they disagree — this is the CI gate
against the matrix drifting from the manifests it describes.

## 3. Dry-run and read the plan file

Before ever running for real against a tenant — first sync, config change, connector
upgrade — run `dry-run`:

```
qlabs-catalog-sync dry-run --config deploy/config/acme.yaml \
  --plan-file dry-run-plan.json
```

**`dry-run` performs zero mutations, by construction — not because the CLI remembers not
to call something.** `SyncLoop` itself skips every `create`/`update` call when run in
dry-run mode, so nothing reaches Qlik and nothing is written to the state store, no
matter what the plan contains.

It writes two things:

- **A human-readable summary on stdout** — the same renderer `run` uses. Read it top to
  bottom: counts first, then the exceptional things (failures, orphans, dropped/withheld
  fields, held watermarks), and only then the routine creates/updates — that ordering is
  deliberate, so a reviewer sees what needs attention before scrolling past it.
- **The full machine-readable plan, at `--plan-file`** (default `dry-run-plan.json`).
  One JSON object per invocation, with one entry in `runs` per pair/entity-type
  combination:

  ```jsonc
  {
    "kind": "qlabs-catalog-sync/dry-run-plan",
    "version": 1,
    "generated_at": "...",
    "config_file": "deploy/config/acme.yaml",
    "runs": [
      {
        "pair": "acme-uc-to-qlik",
        "status": "ok",                 // ok | partial | failed | skipped
        "counts": { "read": 42, "created": 3, "written": 5, "unchanged": 34,
                    "no_op": 0, "skipped": 0, "orphaned": 1, "filtered": 2, "failed": 0 },
        "watermark": { "before": "...", "after": "...", "advanced": true,
                       "held_by": [], "has_more": false, "pages": 1 },
        "records": [ /* one entry per candidate object -- see below */ ],
        "orphans": [ /* vanished source objects -- see section 5 */ ],
        "errors": [ /* non-fatal + fatal errors this cycle hit */ ],
        "quarantined_endpoints": []
      }
    ]
  }
  ```

  Read `counts` first for the shape of the run, then walk `records` for detail. Each
  record carries:

  - `outcome` — `created`, `written`, `unchanged` (idempotent no-op — every checksum
    matched), `no_op` (a write was sent and the target reported it already matched —
    what a replayed cycle looks like after a previous run's write landed but its
    transaction did not), `skipped`, `orphaned`, or `filtered` (excluded by
    `catalog_schema_patterns`).
  - `changed_fields` — what the diff wanted to write, before any policy withheld
    anything.
  - `dropped` — fields that genuinely differ but the **target's manifest** cannot carry,
    each with a reason (`read_only`, `not_applicable`, `undeclared`, ...). Cross-check
    against `docs/capability-matrix.json` to see exactly why a given field is dropped
    for a given endpoint.
  - `withheld` — fields the **engine's own policy** held back (activation opt-out,
    manual-edit policy), distinct from `dropped`.
  - `target_skipped_fields` — fields the target reported it did *not* write (an
    unresolved Qlik dataset member, an owner email with no matching Qlik user —
    decisions D2/D3: omitted and reported, never invented).
  - For a `skipped` record, `reason` is one of a fixed, stable set —
    `no_target_binding` (nothing confirmed yet, or creation is off), and its most common
    partner error `unconfirmed_target_binding` (a proposal exists but nobody has run
    `identity-confirm confirm` on it) are the two you will see most on a first sync.

## 4. Confirm identity matches

**The single most important fact about this software: nothing binds a source object to
a Qlik object without an explicit human decision.** A confirmed IdentityMap binding is
the only licence to write to an *existing* Qlik object. There is deliberately no
"confirm everything" shortcut, and an ambiguous match (two Qlik objects share a natural
key equally) is reported for a human to break the tie, never guessed.

```
qlabs-catalog-sync identity-confirm bootstrap --config deploy/config/acme.yaml \
  --pair acme-uc-to-qlik
qlabs-catalog-sync identity-confirm list                      # --pending is the default
qlabs-catalog-sync identity-confirm confirm <proposal-id>              # unambiguous
qlabs-catalog-sync identity-confirm confirm <proposal-id> --candidate <native-key>  # ambiguous
qlabs-catalog-sync identity-confirm reject <proposal-id> --reason "..."
qlabs-catalog-sync identity-confirm apply                      # bind every recorded `confirm` decision
```

- **`bootstrap` binds nothing.** It reads every object of the pair's entity types at
  both endpoints and proposes matches by natural key (name + type, with the parent path
  ignored by default for a source-to-Qlik pair — see `--parent-path-rule`), then writes
  the proposals to the review file. Nothing is bound until `confirm` or `apply` runs.
- **`confirm` refuses an ambiguous proposal with no `--candidate`.** Two target objects
  sharing a natural key equally is a structural fact, not something the CLI is entitled
  to break the tie on by picking one.
- **`apply` binds every `confirm` decision already recorded in the review file** — for
  someone who edited the file by hand (`"decision": "confirm"`, plus
  `"chosen_native_key"` for an ambiguous entry) instead of running `confirm` once per id.
- **On an *empty* Qlik space, `bootstrap` has nothing to match against — it fails
  outright** ("no existing ... objects were found to bootstrap identity against"). This
  is expected, not a bug: identity bootstrap matches source objects against *existing*
  Qlik objects, and there is nothing existing yet. **The first sync into an empty space
  needs `--create-missing`** on `run`/`dry-run`/`serve` instead — every matched source
  object is then created fresh, and the binding is recorded automatically (no bootstrap
  step required, since there is nothing to match against).

  Conversely, if the target space **already has** data products corresponding to your
  source schemas, run `bootstrap` first and confirm every match before ever running with
  `--create-missing` — otherwise the sync creates duplicates of objects that already
  exist, instead of updating them.

## 5. Read the orphan report

**The sync never deletes anything in Qlik (decision D4).** When a source object that was
previously synced disappears — deleted upstream, or moved outside the pair's
`catalog_schema_patterns` — the engine records it as an **orphan**: a note that "this
object used to exist at the source and no longer does," surfaced in the run report and
persisted in the state store so it carries forward until resolved.

There is no separate "orphan list" command. Read it from a `run`/`dry-run` report:

- On stdout: `orphaned -- gone at the source, never deleted (<n>):` followed by each
  object's native key.
- In the JSON plan: the `orphans` array (one entry per orphan: `neutral_id`,
  `entity_type`, `endpoint`, `native_key`, `observed_at`), and the same object also
  appears once in `records` with `"outcome": "orphaned"`.

**What it means, concretely: the Qlik object is untouched and still live in the
tenant.** Nothing about the sync removed it, deactivated it, or changed it. If the
source object reappears later (renamed back, un-deleted, matching a widened selector
again), the orphan resolves automatically on the next cycle that observes it. Until
then, the decision of what to do about a real deletion — deactivate the Qlik product by
hand, leave it, migrate its ownership — is the operator's, deliberately never the
engine's; automating that is explicitly out of scope for v1.

## 6. Respond to a red healthcheck

`GET /healthz` returns `200` with `{"status": "ok", ...}` while every endpoint that has
reported in is healthy (an endpoint that has not reported yet — e.g. right after
startup, before its first cycle — counts as healthy: there is nothing yet known to be
broken). It flips to **`503`** with `{"status": "degraded", "components": {...}}` the
moment **any one** configured endpoint is quarantined. The response body names which
component and why (`components.<endpoint>.detail`).

**An endpoint is quarantined by an `AuthError`, anywhere in a cycle — a preflight
healthcheck or an actual read/write call.** That is the one exception whose cycle stops
immediately, commits nothing, and reports the endpoint in `quarantined_endpoints`; every
other pair keeps running normally.

Distinguish the three things a red healthcheck / non-zero exit usually means, using the
exit code and the structured (JSON, on stderr) logs together:

| Symptom | Exit code | What to look for in the logs |
| --- | --- | --- |
| **Credentials wrong** (bad client id/secret, expired token, revoked service principal/OAuth client) | **3** (endpoint unreachable) | An error report / log line with `"kind": "AuthError"` and the failing `endpoint`/`operation` named. This is what `/healthz` calling that component `degraded` almost always means. Fix: open the endpoint in the console, re-enter the credential and press **Test connection** -- it takes effect on the next cycle with no restart. For an endpoint that resolves through `env:<PREFIX>` instead, correct the variable in `.env` / the secret manager and restart. |
| **Endpoint down / genuinely unreachable** (network partition, wrong `host`/`base_url`, the vendor is having an outage) | **3** (endpoint unreachable) | A preflight `healthcheck` call failing with `"kind": "HealthCheck"` (the connector's own healthcheck reported itself unhealthy, distinct from an `AuthError`) — check the `message`/`detail` for a connection-level reason rather than an auth-level one. Fix: confirm the configured `host`/`base_url` is correct and reachable from where the container runs; check the vendor's status page. |
| **Sustained rate limiting (429s)** | **0** if the retry budget absorbed it, **1** if it did not | `"sync.retry"` log lines (`operation`, `endpoint`, `attempt`, `delay_seconds`) and the `qlabs_sync_rate_limited_total` counter on `/metrics`, both climbing. A single 429 is absorbed automatically (`Retry-After`-aware backoff) and never surfaces as a failure at all. If retries are *exhausted* mid-cycle, that cycle aborts with `"kind": "TransientError"` in its error report — this does **not** quarantine the endpoint (it is a cycle-level failure, not "this endpoint is broken"), so it shows up as exit code **1** ("some records failed"), not 3, and other pairs against the same endpoint keep running. See [Known gaps](#known-gaps) for why sustained pressure needs a cadence change, not a code fix, today. |

The full exit-code contract (`run`/`dry-run`; `serve` never exits on its own, so this
applies most directly to one-shot invocations and to reading a stuck `serve` process's
logs):

| Code | Meaning |
| --- | --- |
| **0** | Every cycle in this invocation completed cleanly — committed, nothing outstanding, no errors. |
| **1** | Ran to completion, but something about the *work* did not finish: a record failed, a cycle came back `partial`/`failed` without an endpoint being quarantined, or non-fatal errors were collected. |
| **2** | The config file, an endpoint's settings/secrets, or a CLI argument was invalid. Nothing was attempted against a live endpoint. (Also Click's own exit code for a bad CLI usage.) |
| **3** | A configured endpoint could not be reached or authenticated — `AuthError` anywhere in the cycle, or a preflight healthcheck reporting the endpoint unhealthy. |

For a `serve` process that is up but reporting `503`, the same three log signatures
above tell you which of the three situations you are in; there is no separate exit code
to read since the process does not exit.

## Known gaps

Two operational limits are worth knowing before they surprise you in production, on top
of the "never run against a live tenant" caveat at the top of this document:

- **The dual-write / create-duplication window.** A `create` is anchored into the
  identity map the moment Qlik confirms it — in its own small, immediate transaction,
  separately from the rest of the cycle's single end-of-cycle commit — specifically to
  close most of this gap. But a hard process kill (OOM kill, host crash, or a `serve`
  shutdown that abandons a cycle past `--shutdown-timeout`) landing in the narrow window
  between "Qlik accepted the create" and "that anchor transaction commits locally" is
  still possible. On restart, the engine has no record of the binding, so if the same
  source object is still selected and `--create-missing` is still enabled, the next
  cycle creates a **second** copy of it in Qlik. **The mitigation is that creation is
  opt-in and off by default** (`--create-missing`) — this risk only exists for a pair
  actually running with it on, most commonly a tenant's very first sync into an empty
  space. If you suspect this happened, check for two Qlik data products with the same
  name/source native key.
- **No adaptive rate-limit backoff that lengthens cadence under sustained 429s.**
  Per-request retry (honoring `Retry-After`, exponential backoff otherwise) exists and
  absorbs a single hit fine. Nothing paces requests proactively or reschedules a pair's
  cadence in response to sustained pressure — that is genuinely unbuilt, not merely
  unconfigured. The cadence defaults in `deploy/defaults/README.md` are sized with
  headroom specifically to make this less likely to bite; if you observe sustained 429s
  in the logs or a climbing `qlabs_sync_rate_limited_total`, widening that pair's
  `cadence_seconds` (or narrowing its `catalog_schema_patterns`) is currently the
  operator's decision to make, not something the engine will do for you.
