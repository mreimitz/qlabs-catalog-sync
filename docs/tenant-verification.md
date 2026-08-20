# Tenant verification checklist

This is the checklist a human runs against a real Qlik tenant (and, where a SQL
warehouse is configured, a real Databricks workspace) before trusting this software
with a production catalog. Run it once before the first production sync, and again
after any change to the Qlik or Databricks connector's write/read logic.

## Read this first: what "verified" actually means here

**This connector was built and tested entirely against mocks.** RM-01's own scope
decision says so explicitly: "The MVP is also built without live tenants: all tests run
against respx mocks, an SDK-provided fake connector, and hand-authored cassettes." No
live Qlik tenant and no live Databricks workspace were available during development.
Every wire-level detail this checklist covers — whether an endpoint enforces
optimistic-concurrency headers, whether a response carries the header the connector
reads, whether a filter parameter means what the vendor's other docs imply it means —
was inferred from the Qlik and Databricks API reference documentation
(`planning/Research/RS-01-databricks-catalog-api/`, `planning/Research/RS-02-qlik-catalog-api/`),
not observed against a real system.

That is not a defect to apologize for — it is why this checklist and
`scripts/tenant_probe.py` exist. But it means: **do not read a passing test suite as
proof this connector works against a real tenant.** It proves the connector does what
its own code says it does. Whether what the code says matches what Qlik and Databricks
actually do is exactly what is unconfirmed, and exactly what this document is for.

The full machine-readable version of every item below lives in
`packages/qlabs-connector-qlik/src/qlabs_connector_qlik/unverified.py` (`REGISTRY`).
This document does not restate that data field-by-field — it tells you, as the person
doing the work, what to run and what a pass/fail looks like. Read an item's full
`assumption`/`consequence`/`source` text in the registry (or in the probe's report,
which quotes the registry directly) when you need the detail behind a checklist row.

## Before you start

1. **Credentials, never hard-coded, never typed into a prompt.** Export the same
   environment variables the connector itself reads via `ConnectorConfig.for_endpoint`:

   ```
   QLIK__BASE_URL=https://<tenant>.<region>.qlikcloud.com
   QLIK__CLIENT_ID=<oauth client id>
   QLIK__CLIENT_SECRET=<oauth client secret>
   QLIK__SPACE_ID=<target space id>
   # optional: QLIK__SCOPE (defaults to user_default)

   # only if you are also checking the Databricks items:
   DATABRICKS__HOST=https://<workspace-host>
   DATABRICKS__CLIENT_ID=<service principal client id>
   DATABRICKS__CLIENT_SECRET=<service principal client secret>
   DATABRICKS__SQL_WAREHOUSE_ID=<warehouse id>   # required for the DBX-* items
   ```

2. **Use a disposable space, not a production one, for the first pass.** The
   `--include-destructive` checks below create one throwaway Qlik data product and
   delete it again; there is no reason to run that against a space real consumers
   depend on when a scratch/sandbox space will do just as well for the first
   confirmation. Re-run against the actual production target space afterward if you
   want end-to-end confidence in that specific space's permissions (see
   `QLIK-ROLE-PERMISSION-STRINGS` below).

3. **Read-only first.** Run the probe without `--include-destructive` at least once
   before ever passing it, so you see what a clean read-only pass looks like on this
   tenant:

   ```
   uv run python scripts/tenant_probe.py --component qlik
   ```

4. **Then run the write-path checks**, once you are pointed at a space you are
   comfortable creating and deleting a throwaway object in:

   ```
   uv run python scripts/tenant_probe.py --component qlik --include-destructive \
     --sample-dataset-name "<a real dataset name already in the space>" \
     --sample-owner-email "<a real user's email in the tenant>"
   ```

   `--sample-dataset-name` and `--sample-owner-email` are optional but unlock two more
   checks each; omit them and those rows report "skipped" rather than pass/fail. See
   `scripts/tenant_probe.py`'s module docstring for exactly what gets created and how
   it is cleaned up before you run this.

5. **Databricks, if a SQL warehouse is configured:**

   ```
   uv run python scripts/tenant_probe.py --component databricks \
     --databricks-catalog "<a real UC catalog with some tagged objects>"
   ```

6. Read the printed report. Every row is one registry item; `[MUST VERIFY BEFORE
   PRODUCTION]` marks the ones this checklist treats as a hard gate. `PASS`/`FAIL` are
   what they say; `????` (inconclusive) means the probe could not tell either way —
   treat it as unverified, not as passing.

## Section A — must verify before production (automated)

These are load-bearing for the write path the MVP ships: get every one of these to
`PASS` (or resolve a `FAIL` per its "on fail" column) before syncing into a real
customer's tenant. Run with `--include-destructive` and both `--sample-*` flags to
cover all of them in one pass.

| id | what it checks | pass looks like | on fail |
| --- | --- | --- | --- |
| `QLIK-DP-ETAG-PATCH` | Whether Qlik rejects a data-product PATCH whose `if-match` is stale (HTTP 412). The single most important row in this table — this is the connector's whole optimistic-concurrency story. | Probe reports `PASS`: the second of two back-to-back PATCHes with the same stale `if-match` came back 412. | If `FAIL` (the second PATCH succeeded): Qlik is not enforcing `if-match` on this endpoint. Concurrent syncs (or a sync racing a human edit) will silently clobber each other with no error. Do not run two writers against the same product concurrently until this is confirmed fixed or mitigated, and flag it to whoever owns the write-path design — this may need a different concurrency strategy (e.g. serializing writes per product) rather than a code fix in this connector. |
| `QLIK-DP-CREATE-ETAG` | Whether the data-product `POST` (create) response carries an ETag header. | `PASS`. | If `FAIL`: every product's very first `update()` after creation runs unguarded (no `if-match`). Not fatal on its own, but means the concurrency protection has a gap immediately after every create — worth knowing before you rely on it. |
| `QLIK-DP-PATCH-204-ETAG` | Whether a successful PATCH's `204 No Content` response carries a fresh ETag. | `PASS`. | If `FAIL`: every update after the first one on a product runs unguarded too — the concurrency protection effectively never engages past the first write. Combined with a `FAIL` on `QLIK-DP-ETAG-PATCH` this is a serious gap; report it. |
| `QLIK-DP-PATCH-CONTENT-TYPE` | Whether Qlik accepts `Content-Type: application/json` for a PATCH's JSON Patch body. | `PASS`. | If `FAIL`: every `update()` call fails outright. This is a one-line fix in `write.py`'s `_send_patch` (send `application/json-patch+json` instead) — see that module's docstring, point 14. Do not ship until this passes or the fix lands. |
| `QLIK-TAGS-CHARSET-CAP` | Whether Qlik accepts a `tags[]` entry containing `=` (the flattened form of a valued key/value tag). | `PASS`. | If `FAIL`: any product carrying a Databricks UC tag with a value (not just a bare key) will fail to create or update entirely. Check whether the failure is specifically about `=` or about length, and consider whether the flattening scheme in `write.py:_tag_values` needs to change. |
| `QLIK-DATASETIDS-RESOURCEID` | Whether the id this connector resolves for a dataset member (`resolve.py`'s `resourceId`-preferring `_dataset_id_of`) is accepted by `datasetIds`. | `PASS`, **and** you have manually confirmed in the Qlik hub UI that the throwaway product actually shows the sample dataset as a member (the probe can only confirm Qlik accepted the id, not that it means what we think it means). | If `FAIL` or the manual UI check disagrees: dataset resolution is sending the wrong id shape. This blocks every product that has member datasets — essentially every real sync. Needs a fix in `resolve.py:_dataset_id_of` before shipping. |
| `QLIK-USERS-FILTER-SYNTAX` | Whether `GET /api/v1/users?filter=email eq '<value>'` is accepted (200) rather than rejected (400). | `PASS`. | If `FAIL`: owner resolution (`resolve.py`) fails hard for every product that has an owner with an email. Decision D3's whole `keyContacts` path is broken. Needs a different filter syntax found from a live tenant response or Qlik support before shipping. |
| `QLIK-KEYCONTACTS-ROLE-VOCAB` | Whether Qlik accepts `keyContacts[].role` values `"contact"` and `"other"` (`"owner"`/`"steward"` are already attested by RS-02's own examples). | `PASS`. | If `FAIL`: any product whose owners include a `CONTACT`- or `OTHER`-role party fails to write entirely (the whole request, not just that contact). Either confirm the real vocabulary from a live tenant error message, or map those two neutral roles onto `"steward"`/omit them until confirmed. |
| `QLIK-DP-LIST-ENDPOINT` | Whether `GET /api/data-governance/data-products` (no id) exists and returns a `{data: [...]}` shape. Flagged in `read.py`'s own docstring as "the single riskiest assumption in this module." | `PASS`. | If `FAIL`: `list_changed(DATA_PRODUCT)` cannot enumerate existing Qlik-side products. The engine loses the ability to detect drift on products not already in its IdentityMap — updates to existing (not-yet-mapped) products will not be found. `create()` still works. Escalate before relying on update-path drift detection. |
| `QLIK-DATASET-RESOURCE-ATTRS` | Whether Items-API dataset detail GETs reliably carry `resourceAttributes.secureQri`. | `PASS` (all sampled items carry it). | If `????`/`FAIL`: some datasets' identity is silently degrading to the less-durable Items-API item id. Re-run with a larger, more representative sample; if it is a real, reproducible gap, treat `secureQri` as unreliable for those datasets and expect possible identity-map churn for them. |
| `QLIK-DP-STATUS-ACTIVATED-SIGNAL` | Whether a deactivated product is distinguishable from a never-activated one. The automated check is informational only. | Manually confirm: activate a disposable product, deactivate it, then GET it and diff the response against a product that was never activated. If the two are byte-identical apart from `activated`, the assumption holds. | If they differ (e.g. `activatedOn` history persists): good — read.py could use that field later for a richer status mapping, but nothing needs to change today. If a genuinely fresh-looking response is indistinguishable and status reconciliation (D7) is later wired up, flag that a re-activation after an intentional deactivation may look identical to a first activation. |
| `QLIK-ROLE-PERMISSION-STRINGS` | Whether the configured service account can create, update, and delete in the target space (a practical proxy for "does it hold the right custom role"). | The probe's create/update/delete sequence completes with no auth error, **and** you have separately confirmed the exact assigned role against the tenant's `help.qlik.com` permission matrix (see RS-02 section 5) for the specific named role, since the probe can only observe success/failure, not which role granted it. | If any step in the probe fails with an auth error: the service account is missing a specific permission (create in space / update in space / delete). Fix the role assignment on the tenant before shipping — do not work around it by disabling the failing operation. |

## Section B — Databricks, must verify before production (automated)

Only relevant when `DATABRICKS__SQL_WAREHOUSE_ID` is configured (decision D6 — tags are
`na` otherwise, and none of this applies).

| id | what it checks | pass looks like | on fail |
| --- | --- | --- | --- |
| `DBX-SCHEMA-TAGS-COLUMNS` | `INFORMATION_SCHEMA.SCHEMA_TAGS` has exactly the columns `sql_tags.py` selects by name. | Probe's `DBX-SQL-TAGS-READ` row is `PASS`. | If `FAIL`: every catalog's tag read fails outright (loud, not silent) until `sql_tags.py`'s `_SCHEMA_TAGS_COLUMNS` is corrected to the real column names. |
| `DBX-TABLE-TAGS-COLUMNS` | Same, for `INFORMATION_SCHEMA.TABLE_TAGS`. | Same `DBX-SQL-TAGS-READ` row. | Same fix, in `_TABLE_TAGS_COLUMNS`. |
| `DBX-STATEMENT-EXEC-RESPONSE-SHAPE` | Whether `status.state`/`result.data_array`/chunk continuation parse the way `sql_tags.py` expects. | Same `DBX-SQL-TAGS-READ` row, with a nonzero schema/table tag count if the catalog you probed actually has tagged objects. | If the probe reports success but with suspiciously **zero** tags on a catalog you know has tags: this is the dangerous failure mode named in the registry (`result.data_array` misread as empty looks identical to "no tags"). Manually run the same `SELECT` in a Databricks SQL editor against the same warehouse and compare row counts before trusting a zero. |
| `DBX-IDENTIFIER-CHARSET` | Whether the probed catalog name passes `sql_tags.py`'s conservative `[A-Za-z0-9_]`-only identifier validator. | The probe ran at all (an `IdentifierError` before any HTTP call would show up as `DBX-SQL-TAGS-READ` `FAIL` with that specific message). | If your real catalogs use characters outside that set (hyphens are common), every one of them will have tags refused as unreadable even though nothing is actually wrong with the catalog. This is a false-negative, not a security issue — widen `_IDENTIFIER_RE` in `sql_tags.py` if you hit this. |

## Section C — should verify, not blocking (perf/cost or self-mitigated)

These do not block shipping v1, but confirming them either saves cost/latency at scale
or closes out a low-risk unknown cheaply.

| id | what to do | why it is not blocking |
| --- | --- | --- |
| `QLIK-ITEMS-NAME-FILTER-SEMANTICS` | Run the probe with `--sample-dataset-name`; it reports whether a substring of the name still matches. | `resolve.py` always re-checks results client-side for an exact match, so a fuzzier-than-expected server filter cannot cause a wrong resolution — only extra network calls. |
| `QLIK-LIST-CHANGED-FULL-SCAN` | Manual: check Qlik's developer docs for a documented `updatedAt`-since filter on the Items and data-products list endpoints. | The engine's checksum-based idempotency already makes an unnecessary re-read a no-op; this only affects cost/latency at scale, not correctness. |
| `QLIK-PAGE-SIZE-DEFAULT` | Manual: check Qlik's docs for a documented `limit` maximum, or send one deliberately oversized request and see whether it is rejected or capped. | The pagination helper already walks multiple pages regardless of the per-page size; low risk either way. |
| `DBX-ERROR-CODE-AUTH-VOCAB` | Manual, and only if you have a second catalog/warehouse combination the configured account is deliberately *not* authorized on: query it and record the real `error_code`. | A misclassification here means wasted retries against a doomed call, not data corruption — an operational cost, not a correctness one. |

## Section D — dormant: verify only before enabling the feature that wakes them up

Decision D4 (no destructive lifecycle actions called by the engine in v1) and decision
D7 (activation opt-in, off by default) mean none of these four items affect the MVP as
shipped. They are implemented for contract completeness and stay refused unless a
future task explicitly opts a `LifecycleActions` instance into the specific action —
see `lifecycle.py`'s module docstring for why that opt-in is a constructor-level
capability, not a flag that can be tripped by accident. **Verify each one manually,
against a disposable tenant, before the corresponding feature is turned on** — do not
wait for this checklist to be re-run automatically, since nothing in the engine will
prompt you.

| id | verify before enabling... | how |
| --- | --- | --- |
| `QLIK-MOVE-IDENTITY-STABILITY` | any feature that calls `LifecycleActions.move` | Move a real (disposable) product between two spaces and confirm its `id`/`qri` in a follow-up GET are unchanged. |
| `QLIK-QRI-CROSS-TENANT` | any feature that migrates a resource across tenants | Not currently buildable against v1's scope; revisit if that roadmap item exists. |
| `QLIK-ACTIVATE-NONMANAGED-STATUS` | wiring `LifecycleActions.activate` into the engine (D7 reconciliation) | Attempt to activate a disposable product into a space that is *not* managed, on a disposable tenant, and record the HTTP status Qlik returns. |
| `QLIK-DEACTIVATE-BODY` | wiring `LifecycleActions.deactivate` into the engine | Call deactivate on a disposable product and confirm the empty `{}` body is accepted. |

## Section E — registered, not used in v1 (Track B / RM-05)

Decision D5 puts the Qlik glossary write path out of the MVP entirely —
`glossary.py` is an unimplemented `TODO(T3.6)` stub, and nothing in this build calls
any of the three endpoints below. They are registered because RS-02 already flagged
them as unconfirmed, not because this build depends on them. **No action needed for
the current release.** Re-verify when Track B (RM-05) implements T3.6:

- `QLIK-GLOSSARY-PATCH-PATH-ENUM` — the per-field JSON Pointer paths a glossary-term
  PATCH accepts.
- `QLIK-GLOSSARY-CHANGE-STATUS-BODY` — the request body key for the change-status
  action (`{"status": ...}` vs. `{"type": ...}`).
- `QLIK-GLOSSARY-LINKS-PAYLOAD` — the `POST /links` body shape vs. the inline
  `linksTo` shape.

## Section F — an operational item, not a code path

- `DBX-RATE-LIMIT-CADENCE` — Databricks' rate-limit behavior under the sync loop's
  actual production polling cadence and catalog size. Not automated (a deliberate
  rate-limit stress test is not something this probe launches against a customer's
  tenant by default — see `scripts/tenant_probe.py`'s module docstring). Before going
  live at scale: run the intended production cadence against a real workspace for a
  sustained period and watch for repeated 429s in the logs. `HttpEndpoint` already
  retries with `Retry-After`-aware backoff, so a hit is not immediately fatal, but
  sustained backoff is a sign the polling cadence needs to widen or the catalog scope
  needs to shrink per pair.

## Where each item comes from, if you need the full detail

Every row above is a summary. The full text — the exact assumption, every call site in
the codebase that depends on it, and the citation into RS-01/RS-02 — lives in
`packages/qlabs-connector-qlik/src/qlabs_connector_qlik/unverified.py`. From a Python
shell or a REPL in this workspace:

```python
from qlabs_connector_qlik.unverified import REGISTRY, by_id

by_id("QLIK-DP-ETAG-PATCH")          # one full entry
[e.id for e in REGISTRY if e.must_verify_before_production]  # everything Section A/B covers
```

## If you find this checklist is wrong

If a probe result or a manual check disagrees with what an item's `assumption` field
says — the item was verified and the code's assumption turned out to be correct, or
turned out to be wrong in a way this document does not describe — update
`unverified.py`'s entry (its `status`, `consequence`, and `verification` fields) in the
same change that fixes the underlying code, so the registry never drifts from what is
actually true. An `UnverifiedAssumption` entry that no longer describes an open
question should either be removed (if the underlying behavior can now be asserted
outright in code, with no registry entry needed) or have its status updated to reflect
what is now known — never left saying "unverified" once it has been verified.
