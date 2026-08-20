---
type: "Research Output"
title: "Qlik Cloud Catalog & Data Products — API Reference"
description: "Detailed reference of Qlik Cloud catalog assets, data products, and the read/write API surface for metadata synchronization."
tags: ["research", "RS-02", "qlik", "qlik-cloud", "api"]
timestamp: "2026-08-06T08:00:00Z"
status: "draft"
---

# Qlik Cloud Catalog & Data Products — API Reference

This reference describes how Qlik Cloud (including the Qlik Talend Data Integration / Data
Fabric catalog surface) manages catalog metadata and data products, and the concrete REST API
surface available for reading and writing that metadata. It is written for the QLabs Catalog Sync
bridge, so each section flags what is synchronizable (readable AND writable) versus read-only, and
which fields serve as stable identity/matching keys.

All facts below are drawn from the official Qlik Developer Portal (qlik.dev) and Qlik Cloud Help
(help.qlik.com); see the Citations section.

---

## 1. Catalog assets — what Qlik Cloud maintains

Qlik Cloud does not have a single monolithic "catalog" object. The catalog is the aggregate of
several first-class resources, each with its own REST API, tied together by the **Items** service
(a unified index of everything a user can see) and by **Spaces** (governance containers).

### 1.1 Items — the unified catalog index

**Items** is the core catalog registry. It lists the resources a user has access to across the
tenant (apps, datasets, data assets, data products, glossaries, automations, notes, ML
experiments, knowledge bases, and more). Every catalog object surfaces as an item with a common
metadata envelope, which makes Items the natural entry point for discovery and for cross-type
matching.

Key item fields (from `GET /api/v1/items/{itemId}`):

- `id` — the item's unique identifier (the item-service key, distinct from the underlying
  resource's own id).
- `resourceId` — the id of the underlying resource (e.g. the app id or dataset id).
- `resourceType` — the case-sensitive type discriminator. Observed enum includes: `app`,
  `qlikview`, `qvapp`, `genericlink`, `sharingservicetask`, `note`, `dataasset`, `dataset`,
  `automation`, `automl-experiment`, `automl-deployment`, `assistant`, `dataproduct`,
  `dataqualityrule`, `glossary`, `knowledgebase`, `script`, `semantictype`, `page`.
- `resourceSubType`, `resourceLink`, `resourceUpdatedAt`.
- `name`, `description`, `spaceId`, `ownerId`, `thumbnailId`.
- `meta` — computed metadata, including `tags` (each with an `id` and `name`) and collection
  membership. In the Items model, "tags" and "collections" are represented by the same underlying
  tag/collection ids.

Notably, **`dataproduct` and `glossary` are item resource types**, so data products and glossaries
appear in the same catalog index as datasets and apps.

### 1.2 Data sets and data assets

- **Data assets** (`/api/v1/data-assets`) are containers; a **data set** is a member of a data
  asset. Datasets are the catalog's tabular metadata objects (files, tables, QVDs, and generated
  datasets for QIX data files).
- A dataset carries: `id`, `qri` (Qlik Resource Identifier — the cross-platform resource key,
  being migrated to `secureQri`), `name`, `type`, `tags` (unique string array), and a `schema`
  object describing fields/columns (with optional additional schemas for multi-sheet/multi-table
  files). Data quality/profiling is available separately via `GET
  /api/v1/data-sets/{data-set-id}/profiles`.
- **Data Products for Analytics** lets users turn existing QVDs and datasets into governed,
  discoverable, reusable data products with quality, context, and ownership inside the analytics
  environment.

### 1.3 Spaces — governance containers

**Spaces** (`/api/v1/spaces`) are logical containers that control access via space roles. Every
catalog object (dataset, app, glossary, data product) lives in a space referenced by `spaceId`.
Space types are enumerated at `GET /api/v1/spaces/types` (shared, managed, data, personal, etc.).
Membership/permissions are managed through the `assignments` and `shares` sub-resources.

### 1.4 Tags and collections

Qlik represents user-facing "tags" through the **Collections** service (`/api/v1/collections`).
A tagged collection is effectively a tag; item `meta.tags` entries carry the collection id and
name. Datasets and glossaries also carry a lightweight free-text `tags` string array directly on
the resource. Collections retrieval excludes the user's favorites collection (fetched separately at
`/api/v1/collections/favorites`).

### 1.5 Business glossary

**Glossaries** (`/api/v1/glossaries`) implement the business glossary: an agreed set of business
terms defining the meaning of data. The model is glossary -> categories -> terms:

- **Glossary**: `name`, `description`, `tags`, `spaceId`, `overview` (rich text stored as a JSON
  string), and a `termTemplate`. Only a steward can create a glossary.
- **Category**: grouping of terms within a glossary.
- **Term**: `name`, `description`, `abbreviation`, `tags`, `categories` (ids), `stewards` (user
  ids), `status` (`draft` | `verified` | `deprecated`), `relatedInformation` (rich text JSON),
  `relatesTo` (term-to-term relationships such as `synonym`, `isA`, `replaces`, `definedBy`), and
  `linksTo` (links to catalog resources — an `app` or `dataset`, optionally down to a subresource:
  `master_dimension`, `master_measure`, or `field`, with `subResourceId`/`subResourceName`/
  `subResourceType` supplied together). Terms are versioned (`revisions`) and status changes have a
  dedicated action endpoint.

### 1.6 Lineage, data quality, and trust

- **Lineage graphs** (`/api/v1/lineage-graphs`) expose upstream/downstream lineage. Data product
  lineage and impact analysis let consumers trace origins and transformations.
- **Data qualities** (`/api/v1/data-qualities` and the data-governance data-qualities API) and
  **Trust scores** (`/api/data-governance/trust-scores`) provide computed quality/validity/
  completeness metrics. These are largely computed, read-oriented signals.

### 1.7 Custom properties / classifications

Qlik Cloud's catalog does not expose an arbitrary user-defined "custom properties" bag the way
Qlik Sense on-premises (QMC custom properties) did. The synchronizable free-form metadata is
effectively: `description`, `tags`, glossary terms (and their links), `ownerId`, and space
placement. Semantic types (`semantictype` item type) provide field-level classification.

---

## 2. Data products in the Qlik ecosystem

A **data product** is a curated package that groups related datasets into a single governed,
discoverable offering, organized by business domain. Data products are created inside spaces and
made available through the **Data Marketplace** in Qlik Talend Data Integration, where consumers
can shop for them, preview quality/profiles/descriptions/tags, and load them directly into Qlik
Sense apps. Data Product capabilities exist in Qlik Talend Cloud and Qlik Cloud Analytics
(Premium/Enterprise, Qlik Sense Enterprise SaaS).

Relationship to the rest of the catalog:

- A data product **references datasets** by id (`datasetIds`), so it is a governance/packaging
  layer on top of catalog datasets, not a copy of the data.
- It can **link glossary terms** via `glossaryIds`, tying business definitions to the product.
- It appears in the unified Items index as `resourceType: "dataproduct"` and lives in a `spaceId`.
- It carries first-class governance metadata: `ownerId`, `keyContacts` (users with roles),
  `tags`, `description`, `readMe` (Markdown documentation), computed `quality`
  (validity/completeness), and a lifecycle (`draft`/created -> activated -> deactivated).
- `apiConsumableDatasetIds` marks which member datasets are exposed for API consumption.

The lifecycle is explicitly three-phase: (1) create an empty product, (2) add datasets and set
metadata, (3) activate/publish to make it discoverable and consumable.

---

## 3. API mechanics

### 3.1 API surfaces

- **Tenant REST APIs** under `https://<tenant>.<region>.qlikcloud.com/api/v1/...` for the bulk of
  catalog resources: `items`, `collections`, `data-sets`, `data-assets`, `glossaries`, `spaces`,
  `lineage-graphs`, `data-qualities`, `users`, `groups`, etc.
- **Data governance APIs** under `.../api/data-governance/...` — notably the newer **Data Products
  API** (`/api/data-governance/data-products`), plus `trust-scores` and data-quality governance
  endpoints. Note this base path is `/api/data-governance/`, NOT `/api/v1/`.
- **qlik-cli** — the official CLI (e.g. `qlik glossary ls`, `qlik item ls`) wraps the same REST
  surface and has built-in rate-limit handling.
- **SDKs / toolkits** — `@qlik/api` (TypeScript, high-level typed wrappers such as
  `qlik.glossaries.getGlossaries()` and `spaces.getSpaces()`), plus platform-operations connectors
  in Qlik Automate.

### 3.2 Authentication

Qlik Cloud supports three main credential types for programmatic access. All are presented as a
bearer token in the `Authorization: Bearer <token>` header.

- **API keys** — long-lived JWT bearer tokens generated per user in the tenant (managed via
  `/api/v1/api-keys`). In `@qlik/api` this is `authType: 'apikey'`. Simplest for server-side jobs;
  act as the issuing user.
- **OAuth 2.0 M2M (machine-to-machine) clients** — the recommended method for backend
  integrations. Create an OAuth client (Cloud console or `/api/v1/oauth-clients`), then use the
  **client credentials** grant: POST the `client_id`/`client_secret` (and optional space-delimited
  `scope`) to the tenant's `/oauth/token` endpoint to receive a short-lived access token. On first
  token issuance a non-interactive bot user is created on the tenant. Scopes control access; with
  no scope a client can access nothing (it may inherit the client's configured scopes).
- **JWT (IdP)** — JWT authorization via a configured identity provider for interactive/embedded
  user contexts.

Tenant base URL structure: `https://<tenant>.<region>.qlikcloud.com`. Region examples include
`eu`, `us`, `ap`. REST calls hang off `/api/v1/...`; the OAuth token endpoint is `/oauth/token`.

Token request example (client credentials / M2M):

```
curl -X POST "https://<tenant>.<region>.qlikcloud.com/oauth/token" \
  -H "Content-Type: application/json" \
  -d '{
        "grant_type": "client_credentials",
        "client_id": "<client_id>",
        "client_secret": "<client_secret>",
        "scope": "user_default"
      }'
```

Authenticated call example:

```
curl "https://<tenant>.<region>.qlikcloud.com/api/v1/spaces" \
  -H "Authorization: Bearer <access_token>"
```

### 3.3 Pagination, filtering, versioning

- **Pagination** is cursor-based: list responses return a `data` array plus a `links` object with
  `next`/`prev` hrefs; a `limit` query parameter controls page size. Many list endpoints support
  `sort` and field-level `filter` expressions (fields marked *Filterable* in the spec, e.g. term
  `status`).
- **Versioning** is by URL path segment (`/api/v1/...`). The data-governance family uses its own
  base path (`/api/data-governance/...`) rather than a `/v1` segment.

### 3.4 Rate limits

Per-tier, per-user, per-tenant, evaluated over a 5-minute window to allow bursts:

- **Tier 1** — 1,000 req/min: most `GET` requests (e.g. list/get items, get glossary).
- **Tier 2** — 100 req/min: create/update/delete endpoints (POST/PUT/PATCH/DELETE on datasets,
  glossaries, terms, categories, data products).
- **Special** — varies, documented per endpoint (e.g. reloads).
- Tenant aggregate limit = `user_rate_limit * number_of_users * 0.5`.
- On breach: `HTTP 429 Too Many Requests` with a `Retry-After` header (seconds). Same-tier calls
  are blocked during the wait; other tiers are unaffected. qlik-cli and Automate connectors handle
  this automatically.

### 3.5 CRUD reference by resource

#### Items (catalog metadata envelope)

```
GET    /api/v1/items                      # list/discover items (filter by resourceType, spaceId, name)
GET    /api/v1/items/{itemId}             # read one item (id, resourceId, resourceType, meta.tags, ownerId, spaceId)
PUT    /api/v1/items/{itemId}             # update item metadata
DELETE /api/v1/items/{itemId}
GET    /api/v1/items/{itemId}/collections # tags/collections the item belongs to
```

Update body (PUT) — writable fields: `name`, `description`, `spaceId`, `resourceId`,
`resourceLink`, `resourceType` (required), `resourceSubType`, `thumbnailId`, `resourceUpdatedAt`.
Omitted fields are ignored; send a field's zero value to unset it.

```
PUT /api/v1/items/{itemId}
{
  "name": "Sales — Curated",
  "description": "Curated sales dataset for finance",
  "resourceType": "dataset",
  "resourceId": "6410...abcd",
  "spaceId": "5f2a...9911"
}
```

#### Data sets

```
POST   /api/v1/data-sets                  # save new dataset (Tier 2)
GET    /api/v1/data-sets/{data-set-id}    # read
PATCH  /api/v1/data-sets/{data-set-id}    # partial update
PUT    /api/v1/data-sets/{data-set-id}    # full update
DELETE /api/v1/data-sets                  # delete (by body/query)
GET    /api/v1/data-sets/{data-set-id}/profiles
```

Writable dataset fields include `name`, `tags`, `type`, `schema`, `qri` (`id` must be null for
new resources; required on update).

```
POST /api/v1/data-sets
{
  "qri": "qdf:<store-type>:<tenant-guid>:<space-guid>:<path-to-file>",
  "name": "orders_2026",
  "type": "qix-df",
  "tags": ["finance", "orders"]
}
```

#### Glossaries, categories, and terms

```
GET/POST                 /api/v1/glossaries
GET/PATCH/PUT/DELETE     /api/v1/glossaries/{id}
GET/POST                 /api/v1/glossaries/{id}/categories
GET/PATCH/PUT/DELETE     /api/v1/glossaries/{id}/categories/{categoryId}
GET/POST                 /api/v1/glossaries/{id}/terms
GET/PATCH/PUT/DELETE     /api/v1/glossaries/{id}/terms/{termId}
POST                     /api/v1/glossaries/{id}/terms/{termId}/actions/change-status
GET/POST                 /api/v1/glossaries/{id}/terms/{termId}/links
GET                      /api/v1/glossaries/{id}/terms/{termId}/revisions
GET                      /api/v1/glossaries/{id}/actions/export
POST                     /api/v1/glossaries/actions/import
```

Create a glossary (writable: `name` required, `description`, `tags`, `spaceId`, `overview`,
`termTemplate`):

```
POST /api/v1/glossaries
{
  "name": "Finance Glossary",
  "description": "Agreed finance terminology",
  "spaceId": "5f2a...9911",
  "tags": ["finance"]
}
```

Create a term (writable: `name` required, `description`, `abbreviation`, `tags`, `categories`,
`stewards`, `relatesTo`, `linksTo`, `relatedInformation`):

```
POST /api/v1/glossaries/{id}/terms
{
  "name": "Net Revenue",
  "description": "Revenue after returns and allowances",
  "abbreviation": "NR",
  "tags": ["kpi"],
  "categories": ["<categoryId>"],
  "stewards": ["<userId>"],
  "linksTo": [
    {
      "type": "definition",
      "resourceType": "dataset",
      "resourceId": "<datasetId>",
      "subResourceType": "field",
      "subResourceId": "net_rev",
      "subResourceName": "net_rev"
    }
  ]
}
```

Term status is changed via its own action (values `draft` | `verified` | `deprecated`):

```
POST /api/v1/glossaries/{id}/terms/{termId}/actions/change-status
{ "status": "verified" }
```

Bulk import/export of an entire glossary (terms, categories, links) is supported via
`/actions/import` and `/actions/export` — useful for full-graph sync.

#### Spaces

```
GET/POST                 /api/v1/spaces
GET/PATCH/PUT/DELETE     /api/v1/spaces/{spaceId}
GET/POST/PUT/DELETE      /api/v1/spaces/{spaceId}/assignments[/{assignmentId}]
GET/POST/PATCH/DELETE    /api/v1/spaces/{spaceId}/shares[/{shareId}]
GET                      /api/v1/spaces/types
```

Writable space fields include `name`, `description`, `type`, and (via assignments) role
membership.

#### Data products (data-governance API)

```
POST   /api/data-governance/data-products                                   # create (empty or with datasets)
GET    /api/data-governance/data-products/{dataProductId}                    # read full details
PATCH  /api/data-governance/data-products/{dataProductId}                    # update metadata + datasets
POST   /api/data-governance/data-products/{dataProductId}/actions/move       # move to another space
POST   /api/data-governance/data-products/{dataProductId}/actions/activate   # publish/discoverable
POST   /api/data-governance/data-products/{dataProductId}/actions/deactivate
POST   /api/data-governance/data-products/{dataProductId}/actions/compute-datasets-data-quality
GET    /api/data-governance/data-products/{dataProductId}/actions/export-documentation
GET    /api/data-governance/data-products/{dataProductId}/changelogs
POST   /api/data-governance/data-products/actions/generate-provider-url
```

Create a data product — writable fields: `name` (required), `description`, `tags`, `readMe`
(Markdown, up to 100k chars), `spaceId`, `datasetIds` (up to 100, unique), `glossaryIds` (up to
100 UUIDv4), `keyContacts` (`userId` required + `role`), `apiConsumableDatasetIds` (subset of
`datasetIds`):

```
POST /api/data-governance/data-products
{
  "name": "Customer 360",
  "description": "Governed customer master data product",
  "spaceId": "5f2a...9911",
  "tags": ["customer", "gold"],
  "readMe": "# Customer 360\nDaily-refreshed customer master...",
  "datasetIds": ["<datasetId1>", "<datasetId2>"],
  "glossaryIds": ["<glossaryId>"],
  "keyContacts": [{ "userId": "<userId>", "role": "Data Owner" }],
  "apiConsumableDatasetIds": ["<datasetId1>"]
}
```

The 201 response returns identity/governance fields: `id`, `qri` (cross-platform key), `mainId`,
`name`, `tags`, `ownerId` (required), computed `quality` (`validity`, `completeness`), `spaceId`,
and `readMe`. Update metadata and dataset membership with `PATCH`; publish with the `activate`
action.

---

## 4. Relevance to sync — writable vs read-only, and matching keys

### 4.1 Identity / matching keys

- **Item id** (`items.id`) — the item-service key; stable within the tenant. Use for
  cross-type discovery.
- **`resourceId` + `resourceType`** — join key from an item to its underlying resource (dataset,
  app, data product, glossary).
- **Dataset id** — key for dataset CRUD and for `datasetIds`/`apiConsumableDatasetIds` on a data
  product.
- **`qri` / `secureQri` (Qlik Resource Identifier)** — the durable cross-platform resource
  identifier on datasets and data products; the best candidate for stable external matching, though
  `qri` is being migrated to `secureQri`.
- **glossary id / categoryId / termId** — UUIDs keying the glossary graph.
- **spaceId, ownerId, userId** — governance keys (users/spaces).

For a two-way bridge, maintain a mapping table keyed on the external system's id <-> Qlik
`qri`/`id`, because Qlik ids are tenant-scoped and not portable across tenants.

### 4.2 Synchronizable (read AND write via API)

- **Descriptions** — writable on items, datasets, glossaries/terms, and data products.
- **Tags** — writable on datasets, glossaries, terms, and data products (free-text arrays);
  item/collection tags via the Collections API.
- **Names** — writable on items, datasets, glossaries, terms, spaces, data products.
- **Owner / key contacts** — data product `ownerId` and `keyContacts` are settable (via
  create/patch/move); item `ownerId` is generally set by ownership-transfer flows.
- **Space placement** — writable (`spaceId` on create/update; `actions/move` for data products).
- **Glossary graph** — terms, categories, term status, term-to-resource links, and term-to-term
  relationships are all create/update/delete capable; whole-glossary import/export enables bulk
  round-trips.
- **Data product composition** — `datasetIds`, `glossaryIds`, `apiConsumableDatasetIds`, `readMe`
  documentation, and lifecycle (activate/deactivate) are all API-driven.

### 4.3 Read-only / computed (do not attempt to write)

- **Data quality / profiling / trust scores** — `quality` (validity, completeness), dataset
  `profiles`, and trust scores are computed by Qlik; they can be triggered for recomputation
  (data-product `compute-datasets-data-quality`) but not directly authored.
- **Lineage** — derived from load/transformation graphs; readable via lineage-graphs, not
  author-settable.
- **System timestamps and audit fields** — `resourceUpdatedAt`, term `status.updatedAt/updatedBy`,
  changelogs, and revisions are system-maintained.
- **`id` / `qri`** — server-assigned identity keys; read-only after creation.

### 4.4 Practical sync notes

- Respect Tier 2 (100/min) limits on all write paths; implement `Retry-After` back-off.
- Rich-text fields (glossary `overview`, term `relatedInformation`) are JSON-encoded rich text —
  round-trip them as opaque strings unless you can safely parse the format.
- Prefer the glossary import/export actions for bulk term synchronization, and PATCH for
  incremental data-product metadata updates.

---

## 5. Semantic layer

**Qlik Cloud has no dedicated, tenant- or catalog-level semantic layer product at this time.**
There is no standalone, catalog-scoped modeling surface where you define a governed set of
enterprise metrics, dimensions, and relationships once and have every consuming app, tool, or query
resolve against it (the pattern found in some competing platforms). What Qlik offers instead is a
set of *nearest equivalents* that carry semantic meaning, but each is scoped either to an individual
analytics app or to the governance/glossary layer — not to a shared, tenant-wide semantic model.
Treat any "semantic layer" expectation from an external catalog as something the bridge must
*approximate* by mapping onto these existing assets, not as a native Qlik object.

### 5.1 App-scoped logical model / business logic

**Business logic** (logical model + vocabulary) is Qlik's per-app semantic customization. It tells
**Insight Advisor** how to interpret an app's data model when generating analyses and answering
natural-language questions. A logical model organizes fields and master items into groups, and adds
packages, hierarchies (drill-down relationships), and behaviors (prefer/deny relationships and
required selections); vocabulary adds synonyms and custom/example analyses. The default logical
model is a star schema, and the model can be left at its automatically inferred default or
overridden with a custom one.

Key constraints for sync:

- **Scope is a single application**, not the tenant or catalog. Two apps over the same data each
  have their own business logic; there is no shared model they inherit from.
- **Governance**: only the app owner or space members with edit-data rights can change an app's
  logical model or vocabulary.
- **API access**: business logic lives inside the app (QIX/engine layer), not in the tenant REST
  catalog. The Insight Advisor APIs can enumerate an app's logical model — listing each field and
  master item and classifying it as dimension, measure, or other, and indicating whether the app
  uses the default or a custom logical model — but this is app-engine access, not a
  `/api/v1/...` catalog resource. There is no catalog-level endpoint that reads or writes business
  logic across apps.

### 5.2 Master items (measures, dimensions)

**Master items** — master measures and master dimensions (plus master visualizations) — are the
reusable, named calculations and categorizations stored in an app's library. They are the closest
thing to governed "metrics" and "dimensions" in Qlik, and a master measure can carry a definition,
label expression, colors, and description that make a calculation a recognized, reusable entity.

Key constraints for sync:

- **Scope is the app library**, again per-app rather than tenant-wide. A master measure defined in
  one app is not automatically available to another.
- **API access**: master items are read/written through the **QIX Engine API** (e.g. via
  `@qlik/api`/nebula-style engine sessions), not through the tenant REST catalog. You can list all
  master measure/dimension definitions in an app and evaluate them, but they are not first-class
  tenant catalog resources.
- **Catalog linkage**: the business glossary *can* reference a master item — a term's `linksTo`
  supports `subResourceType` values `master_dimension` and `master_measure` (with
  `subResourceId`/`subResourceName`) against an `app` resource. This is the one place where an
  app-scoped semantic definition is surfaced into the governance layer, and it is the natural hook
  for the bridge.

### 5.3 Business glossary — the governance-level semantic asset

The **business glossary** (Section 1.5) is the closest Qlik gets to a *catalog-level* semantic
asset: it is a tenant resource, lives in a space, has a full REST CRUD surface, and lets you define
agreed business terms, relationships (`synonym`, `isA`, `replaces`, `definedBy`), and links from
terms down to datasets, fields, and master items. It captures *meaning and definitions* but not
executable metric logic — a term documents what "Net Revenue" means and what it links to, but it
does not itself compute Net Revenue. It is the best target for semantic metadata in the bridge and,
unlike business logic and master items, it is fully addressable through `/api/v1/glossaries`.

### 5.4 Direction of travel (evolving — flagged)

Qlik's AI-oriented features — **Qlik Answers** (a knowledge-base/unstructured-data assistant) and
the **Qlik MCP server** (which exposes Qlik apps/data to AI agents and MCP clients) — are moving
toward richer, model-aware semantic access, and business logic is explicitly the layer that makes
apps interpretable to Insight Advisor's natural-language capabilities. These indicate an evolving
direction toward stronger semantic/AI-readiness features, **but as of this writing none of them
constitutes a standalone, catalog-level semantic layer**, and this section should be revisited as
Qlik's roadmap develops. Do not model the bridge around a tenant-wide semantic layer that does not
yet exist.

### 5.5 What this means for the sync bridge

- **No 1:1 target for an external "semantic layer".** If a source catalog exposes governed metrics
  and dimensions as a shared model, there is no equivalent single Qlik object to sync into.
- **Map semantics onto the glossary.** Treat the business glossary as the durable, API-addressable
  home for imported semantic definitions (terms, synonyms, relationships), and use term `linksTo`
  to bind them to datasets, fields, and — where applicable — app master items.
- **Treat app logical models and master items as read-mostly, app-local context.** They can be
  *read* (via engine/Insight Advisor APIs) to enrich or reconcile definitions, but they are not a
  tenant-level write target and cannot be kept in lockstep across many apps through the catalog
  REST surface.
- **Record the boundary explicitly** in the bridge's capability matrix so downstream consumers do
  not assume Qlik round-trips an enterprise semantic model.

---

# Citations

- https://qlik.dev/apis/rest/ — Qlik Cloud REST API index (catalog resource list: items,
  collections, data sets, glossaries, spaces, lineage graphs, data qualities, etc.).
- https://qlik.dev/apis/rest/items/ — Items REST API (unified catalog index; item fields,
  resourceType enum including `dataproduct` and `glossary`, PUT update body).
- https://qlik.dev/apis/rest/data-sets/ — Data sets REST API (dataset CRUD, `qri`, `schema`,
  `tags`, profiles endpoint).
- https://qlik.dev/apis/rest/glossaries/ — Glossaries REST API (glossary/category/term CRUD, term
  links, relationships, status, import/export).
- https://qlik.dev/apis/rest/spaces/ — Spaces REST API (space CRUD, assignments, shares, types).
- https://qlik.dev/apis/rest/collections/ — Collections REST API (tags/collections model).
- https://qlik.dev/apis/rest/data-governance/data-products/ — Data Products API reference
  (create/patch/activate/move, writable metadata fields, `qri`/`ownerId`/`quality` response).
- https://qlik.dev/changelog/196-api-data-products/ — Data Products API release note (endpoint
  overview and lifecycle).
- https://qlik.dev/manage/data-governance/create-data-product/ — Create and activate a data product
  tutorial.
- https://qlik.dev/apis/rest/rate-limiting/ — Rate limiting tiers (Tier 1/Tier 2), 429 handling,
  tenant aggregate limit, retry examples.
- https://qlik.dev/authenticate/oauth/getting-started-oauth-m2m/ — OAuth machine-to-machine
  (client credentials) setup, `/oauth/token`, scopes, bot user creation.
- https://qlik.dev/authenticate/oauth/ — OAuth overview and scopes model for Qlik Cloud.
- https://qlik.dev/toolkits/qlik-api/rest/ — @qlik/api toolkit (typed REST wrappers, apikey/host
  config).
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/DataIntegration/Marketplace/Data-products.htm — Working with data products (Qlik Cloud Help).
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/DataIntegration/Marketplace/Creating-data-products.htm — Creating data products (marketplace, datasets, quality, tags).
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/DataIntegration/Marketplace/Consuming-in-data-marketplace.htm — Consuming in the data marketplace.
- https://help.qlik.com/en-US/evaluation-guides/Content/data-integration/data-products.htm — Data Products evaluation guide (quality, lineage, analytics integration).
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/Admin/mc-create-oauth-client.htm — Creating and managing OAuth clients (Qlik Cloud Help).
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/BusinessLogic/business_logic.htm — Customizing logical models for Insight Advisor (business logic = logical model + vocabulary; app-scoped, owner/space-member managed).
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/BusinessLogic/business-logic-logical-models.htm — Building logical models for Insight Advisor (fields, master items, groups, packages, hierarchies, behaviors; default star schema).
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/Assets/work-with-master-items.htm — Reusing assets with master items (master measures/dimensions stored in the app library).
- https://qlik.dev/embed/gen-ai/explore-app-content-insight-advisor/ — Explore app content using the Insight Advisor APIs (enumerate an app's logical-model fields and master items; default vs custom model).
- https://qlik.dev/embed/nebula/customize/get-master-measures/ — Evaluate master measure data (list and evaluate app master measures via the engine API).
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/QlikAnswers/Qlik-Answers.htm — Qlik Answers (AI assistant; evolving AI/semantic direction, not a catalog semantic layer).
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/QlikMCP/Qlik-MCP-server.htm — Qlik MCP server (exposes apps/data to AI agents; evolving AI direction, not a catalog semantic layer).
