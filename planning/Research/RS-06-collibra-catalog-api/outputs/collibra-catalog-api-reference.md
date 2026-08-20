---
type: "Research Output"
title: "Collibra Catalog & Data Products — API Reference"
description: "Detailed reference of Collibra catalog assets, data products, and the read/write API surface for metadata synchronization."
tags: ["research", "RS-06", "collibra", "data-marketplace", "api"]
timestamp: "2026-08-06T08:00:00Z"
status: "draft"
---

# Collibra Catalog & Data Products — API Reference

This reference documents how Collibra (Collibra Platform / Data Intelligence Platform)
models catalog metadata and data products, and the API surface available to read and
write that metadata. It is written to support the QLabs Catalog Sync bridge, which
synchronizes data-product metadata two ways across catalogs. All endpoint paths, JSON
field names, and asset/attribute type identifiers below are drawn from official Collibra
developer and product documentation (see Citations). Verbatim manifest and example
payloads are paraphrased; UUIDs shown are the out-of-the-box identifiers published by
Collibra.

---

## 1. Catalog assets and the operating model

### 1.1 The meta-model (operating model)

Collibra organizes all metadata in a governed graph called the **operating model**. The
structural layers, from container to characteristic, are:

- **Community** — the top-level organizational grouping. A *root* community has no parent;
  communities can nest into sub-communities. Communities hold domains.
- **Domain** — a logical grouping of assets inside a community. Every domain has a
  **domain type** (for example *Glossary*, *Policy*, *Data Product Catalog*, *Schema*,
  *Physical Data Dictionary*). A domain type constrains which asset types can live there.
- **Asset** — the core metadata object (a business term, a table, a column, a data product,
  and so on). Every asset has an **asset type**, belongs to exactly one domain, has a
  **name** (full name is `Community > Domain > Asset name`), and carries a set of
  characteristics.
- **Characteristics** of an asset:
  - **Attributes** — typed key/value characteristics such as *Definition*, *Description*,
    *Job Title*, *Data product category*. Each attribute has an **attribute type** and a
    value whose kind depends on the type (string, boolean, number, date, single- or
    multi-select).
  - **Relations** — typed, directed links between two assets, defined by a **relation
    type** with a source role and target role (for example "Table *is part of* Schema").
    Collibra also supports **complex relations**, which are many-to-many relations that
    can themselves carry attributes.
  - **Tags** — free-form labels attached to an asset.
  - **Responsibilities** — assignments of a **role** (for example *Owner*, *Steward*) to a
    user or user group on a resource; this is how ownership/stewardship is expressed.
  - **Status** — the workflow/lifecycle state of the asset (for example *Candidate*,
    *Accepted*, *Approved*), drawn from a configurable set of statuses.
  - **Comments** and **attachments**.

Everything above is metadata-configurable: asset types, attribute types, relation types,
domain types, statuses, and roles are all defined in the operating model and identified by
UUID. Out-of-the-box types ship with fixed, well-known UUIDs (examples below), while
customer-created types get generated UUIDs.

### 1.2 Well-known out-of-the-box type IDs

These stable UUIDs are used as `typeId` values when creating resources through the API:

| Type | Kind | UUID |
| --- | --- | --- |
| Glossary | Domain type | `00000000-0000-0000-0000-000000010001` |
| Policy | Domain type | `00000000-0000-0000-0000-000000030013` |
| Business Term | Asset type | `00000000-0000-0000-0000-000000011001` |
| Definition | Attribute type | `00000000-0000-0000-0000-000000000202` |

Customer environments expose all type IDs through the operating-model endpoints
(`/rest/2.0/assetTypes`, `/rest/2.0/attributeTypes`, `/rest/2.0/relationTypes`,
`/rest/2.0/domainTypes`, `/rest/2.0/statuses`), which a sync engine should read once and
cache as a type-ID map.

### 1.3 Catalog-specific assets

Collibra Data Catalog layers physical/technical assets onto the same operating model:

- **Registered data source (System / Database)** — a data source registered through Edge
  (or legacy jobserver) that Collibra ingests, profiles, and classifies.
- **Schema**, **Table** (and **View**), **Column** (**Data Element**) — the ingested
  technical metadata, linked by relation types such as *Schema contains Table* and *Table
  contains Column*. These are the physical assets that data products ultimately expose.
- Profiling statistics, sample data, and **data classification** results attach to these
  assets (managed by the Catalog Data Classification, Catalog Database Registration, and
  Catalog Sampling APIs).

Catalog ingestion largely runs through **Edge** (connectors) and the **Catalog API**
(base path `/rest/catalog/1.0`), which is the recommended way to push metadata for
sources Collibra does not natively support.

### 1.4 Data Governance assets

On the governance side, the same model hosts **Business Terms**, **Data Concepts**,
**Data Categories**, **Policies**, **Standards**, **Rules**, **Data Quality Rules/Metrics**,
and **Assessments**. These link to physical assets (for example a Column *is classified by*
a Business Term) and to data products (a Data Product *relates to* a Business Term).

---

## 2. Data products in Collibra

### 2.1 What a data product is

In Collibra a **data product** is a reusable package that provides data to answer a
business question or solve a business problem, bundling not just the data but the context,
controls, and access information. It has four conceptual components: **Context** (why it
exists, who owns it, quality/privacy), **Data** (the tables, views, or business assets such
as a report or model), **Controls** (related policies and quality checks), and **Access**
(how to reach the data and the governing policies).

Concretely, a data product is an **asset with asset type `Data Product`**. Data products
are hosted in a domain of type **Data Product Catalog**, alongside their related **Data
Product Port** and **Data Contract** assets.

### 2.2 Data product operating model

Asset types dedicated to data products:

- **Data Product** — the product itself.
- **Data Product Port** — the interface through which the product interacts with the
  ecosystem. A port linked to a Data Product via *exposes data as* is an **output port**;
  linked via *consumes data through* it is an **input port**. Advanced governance can use
  child types **Data Product Output Port** and **Data Product Input Port**.
- **Data Contract** — the formal commitment (structure, format, service level, quality,
  terms of use) a product owner makes to consumers. It is backed by a **Data Contract
  Manifest** (a YAML file, ideally conforming to the Open Data Contract Standard / ODCS),
  which can have multiple versions and carries a **Manifest ID** used to match a manifest to
  its Data Contract asset.

Asset type **groups** used for ports:

- **Data Product Port Asset** — the assets a port is implemented by (by default only
  **Tables**; Data Sets and Data Elements are supported but not the recommended exposure).
- **Data Product Input** — the physical input for advanced input-port use cases.

Key **relation types** (data-product operating model):

- Data Product *exposes data as / is output port for* Data Product Port
- Data Product Port *is input port for / consumes data through* Data Product
- Data Product Port *is implemented as / implements* Data Product Port Asset (this is how a
  port binds to the underlying **Table/Column** assets)
- Data Contract *governs functioning of / should operate according to* Data Product Port
- Data Contract *information to be provided / is mentioned in the terms of* Data Attribute
- Data Product Port *is implemented by / implements* System
- Data Product *relates to / is related to* Measure
- Data Product *relates to / is related to* Business Term
- Data Product *relates to / is related to* Data Domain
- Data Product *is explained in / explains* Data Notebook

Notable **attributes**:

- On **Data Product**: *Data product category* (out-of-the-box values *Derived* for business
  users, *Foundational* for technical users), plus standard *Description* and governance
  attributes.
- On **Data Product Port**: *Access method*, *Access instructions*.
- On **Data Contract**: *Version*, *Manifest ID*, and a large set of SLA/SLO attributes —
  *Backup Frequency*, *Latency*, *Most Recent Record Date*, *Processing Frequency*,
  *Processing Method*, *Recency*, *Recovery Point*, *Recovery Time*, *Response Time*,
  *Retention Period*, *Unlimited Retention*, *Support Availability*, *Uptime Percentage*.

### 2.3 How data products relate to underlying assets

A consumer viewing a Data Product sees its output ports (via *exposes data as*) and, through
each port's *is implemented as* relation, the concrete **Table**/**Column** assets and their
governance context. Applying a data contract manifest (see 3.5) programmatically creates and
updates the relations between **Data Product Ports** and **Table** assets and updates the SLA
attributes — so the manifest is a driver of the underlying graph, not just documentation.

### 2.4 Collibra Data Marketplace

**Data Marketplace** is the consumer-facing shopping/discovery experience. It does not
introduce a separate metadata store; it is a curated, searchable view (defined by a
configurable *scope*) over existing assets — prominently Data Products, but also datasets,
reports, and other business assets. Consumers browse, preview, and request access; the
underlying objects remain ordinary operating-model assets. For sync purposes, treat
Marketplace as a presentation layer and target the underlying **Data Product / Data Product
Port / Data Contract / Table** assets directly.

---

## 3. API mechanics

### 3.1 API surfaces

| API | Base path | Purpose |
| --- | --- | --- |
| **Core REST API v2** | `/rest/2.0` | Main entry point: CRUD on communities, domains, assets, attributes, relations, responsibilities, tags, users, roles, workflows. |
| **Knowledge Graph API (GraphQL)** | `/graphql/knowledgeGraph` (GraphQL) | Read-oriented graph query engine over assets, communities, domains, types and complex relations, with SQL-like filtering, sorting and paging. |
| **Import API v2** | `/rest/2.0/import/...` | Bulk create/update of communities, domains, assets, complex relations and their attributes/relations/responsibilities/tags from JSON, CSV or Excel. |
| **Data Product public API v1** | `/rest/dataProduct/v1` | Manage Data Contracts and their manifest versions (initialize, upload, apply, activate, download). |
| **Catalog API** | `/rest/catalog/1.0` | Ingest non-native technical metadata into Data Catalog. |
| **Search API / Assessments / Protect / Console** | various | Search, assessment, data protection, and platform administration. |

Edge (connectors) is the runtime that performs source ingestion, profiling, classification,
and lineage; those flows are orchestrated by the Catalog Database Registration / Cloud
Ingestions / Technical Lineage APIs rather than by the Core API.

All responses are JSON. Endpoints are stateless. The Core REST API version is pinned in the
path (`/rest/2.0`); within a major release new features may be added and old ones deprecated
(deprecated features keep working but may be dropped in the next major release). The
environment version is discoverable, without authentication, via
`GET /rest/2.0/application/info`.

### 3.2 Base URL structure

A REST call URL has three parts: the instance base URL, the application path, and the
endpoint path:

```
https://<instance>.collibra.com   /rest/2.0   /assets
└── instance base URL ────────┘   └ app path ┘ └ endpoint ┘
```

Cloud environments follow `https://<instance>.collibra.com`. The in-product Swagger
reference is served at `https://<instance>.collibra.com/docs/index.html` (Import API at
`/api-docs/index.html?urls.primaryName=import-api`).

### 3.3 Authentication

Collibra supports three authentication approaches; almost every call requires one.

**1. Basic authentication.** Send an `Authorization: Basic <base64(username:password)>`
header on each request:

```bash
curl -H 'Authorization: Basic QWRtaW46YWRtaW4=' \
  https://<instance>.collibra.com/rest/2.0/communities
```

**2. Session-based (cookie) authentication.** Create a session, then reuse the returned
`JSESSIONID` cookie (and CSRF token for state-changing calls):

```bash
# Log in — creates a server-side session
curl -X POST https://<instance>.collibra.com/rest/2.0/auth/sessions \
  -H 'Content-Type: application/json' \
  -c cookies.txt \
  -d '{ "username": "<user>", "password": "<password>" }'

# Inspect the current session (returns csrfToken and user)
curl -X GET https://<instance>.collibra.com/rest/2.0/auth/sessions/current -b cookies.txt

# Log out — destroys the active session
curl -X DELETE https://<instance>.collibra.com/rest/2.0/auth/sessions/current -b cookies.txt
```

Creating a session returns a `JSESSIONID` via `Set-Cookie`; opening a new session
terminates any existing one for that user. The default idle timeout is 30 minutes between
calls.

**3. JWT / OAuth 2.0 (registered applications).** For application-to-application access, use
the OAuth 2.0 client-credentials flow. The client obtains a signed JWT access token from the
configured Identity Provider (IdP) and passes it as a Bearer token:

```bash
curl -H 'Authorization: Bearer <jwt_access_token>' \
  https://<instance>.collibra.com/rest/2.0/communities
```

The environment must have the JWT section of the Console service configuration set up, and a
Collibra user must exist whose username matches the token `sub` claim. Token `iss` (issuer)
and `aud` (audience) must match the Collibra JWT configuration. Collibra also exposes an
**OAuth 2.0 Authorization API** and **OAuth 2.0 Client Management API** for obtaining tokens
for applications registered with Collibra. A successful authorized-but-empty call returns
`204`; failures return `401` with an error code such as `malformedToken`, `expiredToken`,
`invalidToken`, or `unableToProcessToken`. The Data Product API accepts both `basicAuth` and
`jwtAuth`.

### 3.4 CRUD for metadata (Core REST API v2)

The Core API exposes Add / Change / Find / Get / Remove / Set operations on Assets,
Domains, Communities, Relations, Attributes, Comments, Users, Roles, and Permissions,
subject to the caller's permissions.

**Create a community**

```
POST /rest/2.0/communities
```
```json
{ "name": "Finance", "description": "Manages the organization's money." }
```
`name` is mandatory and unique; the response returns the new community `id` (UUID).

**Create a domain**

```
POST /rest/2.0/domains
```
```json
{
  "name": "Finance Glossary",
  "communityId": "<community_uuid>",
  "typeId": "00000000-0000-0000-0000-000000010001"
}
```
`name`, `communityId`, and `typeId` (domain type) are mandatory.

**Create an asset** (works identically for a Data Product — supply the Data Product asset
type UUID and a Data Product Catalog domain)

```
POST /rest/2.0/assets
```
```json
{
  "name": "Customer Lifetime Value",
  "typeId": "00000000-0000-0000-0000-000000011001",
  "domainId": "<domain_uuid>"
}
```
`name` (unique within the domain), `typeId` (asset type), and `domainId` are mandatory. The
response returns the asset UUID.

**Read assets back**

```
GET /rest/2.0/assets?name=Customer%20Lifetime%20Value
GET /rest/2.0/assets/{assetId}
```
List queries accept filters (name, type, domain, community, status, etc.) and paging.

**Set an attribute** (for example a Description/Definition, `Data product category`, or an
SLA value)

```
POST /rest/2.0/attributes
```
```json
{
  "assetId": "<asset_uuid>",
  "typeId": "00000000-0000-0000-0000-000000000202",
  "value": "The monetary value of a customer relationship."
}
```
`assetId`, `typeId` (attribute type), and `value` are mandatory. `value` may be a string,
number, boolean, date, or (for multi-select) an array.

**Update an attribute**

```
PATCH /rest/2.0/attributes/{id}
PATCH /rest/2.0/attributes/bulk
```
```json
[
  { "id": "<attribute_uuid_1>", "value": "Team Lead developer" },
  { "id": "<attribute_uuid_2>", "value": ["English", "French"] }
]
```

**Update an asset** (name, type, or lifecycle **status**/owner where applicable). The Change
Asset call applies only the properties present and non-null in the request; all others are
left untouched:

```
PATCH /rest/2.0/assets/{id}
```
```json
{ "displayName": "Customer Lifetime Value", "statusId": "<status_uuid>" }
```
Status can also be managed through the dedicated Change-status behavior; valid `statusId`
values come from `GET /rest/2.0/statuses`.

**Create / manage a relation** (for example bind a Data Product Port to a Table, or link a
Data Product to a Business Term)

```
POST /rest/2.0/relations
```
```json
{
  "sourceId": "<source_asset_uuid>",
  "targetId": "<target_asset_uuid>",
  "typeId": "<relation_type_uuid>"
}
```
`sourceId`, `targetId`, and `typeId` are mandatory; source/target roles are fixed by the
relation type. The response returns the relation UUID. Relations are read via
`GET /rest/2.0/relations` (filter by source, target, or type) and removed via
`DELETE /rest/2.0/relations/{id}`.

**Set tags on an asset** (replace semantics — the asset ends up with exactly the tags
supplied)

```
PUT /rest/2.0/assets/{assetId}/tags
```
```json
["gold", "certified", "pii"]
```

**Assign an owner / steward (responsibility)**

```
POST /rest/2.0/responsibilities
```
```json
{
  "roleId": "<role_uuid>",
  "ownerId": "<user_or_group_uuid>",
  "resourceId": "<asset_uuid>",
  "resourceType": "Asset"
}
```
Ownership and stewardship are expressed as responsibilities (a role assigned to a user/group
on a resource), not as plain attributes.

**Delete a resource**

```
DELETE /rest/2.0/assets/{id}
DELETE /rest/2.0/attributes/{id}
DELETE /rest/2.0/relations/{id}
```

**Bulk operations.** Most resources expose a `/bulk` endpoint that accepts a JSON array, for
example `POST /rest/2.0/communities/bulk`, `POST /rest/2.0/domains/bulk`,
`POST /rest/2.0/assets/bulk`, and `PATCH /rest/2.0/attributes/bulk`.

**Common response codes.** `201` created, `200` OK, `204` authorized/no content, `400` bad
request (details in body), `401` not authenticated, `403` no permission, `404` not found,
`409` conflict.

### 3.5 Data Product public API v1 (`/rest/dataProduct/v1`)

This API (OpenAPI `Collibra data product public API`, version 1.1.0; `basicAuth` or
`jwtAuth`) manages **Data Contracts** and their manifest versions. Note: the Data Product and
Data Product Port *assets themselves* are created/edited through the Core API (3.4); this
dedicated API focuses on contracts and manifests, which in turn update the underlying graph
when applied.

Endpoints:

| Method & path | Operation |
| --- | --- |
| `GET /dataContracts` | List data contract metadata (paginated; filter by `manifestId`, `domainId`). |
| `POST /dataContracts` | Initialize a data contract asset and link its first manifest (multipart; `governedAssetId` of the Data Product Port is required). |
| `GET /dataContracts/{id}` | Retrieve data contract metadata. |
| `DELETE /dataContracts/{id}` | Delete all versions; `deleteAsset=true` also deletes the asset. |
| `POST /dataContracts/addFromManifest` | Upload a new version, auto-parsing `manifestId`/`version` from the manifest. |
| `POST /dataContracts/{id}/draftVersion` | Generate a draft manifest via sync-and-merge. |
| `POST /dataContracts/{id}/apply` | Apply the active manifest version to Collibra (creates/updates port↔table relations and SLA attributes). |
| `GET / POST / DELETE /dataContracts/{id}/versions` | List, upload, and delete specific manifest versions. |
| `GET /dataContracts/{id}/versions/manifest` | Download a specific manifest file version. |
| `GET / PUT /dataContracts/{id}/activeVersion` | Get or set the active version. |
| `GET /dataContracts/{id}/activeVersion/manifest` | Download the active manifest. |

The `DataContract` object returned exposes `name`, `id` (asset UUID), `manifestId`,
`domainName`, `domainId`, and `activeVersion`. Manifest formats are `ODCS`, `DCS`, or
`CUSTOM`; only ODCS manifests are parsed into structured fields and matched automatically by
`Manifest ID`.

### 3.6 Import API v2 (bulk / synchronization)

For high-volume, differentiated create-vs-update loads (the typical shape of a sync engine),
the Import API ingests JSON, CSV, or Excel and can also run in **synchronization** mode.

```
POST /rest/2.0/import/json-job
POST /rest/2.0/import/csv-job
POST /rest/2.0/import/excel-job
```

Key parameters: `fileId` or `file` (exactly one), `template` (JSON template for tabular
files), `simulation` (dry-run summary), `continueOnError`, `synchronizationId`, and the
conflict-resolution controls `relationsAction` and `attributesAction`, each `REPLACE`
(default) or `ADD_OR_IGNORE`. Import handles communities, domains, assets, mappings, complex
relations, and their attributes/relations/responsibilities/tags. Recommended limits per job:
50,000 resources and 500,000 additional characteristics; disable indexing/hyperlinking for
very large loads.

### 3.7 GraphQL (Knowledge Graph API)

The Knowledge Graph API is a read-oriented GraphQL engine over the operating model. A single
endpoint queries assets, communities, domains, types, and complex relations with SQL-like
filtering, sorting, and paging. It is ideal for the *read* side of a sync (efficiently
pulling assets plus their attributes and relations in one round trip). Writes still go
through the Core, Import, or Data Product REST APIs. The full schema is available via
introspection in each environment.

### 3.8 Pagination, rate limits, versioning

- **Pagination (Core v2):** list endpoints use `offset` and `limit` query parameters and
  return a total count; iterate by advancing `offset`.
- **Pagination (Data Product v1):** cursor-based — pass `limit` (default 100, max 500),
  `cursor` (opaque, from the previous response's `nextCursor`), and optional
  `includeTotal=true`.
- **Rate limits:** Collibra does not publish fixed public per-second Core-API rate limits;
  it instead constrains bulk/import job sizes (above) and enforces a 30-minute session idle
  timeout. Design a sync engine to batch (bulk/import endpoints), back off on `429`/`5xx`,
  and respect job-size guidance rather than firing many small concurrent Core calls.
- **Versioning:** the Core API version is in the path (`/rest/2.0`); the Data Product API is
  `/rest/dataProduct/v1`; the Catalog API is `/rest/catalog/1.0`. Treat path version as the
  compatibility boundary.

---

## 4. Relevance to synchronization

### 4.1 Identity / matching keys

- **Asset UUID (`id`)** — the primary, stable key for every asset, attribute, relation,
  domain, community, and responsibility. This is the natural correlation key to persist in a
  sync mapping table.
- **Full name / name path** — `Community > Domain > Asset name` uniquely identifies an asset
  functionally; `name` is unique within a domain. Useful as a human-readable fallback match
  when UUIDs are not yet mapped.
- **Type IDs** — `assetType`, `attributeType`, `relationType`, `domainType`, and `status`
  UUIDs. Out-of-the-box types have fixed UUIDs; custom types are per-environment, so resolve
  them once via the type endpoints and cache the map.
- **Manifest ID** — for data contracts, the ODCS `Manifest ID` lets Collibra match an
  inbound manifest to the correct Data Contract asset automatically. This is the natural key
  for contract sync.

### 4.2 Readable and writable (synchronizable) metadata

The following are both readable and writable via API, so they can be synchronized in both
directions:

- Communities and domains (name, description, hierarchy, type).
- Assets: name, asset type, domain placement (create/update/delete).
- Attributes: descriptions/definitions and all custom/typed attributes, including
  data-product attributes (*Data product category*, port *Access method*/*Access
  instructions*) and Data Contract SLA attributes.
- Relations, including the data-product relations (Data Product ↔ Port, Port ↔ Table via
  *implements*, Data Product ↔ Business Term / Data Domain / Measure).
- Tags (set/replace).
- Status (lifecycle state) and responsibilities (owner/steward assignments).
- Data Contracts and their manifest versions (initialize, upload, activate, apply, delete)
  via the Data Product API; applying a manifest also writes port↔table relations and SLA
  attributes.

### 4.3 Read-only or system-managed metadata

- Audit/system fields: `createdBy`, `createdOn`, `lastModifiedBy`, `lastModifiedOn`, and the
  resource `id` itself are assigned by Collibra and cannot be set to arbitrary values.
- Profiling statistics, sample data, and technical-lineage graphs are produced by Catalog
  ingestion/Edge, not authored directly through the Core API.
- Data Marketplace presentation (scope, curation) is configuration, not per-asset writable
  metadata.
- Type definitions (asset/attribute/relation/domain types) are governed configuration;
  they are readable and manageable by administrators but should be treated as a fixed
  contract by a data-plane sync engine rather than mutated per record.

### 4.4 Practical sync guidance

- Read with GraphQL (Knowledge Graph) or Core `GET` list endpoints; write with Core `POST`/
  `PATCH`/`PUT`/`DELETE` for granular changes, or the Import API in `synchronization` mode
  for bulk reconciliation (use `attributesAction`/`relationsAction` to control REPLACE vs
  ADD_OR_IGNORE, and `simulation=true` for a safe dry run).
- Persist the Collibra asset UUID against the counterpart catalog's identifier; fall back to
  full-name matching only for first-time linkage.
- For data products specifically, sync the Data Product / Data Product Port / Table graph via
  the Core API and drive contract structure and SLAs via the Data Product API using the ODCS
  manifest and its `Manifest ID`.

---

## 5. Semantic layer

### 5.1 What Collibra means by "semantic layer" — governance, not query

Collibra is a **data governance and cataloging platform, not a query or BI engine**. Its
notion of a **semantic layer is a business/governance semantic layer**: a curated set of
metadata assets that capture the *business meaning* of data and map that meaning to the
physical catalog assets. It is emphatically **not a query-time semantic layer** in the sense
of Databricks Unity Catalog metrics, dbt/Cube-style metric definitions, or Snowflake
semantic views. Collibra does **not** execute queries, resolve metrics into SQL, serve
governed measures to BI tools at runtime, or act as a query federation/consumption layer.
The KPI and Measure asset types it offers are *documentation* of business metrics (name,
definition, calculation notes, ownership, lineage), not executable metric definitions. A
sync engine should therefore treat Collibra's semantic assets as **descriptive business
metadata to be reconciled**, and must not expect Collibra to be the source or target of a
query-executable metric/semantic model. If a counterpart catalog (Databricks, Snowflake)
owns a real query-time semantic model, only its descriptive facets — names, definitions,
owners, the mapping to physical tables/columns, classifications — map cleanly onto Collibra;
its executable calculation logic has no first-class runtime equivalent here.

### 5.2 The three data layers (Guided Stewardship operating model)

Collibra's business-semantic modeling is delivered mainly through the **Guided Stewardship
operating model**, an out-of-the-box model with three stacked layers. From concrete to
abstract:

- **Physical layer** — the storage-level structure as it exists in source systems:
  **Database**, **Schema**, **Table**, **Column** assets (Schema/Table/Column come with Data
  Catalog and are almost always created automatically by registration/ingestion, not by
  hand). This is the same technical metadata described in section 1.3.
- **Semantic layer** (also called the **logical data layer**) — a business-centric view of
  data for a *specific* system. Its building blocks are **Data Model**, **Data Entity**, and
  **Data Attribute** assets, plus the **System** technology asset. This layer is the bridge
  between raw physical assets and the higher business/governance (Knowledge Graph) assets
  such as Business Term, KPI, and Data Category. Data Model, Data Entity, and Data Attribute
  asset types are available with Guided Stewardship.
- **Conceptual layer** — the enterprise, system-independent blueprint: **Line of Business**,
  **Data Domain**, and **Data Concept** assets. It defines concepts such as Customer or
  Product and their component fields (Name, Address, ID Number) without reference to any
  specific system, using flexible many-to-many relationships. Collibra positions this layer
  as optional/advanced; the physical + semantic layers are the recommended starting point.

Alongside these sit the traditional **Business Glossary** assets — **Business Term** (and
related governance assets like KPI, Data Category, Policy). Business Terms capture the
authoritative business vocabulary and connect the vocabulary to the modeled data. Collibra
distinguishes the conceptual layer (structural, one Data Concept per idea) from the Business
Glossary (nuanced, potentially many business terms for the same underlying concept across
languages, lines of business, and cultures).

### 5.3 Semantic asset types and how they map to physical data and data products

The layers are stitched together by well-defined out-of-the-box relation types. Key
mappings (source *role / inverse role* target):

- **Data Attribute → Column**: *Data Attribute represents / represented by Column*. This is
  the anchor tying a logical attribute to one or more physical columns (a Data Attribute can
  represent many columns; a column is represented by one Data Attribute). The **Physical
  Data Connector** feature automates creating these Data Attribute↔Column links.
- **Data Entity → Data Attribute**: *Data Entity contains / is part of Data Attribute* — a
  Data Entity (e.g. Customer, Product) groups its Data Attributes (e.g. Customer Email).
- **Data Model → Data Entity**: *Data Model contains / is contained in Data Entity*, and
  **System → Data Model**: *System implements / is implemented in Data Model* (one-to-one,
  which is what makes the semantic/logical layer system-*context-dependent*).
- **Data Concept → Data Attribute**: *Data Concept classifies / is classified by Data
  Attribute* — links the context-independent conceptual layer down to concrete logical
  attributes.
- **Business Term → Data Attribute**: *Business asset represents / is represented by Data
  asset* — connects the glossary vocabulary to the logical model.
- **Data Category → Data Attribute** and **KPI/Measure → Data Attribute** (*Measure is
  calculated using / is used to calculate by Data Element*) — attach classifications and
  documented metrics to the logical layer.
- **Conceptual grouping**: *Line of Business groups Data Domain*, *Data Domain groups Data
  Concept* (many-to-many), plus *Data Domain has subtype / is subtype of Data Domain*.

Relationship to **Data Products**: data products link directly to semantic/conceptual and
glossary assets through the data-product relation types already listed in section 2.2 —
*Data Product relates to Business Term*, *Data Product relates to Data Domain*, and *Data
Product relates to Measure*. So a data product acquires business meaning by pointing at
glossary/semantic assets, while its *physical* exposure runs through Data Product Port →
Table (section 2.3). The full chain therefore reads: **Data Product → (Business Term / Data
Domain / Measure) for meaning**, and separately **Data Product → Port → Table → Column**,
with the semantic layer bridging Column ← Data Attribute ← Data Entity ← Data Model and the
conceptual layer classifying via Data Concept / Data Domain.

Representative out-of-the-box type UUIDs (usable as `typeId` in Core API calls; resolve
custom types per environment via the type endpoints):

| Type | Kind | UUID |
| --- | --- | --- |
| Business Term | Asset type | `00000000-0000-0000-0000-000000011001` |
| KPI | Asset type | `00000000-0000-0000-0000-000000011002` |
| Data Model | Asset type | `00000000-0000-0000-0000-000000031003` |
| Data Entity | Asset type | `00000000-0000-0000-0000-000000031004` |
| Data Attribute | Asset type | `00000000-0000-0000-0000-000000031005` |
| Data Category | Asset type | `00000000-0000-0000-0000-000000031109` |
| Data Attribute represents / represented by Column | Relation type | `00000000-0000-0000-0000-000000007094` |
| Data Entity contains / is part of Data Attribute | Relation type | `00000000-0000-0000-0000-000000007047` |
| Data Entity is part of / contains Data Model | Relation type | `00000000-0000-0000-0000-000000007046` |
| Business Term represents / represented by Data Attribute | Relation type | `00000000-0000-0000-0000-000000007038` |
| KPI Measure calculated using / used to calculate Data Attribute | Relation type | `00000000-0000-0000-0000-000000007200` |

### 5.4 API surface for the semantic layer

There is **no dedicated semantic-layer API**; these assets are ordinary operating-model
assets and are managed through the same surfaces described in section 3:

- **Read** — the **Knowledge Graph GraphQL API** (`/graphql/knowledgeGraph`) is the most
  efficient way to pull a Data Model with its Data Entities, Data Attributes, their
  Column mappings, and their glossary/classification relations in one traversal. Core
  `GET /rest/2.0/assets` (+ `/attributes`, `/relations`) works for granular reads.
- **Write / CRUD** — use the **Core REST API v2**: create the assets with
  `POST /rest/2.0/assets` (supplying the Data Model / Data Entity / Data Attribute / Business
  Term / Data Concept / Data Domain asset-type UUID and a domain of the appropriate type),
  set business definitions with `POST /rest/2.0/attributes` (e.g. the *Definition* attribute
  type), and wire the layer together with `POST /rest/2.0/relations` using the relation-type
  UUIDs above (for example Data Attribute→Column, Business Term→Data Attribute). Update and
  delete follow the same `PATCH`/`DELETE` patterns as any asset.
- **Bulk / sync** — the **Import API v2** in synchronization mode is the recommended path for
  loading or reconciling many semantic assets and their relations at once, with
  `attributesAction`/`relationsAction` controlling REPLACE vs ADD_OR_IGNORE and
  `simulation=true` for dry runs.
- **Assisted authoring** — recent Collibra releases add a *Semantic Layer* submenu and an
  optional **Collibra AI** assist (in preview) plus the **Physical Data Connector** to help
  generate the semantic layer and auto-link Data Attributes to Columns. These are authoring
  aids in the product; a sync engine still reads/writes the resulting assets through the
  Core/Import/GraphQL APIs above.

### 5.5 Which semantic fields are synchronizable

Because semantic/conceptual/glossary assets are plain operating-model assets, the same
read/write rules from section 4.2 apply. Synchronizable in both directions:

- Asset identity and placement: name, asset type (Data Model / Data Entity / Data Attribute /
  Business Term / Data Concept / Data Domain / KPI / Data Category), domain, status.
- Business-meaning attributes: *Definition*/*Description* and any custom typed attributes on
  these assets (a documented metric's calculation *description* on a KPI, for example — as
  text, not as executable logic).
- The relations that constitute the semantic model itself: Data Attribute↔Column,
  Data Entity↔Data Attribute, Data Model↔Data Entity, System↔Data Model, Data Concept↔Data
  Attribute, Data Domain/Line-of-Business groupings, and the Business Term / Data Category /
  KPI links to Data Attribute — plus the Data Product↔Business Term / Data Domain / Measure
  links.
- Tags and responsibilities (owner/steward) on any of these assets.

Not synchronizable as arbitrary values: system/audit fields (`createdOn`, `lastModifiedBy`,
`id`), profiling/lineage derived from ingestion, and — most importantly for expectation
management — **any notion of executable metric/query logic**, because Collibra does not
provide a query-time semantic layer to hold or run it.

---

# Citations

- https://developer.collibra.com/tutorials/getting-started-with-collibra-rest-api — Collibra Developer Portal, "Getting started with Collibra REST API": REST principles, list of REST applications (Core, Import, Catalog, Search, etc.), base URL structure, `application/info`, versioning behavior.
- https://developer.collibra.com/tutorials/create-data-with-collibra-rest-api — Collibra Developer Portal, "Create data with Collibra REST API": concrete `POST` examples and mandatory fields for communities, domains, assets, and attributes; out-of-the-box type UUIDs; `/bulk` endpoints; response codes.
- https://developer.collibra.com/tutorials/update-multiple-attributes-with-the-rest-api — Collibra Developer Portal, "Update multiple attributes with the REST API": `PATCH /rest/2.0/attributes/bulk`, attribute `id`/`value` semantics, multi-select arrays, reading asset/attribute IDs.
- https://developer.collibra.com/tutorials/collibra-rest-api-authentication — Collibra Developer Portal, "Collibra REST API authentication": Basic authentication header format and usage.
- https://developer.collibra.com/tutorials/collibra-rest-api-authentication-with-json-web-token — Collibra Developer Portal, "Collibra REST API authentication with JSON Web Token": OAuth 2.0 client-credentials/JWT bearer flow, prerequisites, `iss`/`aud`/`sub` matching, error codes, response codes.
- https://developer.collibra.com/api/references/data-governance/authentication-sessions — Collibra Developer Portal, "Authentication (Sessions)": `POST /rest/2.0/auth/sessions`, current-session `GET`, logout `DELETE /auth/sessions/current`, JSESSIONID and 30-minute idle timeout.
- https://developer.collibra.com/api/references/data-product/data-contract — Collibra Developer Portal, "Data Contract" (Data Product public API v1, `/rest/dataProduct/v1`): data contract endpoints, `DataContract` schema fields, cursor pagination (limit default 100/max 500), manifest formats (ODCS/DCS/CUSTOM), apply/activate/version operations, basic and JWT auth.
- https://developer.collibra.com/api/graphql/knowledge-graph — Collibra Developer Portal, "Knowledge Graph API": GraphQL query engine over assets/communities/domains/types/complex relations with SQL-like filtering, sorting, and paging.
- https://developer.collibra.com/api/guides/import-api — Collibra Developer Portal, "Working with the Import API v2": import/synchronization endpoints, mandatory and optional parameters (`fileId`/`file`/`template`, `simulation`, `continueOnError`, `relationsAction`, `attributesAction`, `synchronizationId`), and job-size limits.
- https://productresources.collibra.com/docs/collibra/latest/Content/DataProducts/co_data-product-om.htm — Collibra Product Resource Center, "Data product asset types and operating model": Data Product / Data Product Port / Data Contract asset types, asset type groups, data-product relation types, and data-product/Data Contract attributes.
- https://productresources.collibra.com/docs/collibra/latest/Content/DataProducts/co_data-product.htm — Collibra Product Resource Center, "About data products and data contracts": data product definition and four components (Context/Data/Controls/Access), Data Product Catalog domain, and data contract vs manifest (ODCS, Manifest ID, apply-to-knowledge-graph behavior).
- https://productresources.collibra.com/docs/collibra/latest/Content/Catalog/GuidedStewardship/OperatingModel/to_catalog-om.htm — Collibra Product Resource Center, "About the Guided Stewardship operating model and data layers": the three layers (physical, semantic/logical, conceptual); Data Model / Data Entity / Data Attribute / Data Concept / Data Domain / Line of Business asset types; the relation types linking them (including Data Attribute represents/represented by Column, Business Term represents Data Attribute, Data Concept classifies Data Attribute); out-of-the-box asset- and relation-type UUIDs.
- https://productresources.collibra.com/docs/collibra/latest/Content/BusinessGlossary/to_business-glossary.htm — Collibra Product Resource Center, "Business Glossary": business terms as the governed business vocabulary, configurable asset types/attributes/taxonomy/relations, and integration with technical assets.
- https://productresources.collibra.com/docs/collibra/latest/Content/Catalog/GuidedStewardship/OperatingModel/co_concept-data-layer-v-bus-gloss.htm — Collibra Product Resource Center, "Conceptual layer versus the Business Glossary": distinction between the context-independent conceptual layer (one Data Concept per idea) and the nuanced Business Glossary vocabulary.
- https://productresources.collibra.com/docs/collibra/latest/Content/Catalog/GuidedStewardship/PhysicalDataConnector/co_physical-data-connector.htm — Collibra Product Resource Center, "About the Physical Data Connector": automated linking of Data Attribute assets to Column assets to connect the logical and physical layers.
- https://productresources.collibra.com/docs/collibra/latest/Content/Catalog/GuidedStewardship/SemanticLayer/ta_semantic-layer.htm — Collibra Product Resource Center, "Create and manage the semantic layer manually or with Collibra AI": Semantic Layer submenu and optional Collibra AI-assisted authoring of the semantic (logical) layer.
