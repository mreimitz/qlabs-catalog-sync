---
type: "Research Output"
title: "Neutral Metadata Model Specification (v1)"
description: "A catalog-neutral metadata model with identity mapping, field envelopes, poll-based change detection, and bidirectional mappings for two-way sync, scoped first to Databricks and Qlik."
tags: ["research", "RS-03", "metadata-model", "two-way-sync", "databricks", "qlik"]
timestamp: "2026-08-06T09:30:00Z"
status: "draft"
---

# Neutral Metadata Model Specification (v1)

This is the catalog-neutral internal model for QLabs Catalog Sync and the rules that map it to and
from each catalog endpoint. It synthesizes the vendor API references (RS-01 Databricks, RS-02 Qlik,
RS-05 Snowflake, RS-06 Collibra) and the RS-02 Qlik two-way sync readiness findings. The first
release targets a two-way Databricks and Qlik sync; the model is designed so Snowflake and Collibra
attach later without changing the core engine. Conflict-resolution policy is specified only at the
interface level here and is owned by RS-04.

## 1. Scope

**In scope (v1):** descriptive data-product and dataset metadata, glossary terms and categories,
tags, owners/contacts, and the associations between them.

**Out of scope (v1):** data movement, lineage, data-quality/profiling metrics, access policies and
grants, and the query/metric semantic layers (Databricks metric views, Snowflake semantic views).
Qlik has no metric semantic layer at all, so it cannot be a two-way target; these are excluded from
synchronization and at most carried as descriptive text later.

## 2. Design principles

1. **Endpoint-agnostic core.** The engine speaks only the neutral model; every catalog is a
   pluggable endpoint behind a fixed interface (feeds RM-03).
2. **Data product = governance entity.** The neutral data product models the curated, first-class
   concept (as in Qlik and Collibra). Distribution constructs — Databricks Delta shares and
   Marketplace listings, Snowflake listings and shares — are treated as *projections* of that
   entity, not the entity itself.
3. **Poll-based change detection.** Qlik Cloud emits no webhooks/audit events for items, datasets,
   data products, or glossaries, so the canonical sync loop is scheduled polling with per-endpoint
   watermarks. Event hooks may optimize individual endpoints later but are never assumed.
4. **Field-level provenance.** Every syncable field travels in an envelope carrying its source and
   revision, which is what makes change detection, conflict detection, and idempotency deterministic.
5. **Respect writability.** Read-only fields (lineage, quality, audit stamps, system-managed tags,
   generated identifiers) are never write targets. The engine only mutates confirmed read/write
   fields.
6. **Minimal, native mutations.** The engine computes a diff against the last-known envelope and
   emits the smallest endpoint-native operation, honoring full-replace semantics where an endpoint
   requires them (read-modify-write).

## 3. Core entities

Shared value types used below: `IdentityRef` (endpoint, entityType, nativeKey, tenantId,
secondaryKeys); `Tag` (key, value); `Party` (partyId, displayName, email, role); `TextField`
(plain or markdown).

### 3.1 DataProduct

| Neutral field | Type | Notes |
| --- | --- | --- |
| neutralId | UUID | Engine-assigned, stable |
| identities | IdentityRef[] | One per endpoint the product exists in |
| name | string | Human name |
| description | TextField | Short description |
| documentation | TextField (markdown) | Long-form readme/docs |
| status | enum | draft, active, deprecated, archived (neutral) |
| owners | Party[] | Owners and key contacts with roles |
| tags | Tag[] | Key/value |
| datasetRefs | neutralId[] | Datasets composing the product |
| glossaryTermRefs | neutralId[] | Associated glossary terms/glossaries |
| placement | string | Space/domain/collection placement |
| customAttributes | map | Endpoint-specific extras preserved round-trip |

### 3.2 Dataset (Asset)

| Neutral field | Type | Notes |
| --- | --- | --- |
| neutralId | UUID | Engine-assigned |
| identities | IdentityRef[] | Per endpoint |
| name | string | |
| description | TextField | |
| owners | Party[] | |
| tags | Tag[] | |
| classifications | string[] | Business/sensitivity classifications (where writable) |
| glossaryTermRefs | neutralId[] | Linked terms |
| physicalRef | string | Fully-qualified physical name at source |
| assetType | enum | table, view, file/volume, dataset, other |

### 3.3 GlossaryTerm

| Neutral field | Type | Notes |
| --- | --- | --- |
| neutralId | UUID | Engine-assigned |
| identities | IdentityRef[] | Per endpoint |
| name | string | |
| definition | TextField | |
| abbreviation | string | |
| categoryRef | neutralId | Parent category |
| status | enum | draft, verified, deprecated (neutral) |
| tags | Tag[] | |
| stewards | Party[] | |
| termRelations | {type, targetTermRef}[] | Typed term-to-term links |
| assetLinks | {type, targetRef}[] | Term-to-dataset/product links |

### 3.4 Category

`neutralId`, `identities[]`, `name`, `description`, `parentCategoryRef`.

### 3.5 Party and Tag

`Party` and `Tag` are value types reused across entities; parties carry a role so ownership vs
stewardship vs contact is preserved.

## 4. Identity and matching

The **IdentityMap** is the engine's memory: `neutralId` to a set of `IdentityRef`. Native keys:

| Endpoint | DataProduct key | Dataset/Asset key | GlossaryTerm key |
| --- | --- | --- | --- |
| Databricks | listing id / share name | three-level full_name + object id | (no native glossary) map to tags/comments |
| Qlik | data-product id (embeds qri) | secureQri (+ id, resourceId) | term id (UUID) |
| Snowflake | listing global name / share name | fully-qualified name | (no native glossary) map to tags/comments |
| Collibra | data-product asset UUID | asset UUID (+ full-name path) | business term asset UUID |

Rules:

- Always carry `tenantId`/account with every key; keys are only unique within a tenant/account.
- Prefer stable machine keys (UUIDs, secureQri, full_name+id) over human names, which can be renamed.
- On first encounter, bootstrap matching by natural key (name + type + parent path) and require
  human confirmation before binding; thereafter match by the stored IdentityMap only.
- Qlik: `secureQri` is the forward-looking dataset key (legacy `qri` is being deprecated); store it
  as the primary Qlik dataset key. Confirm QRI preservation across space-move and across tenants on
  a live tenant (open item).

## 5. Field envelope

Each syncable field is stored as an envelope, not a bare value:

```
{
  "value": <field value>,
  "sourceEndpoint": "qlik|databricks|snowflake|collibra",
  "sourceRevision": "<etag | revision counter | updatedAt>",
  "lastModifiedAt": "<RFC3339 from source>",
  "lastSyncedAt": "<RFC3339 engine time>",
  "checksum": "<hash of normalized value>"
}
```

The checksum drives idempotency (no write when unchanged); `sourceRevision` + `lastModifiedAt` feed
conflict detection and the RS-04 resolution policy.

## 6. Change detection (poll model)

Per endpoint the engine keeps a high-water mark and, each cycle, lists entities changed since it:

- **Qlik:** Items API `updatedAt` (RFC3339, sortable) for datasets/data-products/glossaries;
  data-product `changelogs` endpoint and `pendingChangesCount`; glossary term `revision` counter and
  `revisions` history; ETag / `if-match` optimistic concurrency on glossary/category/term writes.
  No webhooks, so polling is mandatory.
- **Databricks:** `updated_at` fields on UC objects; SQL/UC reads. No field-level provenance, so use
  snapshot + checksum comparison.
- **Snowflake:** `INFORMATION_SCHEMA` for fresh single-database reads, `ACCOUNT_USAGE` for
  account-wide discovery (higher latency); `SHOW` commands for current state.
- **Collibra:** `lastModifiedOn` on assets; GraphQL Knowledge Graph for efficient reads; Import API
  v2 for bulk pulls.

## 7. Write semantics per endpoint

| Endpoint | Create / update mechanics | Update granularity |
| --- | --- | --- |
| Qlik data product | JSON Patch, `op: "replace"` only, closed 8-value path enum, max 8 ops; arrays full-replace; lifecycle via activate/deactivate/move actions | Field-level replace within a fixed path set |
| Qlik glossary term | POST create; PATCH/PUT update; `change-status` action; ETag concurrency | Field-level (PATCH path enum to confirm on tenant) |
| Databricks | UC REST PATCH for containers; SQL DDL (`COMMENT ON`, `SET/UNSET TAGS`, `ALTER ... OWNER TO`) for table/column; `ALTER VIEW` full replace for metric views | Mixed; no generic table REST update — table metadata goes through SQL |
| Snowflake | SQL DDL authoritative (`COMMENT`, `ALTER ... SET/UNSET TAG`, `CREATE/ALTER LISTING`, `ALTER SHARE`); `CREATE OR ALTER` for some objects | Field-level for comments/tags; manifest replace for listings |
| Collibra | Core REST v2 CRUD on assets/attributes/relations; Import API v2 (REPLACE vs ADD_OR_IGNORE) for bulk | Attribute/relation-level |

Consequence: the engine's writer per endpoint must know whether a field supports partial patch or
requires read-modify-write full replacement (notably Qlik product arrays and Databricks metric
views).

## 8. Neutral to endpoint mapping

RW = readable and writable via API (sync target); RO = read-only; N/A = no native equivalent
(projected/lossy).

### 8.1 DataProduct

| Neutral field | Databricks | Qlik | Snowflake | Collibra |
| --- | --- | --- | --- | --- |
| name | RW (listing/share) | RW | RW (listing) | RW |
| description | RW | RW | RW | RW (attribute) |
| documentation | RW (listing) | RW (readMe) | RW (listing manifest) | RW (attribute) |
| status | RW (publish state partial) | RW (actions) | RO/partial (publish state) | RW (asset status) |
| owners/contacts | RW | RW (keyContacts) | RW (provider profile) | RW (responsibilities) |
| tags | RW | RW (meta.tags) | RW | RW |
| datasetRefs | RW (share objects) | RW (datasetIds, full-replace) | RW (listing objects) | RW (port relations) |
| glossaryTermRefs | N/A | RW (glossaryIds, glossary-level) | N/A | RW (relations) |
| lineage/quality | RO | RO | RO | RO |
| audit/ids | RO | RO | RO | RO |

### 8.2 Dataset

| Neutral field | Databricks | Qlik | Snowflake | Collibra |
| --- | --- | --- | --- | --- |
| name | RW (rename) | RW | RW (rename) | RW |
| description | RW (COMMENT) | RW | RW (COMMENT) | RW |
| owners | RW (OWNER TO) | RW (ownerId assign) | RW (grants/owner) | RW (responsibilities) |
| tags | RW (SET TAGS) | RW (meta.tags) | RW (SET TAG) | RW (tags) |
| classifications | RW (tags) | partial | RO (system classes) / RW (custom tags) | RW |
| glossaryTermRefs | N/A (tags proxy) | RW (glossary links) | N/A (tags proxy) | RW (relations) |
| physicalRef | RO (identity) | RO (secureQri) | RO (FQN) | RO |
| lineage | RO | RO | RO | RO |

### 8.3 GlossaryTerm

| Neutral field | Databricks | Qlik | Snowflake | Collibra |
| --- | --- | --- | --- | --- |
| name | N/A | RW | N/A | RW |
| definition | N/A | RW | N/A | RW (attribute) |
| abbreviation | N/A | RW | N/A | RW |
| category | N/A | RW | N/A | RW (domain) |
| status | N/A | RW (draft/verified/deprecated) | N/A | RW (asset status) |
| stewards | N/A | RW | N/A | RW (responsibilities) |
| termRelations | N/A | RW (relatesTo, 14 types) | N/A | RW (relations) |
| assetLinks | N/A | RW (linksTo) | N/A | RW (relations) |

Databricks and Snowflake have no native glossary; neutral terms project onto their tags/comments as
a lossy, best-effort carry (term name as a tag, definition as a comment), and that projection is
one-directional unless a future glossary capability appears.

## 9. Endpoint interface contract (feeds RM-03)

Every endpoint plugin implements:

- `capabilities()` — per entity/field: RW/RO/NA, supports-partial-update, supports-events,
  identity key(s).
- `listChanged(entityType, watermark)` — changed entities since the watermark (poll).
- `read(identityRef)` — current state as neutral entity with field envelopes.
- `create(neutralEntity)` — returns the new native key.
- `update(identityRef, fieldDiff)` — applies minimal native mutation, honoring full-replace where
  required; uses ETag/revision when available.
- `delete(identityRef)` / lifecycle actions.

The engine relies only on this contract; adding Snowflake or Collibra means implementing it, not
changing the core.

## 10. Sync flow and conflict interface (to RS-04)

1. Poll each endpoint via `listChanged`, read changed entities into field envelopes.
2. Resolve identities via the IdentityMap; bootstrap-match new entities with confirmation.
3. Detect field-level differences using checksum + `sourceRevision`.
4. Apply the RS-04 conflict policy (per-field configurable: source-of-truth, last-write-wins by
   `lastModifiedAt`, or manual review queue).
5. Write via `update`, sending ETag/revision to catch concurrent edits; on mismatch, re-read and
   re-resolve.
6. Persist new envelopes and advance watermarks. Unchanged fields are skipped (idempotent).

## 11. v1 scope decision (Databricks and Qlik)

**Synchronized in v1:** name, description, documentation, tags, owners/contacts, and data-product
descriptive attributes on both sides; plus glossary terms/categories mapped Qlik-to-Databricks as a
lossy tag/comment projection (full glossary two-way is reserved for the Qlik and Collibra pair).

**Deferred:** lineage, quality, access policies, and semantic/metric layers.

## 12. Open items to confirm on a live tenant

- Qlik glossary term PATCH path enum, `change-status` request body, and the `/links` payload shape.
- Qlik `secureQri`/`qri` preservation across space-move and across tenants.
- Qlik custom-role permission strings for a sync service account (steward + space roles).
- Databricks metric-view update is full-replace only (no partial patch) — confirm no REST alternative.
- Cross-endpoint status enum reconciliation (neutral status to each native lifecycle).

## 13. Next steps

Prototype the IdentityMap store and a Qlik endpoint adapter against the contract in section 9,
validate the open items in section 12 on a live tenant, and hand the field envelope and conflict
interface (sections 5 and 10) to RS-04 to finalize the default resolution policy.

# Citations

* [Databricks Unity Catalog & Data Products — API Reference](/Research/RS-01-databricks-catalog-api/outputs/databricks-catalog-api-reference.md) — Databricks assets, data products, write semantics, and semantic layer.
* [Qlik Cloud Catalog & Data Products — API Reference](/Research/RS-02-qlik-catalog-api/outputs/qlik-catalog-api-reference.md) — Qlik items, data products, glossary, and identity keys.
* [Qlik Two-Way Sync Readiness — Gaps Closed](/Research/RS-02-qlik-catalog-api/notes/qlik-two-way-sync-readiness.md) — change detection, mutable payload schemas, identity, relationships, permissions.
* [Snowflake Horizon Catalog & Data Products — API Reference](/Research/RS-05-snowflake-catalog-api/outputs/snowflake-catalog-api-reference.md) — Snowflake objects, listings/shares, write semantics.
* [Collibra Catalog & Data Products — API Reference](/Research/RS-06-collibra-catalog-api/outputs/collibra-catalog-api-reference.md) — Collibra operating model, data products, business semantics.
