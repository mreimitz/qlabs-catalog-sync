---
type: "Research Output"
title: "Databricks Unity Catalog & Data Products — API Reference"
description: "Detailed reference of Databricks catalog assets, data products, and the read/write API surface for metadata synchronization."
tags: ["research", "RS-01", "databricks", "unity-catalog", "api"]
timestamp: "2026-08-06T08:00:00Z"
status: "draft"
---

# Databricks Unity Catalog & Data Products — API Reference

This reference describes how Databricks manages catalog metadata and "data products" through Unity Catalog, and it maps the read/write API surface relevant to building a two-way metadata sync bridge. It is organized around four areas: (1) catalog assets and their metadata fields, (2) the data-product concepts (Delta Sharing / OpenSharing and Databricks Marketplace), (3) API mechanics (surfaces, authentication, CRUD), and (4) which fields are synchronizable versus read-only.

All object names, endpoints, and behaviors below are drawn from official Databricks documentation (see `# Citations`). Endpoint paths use the current API versions; treat version numbers as load-bearing and confirm them against the live API reference before implementation.

---

## 1. Catalog assets and the Unity Catalog object model

Unity Catalog (UC) is Databricks' governance layer. It maintains a hierarchy of **securable objects** — objects on which privileges can be granted to a principal (user, service principal, or group). This hierarchy is the backbone of catalog metadata.

### 1.1 Object hierarchy

The **metastore** is the top-level securable object, scoped to a single cloud region; one metastore can be attached to many workspaces in that region. Data assets live in a **three-level namespace** `catalog.schema.object`:

- **Metastore** (top level, one per region)
  - **Catalog** — first level of the namespace; a container that groups schemas by organizational unit or SDLC scope.
    - **Schema** (a.k.a. database) — second level; container for the leaf data/AI objects.
      - **Table** — structured data (managed, external, or foreign).
      - **View** — a stored SQL query; sub-types: **materialized view** (precomputed) and **metric view** (reusable metric definitions).
      - **Volume** — governed unstructured files in cloud storage (managed or external).
      - **Function** — reusable executable logic: SQL/Python UDFs, stored procedures, and **registered models** (MLflow models registered in UC; a model is a container of versioned **model versions**).
      - **Service** — governed invocable AI assets (model services, MCP services; in Beta).
      - **Secret** — governed sensitive values (`catalog.schema.secret`).
      - **Feature** — stored ML feature definitions (Public Preview).

Additional securable objects sit **directly under the metastore** (not in the three-level namespace):

- **Storage credentials** and **external locations** — govern access to cloud storage paths.
- **Service credentials** — auth info for external cloud services.
- **Connections** — endpoints/credentials for external systems (query federation, catalog federation, ingestion, JDBC, HTTP).
- **External metadata** — objects used to declare **custom lineage** for systems outside UC.
- **Shares**, **providers**, **recipients** — the Delta Sharing / OpenSharing objects (see Section 2).
- **Clean rooms** — secure cross-org collaboration environments.

Privilege inheritance flows down catalog → schema → object (metastore-level grants do NOT inherit). The `USE CATALOG` + `USE SCHEMA` usage privileges are prerequisites for touching any child object, and `BROWSE` (catalog-level only) allows metadata discovery without data access.

### 1.2 Metadata fields attached to objects

Across catalogs, schemas, tables, views, volumes, functions, and models, the common metadata surface is:

- **Name** — the object's identifier within its parent; the full identity is the qualified name (e.g. `full_name = catalog.schema.table`).
- **Comment / description** — an open-ended free-text field used for discovery. Set at create time, via `COMMENT ON`, via `ALTER ... SET ... COMMENT`, or via the object's REST `comment` field. Columns carry their own comments. Databricks also supports AI-generated comments.
- **Owner** — the principal (user, group, or service principal) that owns the object. Owners can manage and transfer ownership.
- **Tags** — key + optional value attributes for organizing/discovering objects (see 1.3).
- **Properties** — free-form key-value metadata. Catalogs and schemas carry `properties`; tables carry `TBLPROPERTIES` (also used for Delta features).
- **System / audit fields** — `metastore_id`, object IDs (e.g. `catalog id`, `table_id`), `created_at`, `created_by`, `updated_at`, `updated_by`. These are system-managed.
- **Type-specific fields** — e.g. tables have `table_type` (MANAGED/EXTERNAL/FOREIGN/VIEW), `data_source_format`, `storage_location`, `columns` (name, type, nullability, comment, position); volumes have `volume_type` and `storage_location`; catalogs have `isolation_mode` and workspace bindings.

### 1.3 Tags (classifications)

Tags are the primary classification mechanism.

- **Supported objects**: catalogs, schemas, tables, table columns, volumes, views, functions, registered models, model versions, and external metadata objects. Also dashboards, Genie agents, apps, and notebooks (separate surfaces).
- **Constraints**: keys are case-sensitive; max 50 tags per securable object; up to 1,000 column tags per table; key and value max 256 chars; keys may not contain `. , - = / :`; no leading/trailing spaces; tag search requires exact matching; you cannot tag multiple columns in one `ALTER TABLE` (unlike `COMMENT`).
- **Governed tags**: account-level tags with enforced allowed keys/values and permission-gated assignment (need `ASSIGN` on the governed tag). **System tags** are Databricks-predefined governed tags (e.g. for data classification). A governed tag stores as plain text — no sensitive data.
- **Read-back**: `INFORMATION_SCHEMA.CATALOG_TAGS`, `SCHEMA_TAGS`, `TABLE_TAGS`, `COLUMN_TAGS`, `VOLUME_TAGS`.

### 1.4 Lineage

UC automatically captures **table-level and column-level lineage** for operations run on Databricks compute. Lineage is queryable via system tables (`system.access.table_lineage`, `system.access.column_lineage`) and the Lineage REST API. Native lineage is **derived and read-only**; the only writable lineage path is the **external metadata** object, which lets you declare custom lineage relationships for non-Databricks systems.

---

## 2. "Data products" in the Databricks ecosystem

Databricks does not (today) expose a single primitive literally named "data product" in Unity Catalog. In practice a data product is realized through three overlapping mechanisms, all of which reference underlying UC objects:

### 2.1 Delta Sharing / OpenSharing — shares and recipients

The **OpenSharing (Databricks-to-Databricks) protocol** and the open Delta Sharing protocol are the primary way to publish a curated data product to external consumers.

- A **share** is a metastore-level securable object: a named, logical grouping of data assets (tables, views, volumes, and — for D2D — notebooks/models) that a provider intends to share.
- A **recipient** is a metastore-level object representing the external organization/user group that consumes a share. Access is granted by giving `SELECT` on the share to the recipient.
- A **provider** object (created in the recipient's metastore) represents an external org that shared data with you.
- Shares carry metadata: `name`, `comment`, `owner`, and per-object entries with their own `comment`, share-alias name, partition specs, and `history_data_sharing_status` (whether history/CDF is shared). Recipients carry `name`, `comment`, `owner`, `authentication_type` (TOKEN vs DATABRICKS), and IP access lists.

This is the closest thing to a first-class "shareable data product" and it is fully metadata-bearing and API-manageable.

### 2.2 Databricks Marketplace — listings and products

The **Databricks Marketplace** is an open exchange, built on Delta Sharing, for publishing and consuming data products, notebooks, ML models, and solution accelerators.

- A provider publishes a **listing**, which wraps a share (or other assets) with rich product metadata: title/summary, detailed description, category, data-source info, documentation and support links, license, pricing model / cost, geographical coverage, update frequency, embedded notebook file info, and tags.
- Consumers discover listings and request/instantly get access; accepted listings surface the shared data as a catalog in the consumer's metastore.
- Marketplace metadata is managed through the **Provider Listings** and **Consumer Listings** APIs (Section 3).

### 2.3 How these relate to catalog metadata

Both mechanisms are layered on top of UC securables: a share references concrete `catalog.schema.table` objects, and a listing references a share. Descriptions/tags on the underlying UC objects are distinct from the share comment and the listing's product description — a sync bridge must treat them as separate metadata surfaces that point at the same physical assets.

---

## 3. API mechanics

### 3.1 API surfaces

| Surface | What it manages | Typical base path |
| --- | --- | --- |
| **Unity Catalog REST API** | catalogs, schemas, tables, volumes, functions, registered models, model versions, grants/permissions, shares, recipients, providers, external locations, storage credentials, connections, lineage | `/api/2.1/unity-catalog/...` |
| **SQL DDL** (via SQL warehouse / notebook / Statement Execution API) | `CREATE/ALTER/DROP` objects, `COMMENT ON`, `SET TAGS` / `UNSET TAGS`, `SET TAG` / `UNSET TAG`, `ALTER ... OWNER TO`, `TBLPROPERTIES`, `CREATE/ALTER SHARE` | SQL statements |
| **Statement Execution API** | run SQL DDL/DML over a serverless/SQL warehouse via REST | `/api/2.0/sql/statements` |
| **Marketplace APIs** | provider listings, consumer listings, provider exchanges/personalization requests | `/api/2.0/marketplace-provider/...`, `/api/2.0/marketplace-consumer/...` |
| **Delta Sharing REST API** | the open sharing wire protocol consumed by recipients | recipient-facing sharing server endpoints |
| **Databricks SDKs / CLI / Terraform** | wrappers over the above (Python, Go, Java SDKs; `databricks` CLI; `databricks_*` Terraform resources) | n/a |
| **Account REST API** | account-level admin (metastores, workspace assignment, service principals, governed tag policies) | `/api/2.0/accounts/{account_id}/...` |

### 3.2 Authentication

- **Base URLs**: workspace-level APIs use the workspace host `https://<workspace-host>/api/...`; account-level APIs use the account console host `https://accounts.<cloud>.databricks.com/api/2.0/accounts/{account_id}/...`. Do not append `/api` to the host you configure in tooling.
- **Personal access tokens (PAT)** — legacy. Sent as `Authorization: Bearer <token>`. Workspace-scoped; cannot drive account-level functionality. Databricks now recommends OAuth over PATs.
- **OAuth M2M (service principal, client credentials)** — recommended for automation. Use the service principal's **client ID** + **OAuth secret** against the token endpoint to obtain a bearer access token valid for ~1 hour, then refresh:

```http
POST https://<workspace-host>/oidc/v1/token
Content-Type: application/x-www-form-urlencoded
Authorization: Basic base64(<client_id>:<client_secret>)

grant_type=client_credentials&scope=all-apis
```

  The returned `access_token` is then passed as `Authorization: Bearer <access_token>` to REST calls.
- **OAuth U2M** — interactive user login flow (for user-driven tools/CLI).
- **Workspace vs account scope**: account-level OAuth tokens can call both account and workspace APIs the principal can reach; workspace-level tokens are limited to a single workspace. Account-level automation (e.g. governed tag policies, metastore assignment) requires account admin or a service principal, not PATs.

### 3.3 CRUD for catalog metadata

#### Catalogs (REST)

```http
# Create
POST /api/2.1/unity-catalog/catalogs
{ "name": "sales", "comment": "Sales domain", "properties": {"team": "sales"} }

# Read
GET  /api/2.1/unity-catalog/catalogs                # list (paginated)
GET  /api/2.1/unity-catalog/catalogs/{name}         # get one

# Update (owner, comment, rename, properties, isolation_mode)
PATCH /api/2.1/unity-catalog/catalogs/{name}
{ "owner": "data-eng-sp", "comment": "Curated sales catalog", "new_name": "sales_prod" }

# Delete
DELETE /api/2.1/unity-catalog/catalogs/{name}?force=true
```

Schemas (`/api/2.1/unity-catalog/schemas`, identity `full_name = catalog.schema`), volumes (`/volumes`), functions (`/functions`), registered models (`/models`), and model versions follow the same create / list / get / update (PATCH) / delete shape with `comment`, `owner`, and `new_name` fields where applicable.

#### Setting descriptions / comments (SQL)

```sql
COMMENT ON CATALOG sales IS 'Curated sales domain';
COMMENT ON TABLE  sales.orders.line_items IS 'One row per order line';
ALTER TABLE sales.orders.line_items ALTER COLUMN amount COMMENT 'Net amount in USD';
```

#### Adding / updating / removing tags (SQL — DBR 13.3+)

```sql
-- object-level
ALTER TABLE  sales.orders.line_items SET TAGS ('domain' = 'sales', 'pii' = 'false');
ALTER TABLE  sales.orders.line_items UNSET TAGS ('pii');
-- column-level (one column per statement)
ALTER TABLE  sales.orders.line_items ALTER COLUMN email SET TAGS ('pii' = 'email');
-- generic securable syntax (DBR 16.1+)
SET TAG   ON CATALOG sales `cost_center` = `hr`;
UNSET TAG ON CATALOG sales cost_center;
```

Registered-model tags must be set via Catalog Explorer or the MLflow Client API, not `ALTER`.

#### Changing owners

```sql
ALTER TABLE   sales.orders.line_items OWNER TO `data-eng-sp`;
ALTER SCHEMA  sales.orders            OWNER TO `data-eng-sp`;
```

Or via REST `PATCH ... {"owner": "<principal>"}` on catalogs, schemas, volumes, models, shares, etc.

#### Reading metadata back

- REST `GET` on each object returns its full metadata document.
- SQL: `DESCRIBE {CATALOG|SCHEMA|TABLE} EXTENDED ...`, `SHOW TBLPROPERTIES`, and the `INFORMATION_SCHEMA` tables (`TABLES`, `COLUMNS`, `*_TAGS`, `TABLE_PRIVILEGES`).
- Grants: `GET /api/2.1/unity-catalog/permissions/{securable_type}/{full_name}` (update with `PATCH` using `changes[].{principal, add[], remove[]}`).

#### Tables — important caveat

Tables are primarily created and altered through the SQL/engine layer, not a generic REST "update". The **Tables API** (`/api/2.1/unity-catalog/tables`) supports list / get / exists / delete; table `comment`, `tags`, `owner`, and `TBLPROPERTIES` are written via SQL DDL. Plan table-metadata sync around SQL DDL + Statement Execution API, and use the Tables API / `INFORMATION_SCHEMA` for reads.

### 3.4 CRUD for Delta Sharing shares (data products)

REST:

```http
POST  /api/2.1/unity-catalog/shares            { "name": "sales_share", "comment": "External sales feed" }
GET   /api/2.1/unity-catalog/shares            # list
GET   /api/2.1/unity-catalog/shares/{name}     # get, ?include_shared_data=true for objects
PATCH /api/2.1/unity-catalog/shares/{name}     # add/remove data objects, update comment/owner
DELETE /api/2.1/unity-catalog/shares/{name}
```

`PATCH` uses an `updates[]` array of `{action: ADD|REMOVE|UPDATE, data_object: {name, data_object_type, shared_as, comment, ...}}`.

SQL equivalent:

```sql
CREATE SHARE sales_share COMMENT 'External sales feed';
ALTER SHARE  sales_share ADD TABLE sales.orders.line_items;
ALTER SHARE  sales_share REMOVE TABLE sales.orders.line_items;
```

Recipients: `POST/GET/PATCH/DELETE /api/2.1/unity-catalog/recipients` (fields `name`, `comment`, `owner`, `authentication_type`, `ip_access_list`). Grant a share to a recipient with `GRANT SELECT ON SHARE sales_share TO RECIPIENT acme;`.

### 3.5 CRUD for Marketplace listings (data products)

```http
# Provider: create a listing (product metadata wrapping a share)
POST /api/2.0/marketplace-provider/listing
{ "listing": { "summary": {...}, "detail": { "description": "...", "tags": [...],
    "cost": "...", "license": "...", "update_frequency": "...", "assets": [...] } } }

GET    /api/2.0/marketplace-provider/listings          # list provider listings
GET    /api/2.0/marketplace-provider/listings/{id}     # read
PUT    /api/2.0/marketplace-provider/listings/{id}     # update
DELETE /api/2.0/marketplace-provider/listings/{id}     # delete

# Consumer side
GET  /api/2.0/marketplace-consumer/listings            # discover
```

These require the `marketplace` API scope. A successful create returns the new listing `id`.

### 3.6 Pagination, rate limits, versioning

- **Pagination**: list endpoints accept `max_results` and return a `next_page_token`; pass it back as `page_token` until absent. Some UC list calls default to unbounded/large page sizes — always page defensively.
- **Rate limits**: Databricks enforces per-endpoint and per-workspace limits; over-limit calls return HTTP `429` (with `Retry-After` where applicable). Implement exponential backoff and honor `Retry-After`. Delta Sharing and Statement Execution have their own throughput limits.
- **Versioning**: UC endpoints are on `2.1`; several account/marketplace/statement endpoints are on `2.0`. Versions are pinned in the path; confirm current versions in the live API reference, since some resources are in Public Preview/Beta and can change.

---

## 4. Relevance to two-way metadata sync

### 4.1 Identity / matching keys

- **Primary key for namespace objects**: the fully qualified name `catalog.schema.object` (`full_name`). Object names are case-insensitive for resolution, but **tag keys are case-sensitive** — handle casing carefully.
- **Stable IDs** exist and should be stored for reconciliation: `metastore_id`, catalog/schema IDs, `table_id`, model IDs, share ID, recipient ID, and Marketplace listing `id`. Names can be renamed (`new_name`); IDs are stable and are the safer join key.
- **Principals** (owner, grants) are identified by user email, group name, or service principal application ID.

### 4.2 Readable AND writable (synchronizable) fields

| Field | Write mechanism |
| --- | --- |
| Comment / description (catalog, schema, table, column, volume, function, model, share, listing) | REST `comment` field; `COMMENT ON`; `ALTER ... COMMENT` |
| Tags / key-value classifications (supported objects + columns) | SQL `SET TAGS`/`UNSET TAGS`, `SET TAG`/`UNSET TAG` (governed tags need `ASSIGN`); models via MLflow/Catalog Explorer |
| Owner | REST `PATCH owner`; `ALTER ... OWNER TO` |
| Properties / `TBLPROPERTIES` | REST `properties`; `ALTER ... SET TBLPROPERTIES` |
| Name (rename) | REST `new_name`; `ALTER ... RENAME TO` |
| Grants / privileges | `PATCH permissions`; `GRANT`/`REVOKE` |
| Share membership + share/recipient comment & owner | Shares/Recipients REST `PATCH`; `CREATE/ALTER SHARE`, `GRANT ... ON SHARE` |
| Marketplace listing product metadata (description, tags, cost, license, links, update frequency) | Provider Listings REST |
| Custom lineage (non-Databricks systems) | External metadata object |

### 4.3 Read-only (cannot be synchronized as writes)

- **Native lineage** (table/column) — system-derived; read via system tables / Lineage API. Only external-metadata custom lineage is writable.
- **Audit/system fields** — `created_at`, `created_by`, `updated_at`, `updated_by`, `metastore_id`, and object IDs.
- **Generated/structural fields** — `full_name` (derived from parent + name), managed-table `storage_location`, `table_type`/`data_source_format`. Column data types/schema are changeable only through table DDL, not as free-form metadata edits.
- **System tags** — governed by Databricks; keys/values are predefined and cannot be modified (only assigned if permitted).

### 4.4 Practical sync guidance

- Prefer **REST** for catalog/schema/volume/model/share/recipient/listing CRUD, and the **Statement Execution API + SQL DDL** for table/column comments, tags, owners, and `TBLPROPERTIES` (no generic table-update REST endpoint).
- Reconcile on **stable IDs**, not names; capture rename events via `new_name`.
- Treat the three metadata surfaces as distinct: UC object metadata, share/recipient metadata, and Marketplace listing metadata — a single physical table may carry all three, each edited through a different API.
- Respect governed-tag permissions (`ASSIGN`) and workspace-vs-account scoping when planning the service-principal identity the bridge runs as.

---

## 5. Semantic layer (Unity Catalog metric views)

**Yes — Databricks has a native semantic layer.** It is delivered as **Unity Catalog business semantics**, and its core implementation is the **metric view**. Business semantics has two integrated parts: (1) **metric views** — reusable, governed objects that define business KPIs — and (2) **agent metadata** — display names, synonyms, and formatting rules that let BI and AI tools interpret metrics in business terms. This layer is directly relevant to a sync bridge because it is where the "meaning" of the data (measures, dimensions, business definitions) lives, not just the physical schema.

### 5.1 What a metric view is

A metric view is a **UC view sub-type** (`metric view`) that separates **measure** definitions (the aggregations, e.g. sum of revenue) from **fields / dimensions** (the columns used to group, filter, and slice). Unlike a standard view, which locks aggregation and grouping at creation time, a metric view defines each metric once; consumers then group by any available field and the query engine generates the correct computation at runtime. This guarantees a single, consistent value for a KPI across every consuming tool.

The definition is written as a **YAML specification** embedded in DDL. Its top-level elements are:

- `version` — YAML spec version (current `1.1`; agent metadata and some features require spec `1.1` on Databricks Runtime 17.3+).
- `comment` — a description of the metric view.
- `source` — the base table, view, or SQL query the metrics are computed from.
- `joins` — related tables (star / snowflake modeling, multi-level joins, cardinality, `at_most_one_match`).
- `filter` — a predicate applied to every query against the view.
- `fields` (a.k.a. `dimensions` — equivalent keywords; the low-code UI emits `dimensions`) — each has `name`, `expr`, and optional `comment` plus agent metadata.
- `measures` — each has `name`, `expr` (an aggregation such as `SUM(...)`, `COUNT(1)`), and optional metadata; **window measures** support trailing averages, period-over-period, and cumulative totals.
- Optional `materialization` (pre-compute / incremental refresh, with automatic query rewrite) and query-time `parameters`.

### 5.2 Catalog assets and metadata the semantic layer holds

Because a metric view is a first-class UC securable object registered in the three-level namespace (`catalog.schema.metric_view`), it carries the standard object metadata (name, `comment`, owner, tags, grants, lineage) plus the semantic payload:

- **The metric model itself** — source, joins, filter, fields/dimensions and their expressions, measures and their aggregation expressions, window measures, and materialization settings. This is the durable business logic.
- **Agent (semantic) metadata**, defined per field/measure inside the YAML:
  - `display_name` — human-readable label surfaced in visualization tools (max 255 chars).
  - `synonyms` — up to 10 alternative names per field/measure (max 255 chars each) that help natural-language / LLM tools like Genie discover them.
  - `format` — display formatting (number, currency with ISO-4217 code, percentage, byte; date / date_time formats with decimal-place, grouping, and abbreviation controls).
  - `comment` — free-text description per field/measure.
  - `governed tags` — governed-tag classifications on fields/measures.
- **Downstream surfacing** — display names and formats auto-populate AI/BI dashboards; synonyms auto-import into Genie Spaces. External BI tools (Power BI, Tableau, Sigma) query metric views (via a BI compatibility mode), and semantic-layer / partner tools (including dbt and other semantic-layer partners) connect through Partner Connect. These partner/dbt semantic layers are separate products that integrate with, rather than replace, UC metric views.

### 5.3 API / SQL surface for CRUD

Metric views are **managed as views through SQL DDL** (run interactively or over the **Statement Execution API** on a SQL warehouse / DBR 16.4+, with 17.3+ for agent metadata), plus the Catalog Explorer UI and Genie Code assistant. There is no dedicated first-class "metric view" REST create/update endpoint distinct from views; reads go through the Tables/Views surfaces and `INFORMATION_SCHEMA`.

```sql
-- Create (or replace): YAML spec between $$ delimiters
CREATE OR REPLACE VIEW sales.metrics.orders_metric_view WITH METRICS LANGUAGE YAML AS
$$
  version: 1.1
  comment: "Orders KPIs for sales analysis"
  source: samples.tpch.orders
  filter: source.o_orderdate > '1990-01-01'
  fields:
    - name: Order Month
      expr: DATE_TRUNC('MONTH', source.o_orderdate)
      comment: "Month of order"
  measures:
    - name: Total Revenue
      expr: SUM(source.o_totalprice)
      comment: "Sum of all order prices"
$$;

-- Update: ALTER VIEW replaces the ENTIRE definition (not a partial patch)
ALTER VIEW sales.metrics.orders_metric_view AS $$ <complete YAML definition> $$;

-- Read the definition / query the metrics
DESCRIBE EXTENDED sales.metrics.orders_metric_view;   -- inspect
SELECT `Order Month`, `Total Revenue` FROM sales.metrics.orders_metric_view;

-- Grant query access (standard UC securable privileges)
GRANT SELECT ON sales.metrics.orders_metric_view TO `data-consumers`;

-- Delete
DROP VIEW sales.metrics.orders_metric_view;
```

Governance follows the standard UC model: any principal with `SELECT` can query; **only the owner can edit** the definition (transfer ownership to a group for collaborative editing — not supported for materialized metric views); creating one requires `CREATE TABLE` + `USE SCHEMA` on the target schema and `USE CATALOG` on the parent, plus `SELECT` on the source. Ownership, comment, and tags on the metric view object are set with the usual `ALTER VIEW ... OWNER TO`, `COMMENT ON`, and tag DDL. A notable limitation: metric views do **not** support OpenSharing/Delta Sharing or data profiling, so they cannot be published as a share-based data product.

### 5.4 Synchronizable vs. read-only for the bridge

- **Writable / synchronizable** — the whole YAML model (source, joins, filter, fields/dimensions and their `expr`, measures and their `expr`, window measures, materialization) and all agent metadata (`display_name`, `synonyms`, `format`, per-field/measure `comment`, governed tags), plus the view's own `comment`, `owner`, tags, and grants. **Key constraint:** `ALTER VIEW` rewrites the complete definition, so a sync must **read the current YAML, modify, and write the full spec back** — it cannot patch a single field or measure in place. Note also that saving spec `1.1` strips inline `#` comments from the YAML.
- **Read-only** — computed metric values / query results, lineage (system-derived), and audit/system fields on the view object. And because OpenSharing is unsupported, the semantic layer cannot be surfaced to external recipients as a Delta Sharing data product.

For a two-way bridge this makes metric views the natural carrier of **business semantics** (metric definitions, friendly names, synonyms, formatting) that map onto a partner catalog's measures/dimensions and business glossary — treated as a fourth metadata surface alongside UC object metadata, share/recipient metadata, and Marketplace listing metadata.

---

# Citations

- https://docs.databricks.com/aws/en/data-governance/unity-catalog/securable-objects — Unity Catalog securable objects reference (object hierarchy, metastore/catalog/schema/table/view/volume/function/model/service/secret/share/recipient/provider/clean room definitions and metadata).
- https://docs.databricks.com/aws/en/data-governance/unity-catalog/ — What is Unity Catalog? (governance overview, three-level namespace).
- https://docs.databricks.com/aws/en/catalogs/ — What are catalogs in Databricks? (catalog concepts, default/information_schema).
- https://docs.databricks.com/aws/en/catalogs/manage-catalog — Manage catalogs (updating owner, tags, comments, ALTER CATALOG).
- https://docs.databricks.com/aws/en/database-objects/tags — Apply tags to Unity Catalog securable objects (supported objects, constraints, governed/system tags, SET TAGS/UNSET TAGS, INFORMATION_SCHEMA tag tables).
- https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-set-tag — SET TAG / UNSET TAG SQL syntax.
- https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-table — ALTER TABLE (SET TAGS, OWNER TO, TBLPROPERTIES, column comments).
- https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-lineage — Lineage in Unity Catalog (system tables, external metadata custom lineage).
- https://docs.databricks.com/api/workspace/catalogs — Catalogs API (create/list/get/update/delete, PATCH fields).
- https://docs.databricks.com/api/workspace/catalogs/update — Update a catalog (owner, comment, new_name, properties, isolation_mode).
- https://docs.databricks.com/api/workspace/shares/create — Shares API (create share, comment, data objects).
- https://docs.databricks.com/aws/en/opensharing/create-share — Create shares for OpenSharing (share objects, ALTER SHARE, PATCH /shares).
- https://docs.databricks.com/aws/en/delta-sharing/create-recipient — Create data recipients for Delta Sharing.
- https://docs.databricks.com/aws/en/opensharing — What is OpenSharing? (Databricks-to-Databricks sharing protocol).
- https://docs.databricks.com/api/workspace/providerlistings/create — Create a Marketplace listing (Provider Listings API, listing/detail/summary fields, marketplace scope).
- https://docs.databricks.com/aws/en/marketplace/create-listing — Create a Marketplace listing (listing metadata, shares as products).
- https://docs.databricks.com/api/workspace/consumerlistings — Consumer Listings API (discover/list listings).
- https://docs.databricks.com/aws/en/dev-tools/auth/oauth-m2m — Authorize service principal access with OAuth (client credentials, 1-hour tokens, workspace vs account scope).
- https://docs.databricks.com/aws/en/dev-tools/auth/pat — Authenticate with Databricks personal access tokens (legacy).
- https://docs.databricks.com/aws/en/dev-tools/auth/ — Authorize access to Databricks resources (base URLs, workspace vs account APIs).
- https://docs.databricks.com/api/workspace/introduction — Databricks REST API reference (endpoint versions, pagination, rate limiting).
- https://docs.databricks.com/aws/en/business-semantics/ — Unity Catalog business semantics (the semantic layer; two components: metric views and agent metadata).
- https://docs.databricks.com/aws/en/business-semantics/metric-views/ — Unity Catalog metric views (core implementation of business semantics; separates measures from fields/dimensions; define once, query at runtime).
- https://docs.databricks.com/aws/en/business-semantics/metric-views/create — Create a metric view (CREATE VIEW ... WITH METRICS LANGUAGE YAML, UI/YAML/Genie Code, prerequisites and privileges, runtime requirements, wildcards).
- https://docs.databricks.com/aws/en/business-semantics/metric-views/yaml-reference — Metric view YAML syntax reference (version, source, joins, filter, fields/dimensions, measures, window measures, materialization).
- https://docs.databricks.com/aws/en/business-semantics/metric-views/manage — Manage metric views (SELECT to query, owner-only edit, ALTER VIEW full-definition replacement, group ownership for collaborative editing, DROP VIEW, GRANT, no OpenSharing/profiling).
- https://docs.databricks.com/aws/en/business-semantics/agent-metadata — Agent metadata in metric views (display names, synonyms up to 10, format specifications, governed tags; auto-populates AI/BI dashboards and Genie Spaces).
- https://docs.databricks.com/aws/en/business-semantics/metric-views/bi-tools — Use metric views with external BI tools (Power BI, Tableau, Sigma).
- https://docs.databricks.com/aws/en/partners/bi/bi-metric-view — Use BI compatibility mode to query metric views.
- https://docs.databricks.com/integrations/partner-connect/semantic-layer.html — Connect to semantic layer partners using Partner Connect.
