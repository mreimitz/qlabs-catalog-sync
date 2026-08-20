---
type: "Research Note"
title: "Qlik Two-Way Sync Readiness — Gaps Closed"
description: "Implementation-grade Qlik findings on change detection, mutable payload schemas, qri identity stability, relationships, and write permissions for the universal metadata model."
tags: ["research", "RS-02", "qlik", "two-way-sync", "metadata-model"]
timestamp: "2026-08-06T09:00:00Z"
status: "draft"
---

# Qlik Two-Way Sync Readiness — Gaps Closed

Scope: implementation-grade Qlik Cloud findings to design a universal metadata model with reliable two-way sync. Sources are the official Qlik developer portal (qlik.dev) REST references and tutorials, and help.qlik.com. Base tenant URL pattern is `https://{tenant}.{region}.qlikcloud.com` (e.g. `https://your-tenant.us.qlikcloud.com`); all calls use `Authorization: Bearer <token>` (API key or OAuth2 access token).

Note on endpoint families: the Items and Glossaries APIs live under `/api/v1/...`. The Data Products API lives under `/api/data-governance/...` (no `/v1`) and its resource fields differ from the classic Items model.

## 1. Change detection / events

### Modified timestamps

- Data products (`/api/data-governance/data-products`) expose `createdAt`, `createdBy`, `updatedAt`, `updatedBy` (ISO 8601), plus `activatedAt`, `activated`, `activatedOn[]`, and a `pendingChangesCount` integer. Confirmed present on POST/GET/activate responses.
- Items API (`/api/v1/items`) exposes `updatedAt` documented as "The RFC3339 datetime when the item was last updated." Also `createdAt`. Items support sorting on `+/-updatedAt` and `+/-createdAt`. Datasets, dataproducts, and glossaries are surfaced as items (`resourceType` enum includes `dataset`, `dataproduct`, `glossary`, `dataasset`, etc.), so the Items API is a viable cross-resource "what changed" poller.
- Glossary terms expose `createdAt`, `createdBy`, `updatedAt`, `updatedBy`, and a monotonic integer `revision`, plus a nested `status` object with its own `updatedAt`/`updatedBy`.

### ETags / optimistic concurrency

Glossary write endpoints (PATCH/PUT on glossary, category, and by the same pattern term) accept an optional `if-match` header for conditional updates, using the ETag value returned when the resource was last fetched:

```
if-match: <etag-from-last-GET>   (optional; enables conditional update)
```

The Data Products PATCH endpoint is documented as JSON Patch but no `if-match`/ETag header is documented for it — concurrency control there relies on the changelog rather than ETags. Not documented / needs tenant testing whether data-products honors ETags.

### Glossary term revisions endpoint

`GET /api/v1/glossaries/{id}/terms/{termId}/revisions` exists and returns previous revisions of a term. Terms carry an incrementing `revision` number and `updatedAt`/`updatedBy`, and the `status` object records `updatedAt`/`updatedBy` for status transitions. The exact response envelope (pagination shape, whether each revision returns the full historical term body) was not fully captured from the reference page and needs tenant testing.

### Data product changelog

`GET /api/data-governance/data-products/{dataProductId}/changelogs` returns a paginated audit of changes. Each entry has `id`, `createdAt`, `createdBy`, and a `changes[]` array of `{ path, operator, value }` where `operator` is one of `replace`, `add`, `remove` and `path` is one of `/name`, `/description`, `/spaceId`, `/datasetIds`, `/glossaryIds`, `/readMe`, `/keyContacts`, `/tags`, `/activatedOn`, `/apiConsumableDatasetIds`, `/semanticModel`. This is the most reliable per-field change feed for data products.

### Webhooks / audit events — CONFIRMED GAP

Qlik Cloud system events are delivered by webhooks and by the Audits API (same `data` payload, different envelope; migrating to CloudEvents 1.0.2; webhook payloads signed via `Qlik-Signature` HMAC SHA256). The confirmed supported event categories are: Apps, App usages, Reloads, Automations, Data integration project tasks, Users, User identities, User sessions, Groups, Group settings, Roles, Spaces, Tenants, Environments, Licenses, Large app quotas, API keys, API key configs, OAuth clients, OAuth tokens, Rate limits, Reporting tasks, Alerting tasks, Scheduling tasks, Hub dashboards, Web integrations, IP policies, Notification digests, and AI/MCPS tool executions.

```
There are NO webhook / audit event types for:
  - items
  - datasets
  - data products (data-governance)
  - glossaries or glossary terms
```

Implication: real-time push for catalog metadata changes is NOT available. Change detection for datasets/data-products/glossaries must be poll-based: Items API `updatedAt` sorting for datasets/dataproducts/glossaries; the data-product `changelogs` endpoint for field-level product deltas; and term `revisions` + `updatedAt` for glossary terms. (App/dataset reloads do emit `com.qlik.v1.app.reload.finished` / `com.qlik.v1.reload.finished`, but those are data-refresh events, not metadata edits.)

## 2. Exact mutable payload schemas

### Data products — POST (create)

`POST /api/data-governance/data-products` (Content-Type `application/json`, Tier 2 rate limit). Writable fields:

```
name                     string    REQUIRED  display name
description              string    optional
readMe                   string    optional  Markdown supported
spaceId                  string    optional  target space (personal/shared/data); managed space required only to activate
tags                     string[]  optional
datasetIds               string[]  optional  max 100 items
apiConsumableDatasetIds  string[]  optional  must be a subset of datasetIds
glossaryIds              string[]  optional  each a UUIDv4 (<=36 chars); max 100 items
keyContacts              object[]  optional  [{ userId string REQUIRED, role string optional }]
```

Server-side rules: a given `userId` may appear only once in `keyContacts` (one role per user); `apiConsumableDatasetIds` must be a subset of `datasetIds`; creation requires create permission in the target space. Example body:

```json
{
  "name": "Sales Analytics Data Product",
  "description": "Curated sales datasets",
  "spaceId": "a1b2c3d4e5f6g7h8i9j0k1l2",
  "readMe": "# Sales\nCurated sales datasets.",
  "tags": ["sales", "revenue"],
  "datasetIds": ["6672d8b7a182224cbb3f1c26"],
  "apiConsumableDatasetIds": ["6672d8b7a182224cbb3f1c26"],
  "glossaryIds": ["123e4567-e89b-12d3-a456-426614174000"],
  "keyContacts": [{ "userId": "6909d8524392dbbab822c7f7", "role": "owner" }]
}
```

The create response returns the full object including `id`, `qri`, `ownerId`, `tenantId`, `activated:false`, `createdAt/By`, `updatedAt/By`, and empty arrays for `datasetIds`, `apiConsumableDatasetIds`, `glossaryIds`, `keyContacts`, `activatedOn`.

### Data products — PATCH (update) semantics

`PATCH /api/data-governance/data-products/{dataProductId}` uses JSON Patch (array of operations). Only `op: "replace"` is accepted, `path` is a closed enum, and `value` is a string (for `/name`,`/description`,`/readMe`, or null), a unique-string array, or an object array (for `/keyContacts`). Array paths are full-replace (send the complete desired list, not a delta). Max 8 operations per request (matches the 8 writable fields). Returns `204 No Content`.

```
op    "replace"   (only)
path  one of: /name  /description  /datasetIds  /glossaryIds  /readMe  /keyContacts  /tags  /apiConsumableDatasetIds
value string | string[] | object[]   (shape depends on path)
```

```json
[
  { "op": "replace", "path": "/datasetIds", "value": ["ds1", "ds2"] },
  { "op": "replace", "path": "/keyContacts", "value": [{ "userId": "u1", "role": "steward" }] }
]
```

### Data products — lifecycle actions

```
POST /api/data-governance/data-products/{id}/actions/activate     body: { "name": string, "spaceId": string }  -> managed space only; returns activated:true + trustScore
POST /api/data-governance/data-products/{id}/actions/deactivate   (deactivate)
POST /api/data-governance/data-products/{id}/actions/move          body: { "spaceId": string REQUIRED }  -> needs delete perm in source space + create perm in target
POST /api/data-governance/data-products/{id}/actions/compute-datasets-data-quality
GET  /api/data-governance/data-products/{id}/actions/export-documentation   -> Markdown
DELETE /api/data-governance/data-products/{id}
```

### Glossary term — POST (create)

`POST /api/v1/glossaries/{id}/terms` (Content-Type `application/json`, Tier 2). Writable fields:

```
name                string    REQUIRED
description         string    optional
abbreviation        string    optional
tags                string[]  optional
categories          string[]  optional  category IDs the term belongs to
stewards            string[]  optional  user UIDs of the term's data stewards
relatesTo           object[]  optional  term-to-term relations: [{ type <enum>, termId string }]
linksTo             object[]  optional  term-to-resource links (see relationships section)
relatedInformation  string    optional  rich-text field stored as a JSON string (Slate-style node array)
```

`status` is NOT set at create (server assigns `status.type = "draft"`); `revision`, `id`, `glossaryId`, `createdAt/By`, `updatedAt/By`, and `referredRelations` (inbound relations) are server-managed in the response. Example body:

```json
{
  "name": "Earnings Before Interest and Tax (EBIT)",
  "abbreviation": "EBIT",
  "description": "Profit excluding interest and tax.",
  "tags": ["Finance", "Accounting"],
  "categories": ["123e4567-e89b-12d3-a456-426614174000"],
  "stewards": ["6305e8691a1d504df06e2ab9"],
  "relatesTo": [{ "type": "isA", "termId": "123e4567-e89b-12d3-a456-426614174000" }],
  "linksTo": [{ "type": "definition", "resourceType": "app", "resourceId": "<appId>" }],
  "relatedInformation": "[{\"type\":\"paragraph\",\"children\":[{\"text\":\"...\"}]}]"
}
```

### Glossary term — PATCH / PUT (update) semantics

Both `PATCH` and `PUT /api/v1/glossaries/{id}/terms/{termId}` exist. By the documented pattern shared with glossary and category PATCH: `PATCH` takes a JSON Patch array where `op` is `"replace"`, `path` is a JSON Pointer, and `value` is a string or number, and it accepts the optional `if-match` ETag header; a malformed body returns `400 "Payload could not be parsed to a JSON Patch"`; success is `204`. `PUT` is a full-resource replace. Governance rule: once a term's status is `verified`, only a steward can modify it. The precise per-field JSON Pointer enum for term PATCH was not captured from the (truncated) reference page — needs tenant testing to confirm which paths (e.g. `/name`, `/description`, `/categories`, `/tags`, `/stewards`, `/relatesTo`) are individually patchable.

### Glossary term — change-status action

`POST /api/v1/glossaries/{id}/terms/{termId}/actions/change-status`. Status enum (from the term `status.type`) is `draft`, `verified`, `deprecated`. Only a steward can verify a term. The exact request body key was not captured from the reference (likely `{ "status": "verified" }` or `{ "type": "verified" }`) — Not documented in captured pages / needs tenant testing.

### Server-side validation rules confirmed

- Glossary creation: only a steward can create a glossary.
- Term verification/modification: only a steward can verify; verified terms are steward-only to modify.
- Term relations (`relatesTo`) require `type` (closed enum) and `termId`.
- Term links to a subresource require all three of `subResourceType`, `subResourceId`, `subResourceName` together.
- Data-product `keyContacts`: one entry per user; `apiConsumableDatasetIds` subset of `datasetIds`; array sizes capped at 100 for `datasetIds`/`glossaryIds`.

## 3. Identity durability

Key findings on `qri` / `secureQri`:

- The QRI (Qlik Resource Identifier) is a structured, platform-wide identifier encoding resource type/platform and a canonical path. Examples: app `qri:app:sense://7fc4d85c-...`; dataset `qri:qdf:user://<hash>#<hash>`; data product `qri:data-product://<id>`.
- Data products return `qri` described as "uniquely identifying the data product across the platform." The `id` is a GUID assigned at creation and is the value embedded inside the data-product QRI, so for data products `qri` and `id` are effectively equivalent join keys.
- For datasets, the durable identifier is `secureQri`, exposed under the `resourceAttributes` object of the Items API (`GET /api/v1/items/{itemId}`). Qlik documents that the legacy `qri` field will be deprecated after migration to `secureQri`, so `secureQri` is the forward-looking key for datasets.
- `id` / `resourceId`: `id` is the item/resource primary key (GUID/OID); `resourceId` + `resourceType` identify the underlying resource an item points to. These are stable per-tenant identifiers but are tenant-scoped and not globally portable.

Cross-space / cross-tenant behavior (important, and partly under-documented):
- QRIs are tenant-and-platform scoped identifiers; they are stable and unique within a tenant. There is no documentation stating a QRI stays identical when a resource is copied to a different tenant (cross-tenant migration generally mints new IDs). Not documented / needs tenant testing for exact cross-tenant preservation.
- Preservation on move between spaces: data products keep the same `id`/`qri` across a `.../actions/move` (move changes `spaceId` only, not the identifier). This is strongly implied by the move semantics (it patches the space, not the id) but is Not explicitly documented / recommend tenant testing.

Recommendation for the universal model: use `secureQri` as the long-term join key for datasets, the data-product `qri` (or its `id`) for data products, and the glossary term `id` (UUID) for terms. Store the tenant id alongside every key so the join is unambiguous across tenants, since QRIs are not guaranteed globally unique across tenants.

## 4. Relationship representation

### Term-to-term relations

Written inline on the term via `relatesTo` (create/PUT) as an array of `{ type, termId }`. `type` enum: `isA`, `hasA`, `seeAlso`, `synonym`, `antonym`, `classifies`, `other`, `replaces`, `replacedBy`, `hasSubtype`, `preferredTerm`, `seeInstead`, `defines`, `definedBy`. Inbound relations (where this term is the target) are returned read-only as `referredRelations` with the same shape.

### Term-to-resource links

Managed as external relations independent of term status via the links sub-resource:

```
GET  /api/v1/glossaries/{id}/terms/{termId}/links
POST /api/v1/glossaries/{id}/terms/{termId}/links
```

Links can also be supplied inline on create as `linksTo`. Link object writable shape:

```
type             string   "definition" | "related"   (definition = term formally defines the resource)
resourceType     string   REQUIRED  "app" | "dataset"
resourceId       string   REQUIRED  target resource id (OID/UUID)
subResourceType  string   optional  "master_dimension" | "master_measure" | "field"
subResourceId    string   optional  (required if any subResource* is set)
subResourceName  string   optional  (required if any subResource* is set)
```

Link creation depends on permissions on both the term and the target resource; links can be created for a term in any status. Read responses add server fields (`id`, `openUrl`, `resourceSpaceId`, `createdAt/By`, `subResourceInvalid` when the referenced subresource no longer exists). The exact POST /links request body vs. inline `linksTo` shape overlap was not fully captured — Not fully documented / needs tenant testing, but fields mirror the `linksTo` object above.

### Data product associations

Represented as arrays of IDs directly on the data-product object and written via PATCH `replace` on the corresponding path:

```
datasetIds[]               data-product -> dataset membership (full-replace via /datasetIds)
apiConsumableDatasetIds[]  subset of datasetIds exposed over OData APIs (/apiConsumableDatasetIds)
glossaryIds[]              data-product -> glossary association (/glossaryIds)  (glossary-level, not term-level)
keyContacts[]              data-product -> user (owner/steward), via /keyContacts
```

Note the data-product↔glossary link is at the glossary level (`glossaryIds`), not to individual terms; term-level linkage to a resource is expressed from the term side via `linksTo`/links.

## 5. Write permissions / scopes

Auth model recap: calls authenticate with an API key (`Authorization: Bearer <key>`) or an OAuth2 access token (machine-to-machine client credentials, or authorization-code for interactive). Qlik OAuth uses coarse scopes such as `user_default` (acts as the user) and `admin_classic`; there is no documented fine-grained per-endpoint OAuth scope string for data-products or glossaries. Effective authorization for these governance APIs is therefore driven by the account's assigned roles and space roles, not by granular OAuth scopes. A service account is a normal user identity holding the required roles.

Confirmed permission requirements:

```
Data products:
  create (POST)            create permission in the target space
  update (PATCH)           write/manage permission on the product's space
  move (actions/move)      delete permission in source space + create permission in target space
  activate                 managed space only; activate as a governed/steward operation
  consume (read activated) "Can consume data product" permission + at least View role in the managed space

Glossaries / terms:
  create glossary          steward role (only a steward can create a glossary)
  verify a term            steward role (only a steward can verify)
  modify a verified term   steward role only
  create term / links      write permission on the glossary (+ permission on the linked resource for links)
```

The help.qlik.com "permissions, scopes and capacity model" page is the authoritative source for the exact professional/analyzer entitlement and role matrix; the precise named role that grants glossary steward and data-product management (beyond "steward" and space create/manage) should be confirmed on the target tenant. Not fully enumerated here / needs tenant testing for the exact custom-role permission strings.

## Verdict for the universal model

Confirmed safe to two-way sync:
- Data products: full CRUD is available with a well-defined create body and a closed-enum JSON Patch update (8 fields, full-replace arrays), plus activate/deactivate/move actions and a field-level `changelogs` feed. `qri`/`id`, `updatedAt`, and `pendingChangesCount` give reliable identity and change tracking.
- Glossary terms: create/update/delete, term-to-term relations (`relatesTo`), term-to-resource links, a status lifecycle (draft/verified/deprecated, steward-gated), a `revision` counter, `updatedAt`, and a `revisions` history endpoint.
- Optimistic concurrency: available for glossary/category/term writes via `if-match` ETags.
- Identity join keys: dataset `secureQri` (forward-looking), data-product `qri`/`id`, term UUID `id` — all stable within a tenant.

Remaining uncertain / needs tenant testing:
- No webhook or audit events for items, datasets, data products, or glossaries/terms — change detection must be poll-based (Items `updatedAt`, data-product `changelogs`, term `revisions`). This is the biggest architectural constraint and is confirmed, not merely unknown.
- Exact glossary-term PATCH path enum, the `change-status` request body key, the POST `/links` body vs inline `linksTo`, and the `revisions` response envelope were not fully captured from the (truncated) reference and should be verified against a live tenant.
- Whether Data Products PATCH honors ETags/`if-match` is undocumented.
- Cross-tenant QRI preservation and exact identifier stability on space-move are not explicitly documented; store `tenantId` with every key and verify move behavior on a tenant.

# Citations

- https://qlik.dev/manage/data-governance/create-data-product/ — Data Products create/activate/move/changelog tutorial with concrete request/response bodies.
- https://qlik.dev/apis/rest/data-governance/data-products/ — Data Products REST reference (POST/PATCH/move/activate field schemas, JSON Patch path enum).
- https://qlik.dev/apis/rest/glossaries/ — Glossaries REST reference (term create schema, relatesTo/linksTo enums, status object, if-match ETag, terms/links/revisions/change-status endpoints).
- https://qlik.dev/apis/rest/items/ — Items REST reference (updatedAt RFC3339, resourceType enum including dataproduct/glossary/dataset, resourceAttributes).
- https://qlik.dev/manage/data-governance/get-started-lineage/ — QRI and secureQri formats, secureQri under resourceAttributes, qri deprecation note.
- https://qlik.dev/apis/event/ — Qlik Cloud system events overview: delivery via webhooks and Audits API, CloudEvents migration, full list of event categories (no catalog metadata resources).
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/Admin/mc-administer-webhooks-supported-events.htm — Supported webhook event types (apps, users, automations, DI tasks, api-keys only).
- https://qlik.dev/manage/access-control/scopes/ — OAuth scopes model (user_default, admin_classic).
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/Admin/permissions-scopes-capacity-model.htm — Permissions, scopes and capacity model (role/entitlement matrix for data-product and glossary management).
- https://qlik.dev/apis/rest/audits/ — Audits API (poll-based retrieval of the same event payloads delivered by webhooks).
