---
type: "Research Output"
title: "Snowflake Horizon Catalog & Data Products — API Reference"
description: "Detailed reference of Snowflake catalog assets, data products, and the read/write API surface for metadata synchronization."
tags: ["research", "RS-05", "snowflake", "horizon", "api"]
timestamp: "2026-08-06T08:00:00Z"
status: "draft"
---

# Snowflake Horizon Catalog & Data Products — API Reference

This reference describes how Snowflake manages catalog metadata and data products, and the concrete API surface available for reading and writing that metadata. It is written to support the QLabs Catalog Sync bridge, so it emphasizes which fields are synchronizable (readable AND writable via API), which are read-only, and what identity keys are available for matching.

Snowflake has no single dedicated "catalog metadata" REST endpoint. Instead, catalog metadata is managed through three overlapping surfaces: SQL DDL/DML (the authoritative surface), the metadata/introspection views (`INFORMATION_SCHEMA` and the `SNOWFLAKE.ACCOUNT_USAGE` share), and the resource-oriented Snowflake REST APIs plus the SQL REST API. Understanding all three is required to build a sync bridge.

---

## 1. Catalog assets (the Horizon Catalog governance layer)

### 1.1 What Horizon Catalog is

Snowflake Horizon Catalog is Snowflake's governance and discovery layer over all data, whether managed inside Snowflake or reached through open formats. It bundles the metadata and governance capabilities that were previously described piecemeal: sensitive data classification, data quality monitoring, end-to-end (column-level) lineage, object tagging, auto-generated descriptions, semantic views, and access control. A defining architectural point is that governance policies (masking, row access, tag-based masking) execute at the query-engine layer, so they apply uniformly to any caller — human, BI tool, AI agent — and follow data even into Iceberg tables and shared data products.

Horizon is not a separate metadata store you call; it is the governance behavior layered on top of the ordinary object model. For sync purposes you therefore read and write catalog metadata through the standard object DDL, the metadata views, and the REST resource APIs described below.

### 1.2 The core object hierarchy (securable objects)

Snowflake organizes data objects in a strict container hierarchy that also forms the namespace for fully-qualified names:

- **Account** — top-level container.
- **Database** — top container inside the account.
- **Schema** — logical grouping inside a database.
- **Schema-level objects** — tables (standard, transient, external, dynamic, event, Apache Iceberg), views (standard, materialized, secure, semantic), plus stages, sequences, functions, procedures, streams, tasks, tags, and policies.
- **Columns** — attributes of tables and views.

The fully-qualified name (FQN) of a schema-level object is `DATABASE.SCHEMA.OBJECT`; a column is addressed as `DATABASE.SCHEMA.TABLE.COLUMN`. This FQN is the primary matching/identity key for any catalog sync of structural objects.

### 1.3 Metadata attached to objects

Each object carries several kinds of metadata that a catalog sync cares about:

- **Comments / descriptions** — a free-text `COMMENT` on the object and, separately, on each column. This is the primary human-facing description field and is fully read/write via SQL (see section 3).
- **Object tags** — schema-level `TAG` objects assigned to other objects as key/value pairs (the tag is the key; an arbitrary string up to 256 chars is the value). Tags are the main structured classification mechanism. A tag can restrict its values with `ALLOWED_VALUES` (max 5,000 values), and (Enterprise edition) can auto-propagate along dependencies or data movement.
- **Ownership** — every object has an owning role (the role with the OWNERSHIP privilege). Owner is metadata you can read; changing it is a privilege grant (`GRANT OWNERSHIP`), not a free-text edit.
- **Data classification categories** — for columns identified as sensitive, Snowflake assigns system tags `SNOWFLAKE.CORE.SEMANTIC_CATEGORY` (type of attribute, e.g. NAME) and `SNOWFLAKE.CORE.PRIVACY_CATEGORY` (IDENTIFIER / QUASI_IDENTIFIER / SENSITIVE). Classification can auto-apply these tags and can be mapped to user-defined tags.
- **Policies** — masking policies, row-access policies, and tag-based masking policies are attached governance objects. These are governance objects rather than descriptive metadata; a metadata sync typically records their presence/name but does not two-way sync policy logic.
- **Auto-generated descriptions** — Snowflake Cortex can generate table and column documentation from metadata and sample data, which then lands in the ordinary comment fields.

### 1.4 The two metadata read surfaces: INFORMATION_SCHEMA vs ACCOUNT_USAGE

Snowflake exposes object metadata through two query-able schemas. Both are read-only.

- **`INFORMATION_SCHEMA`** — a per-database, real-time set of views (e.g. `TABLES`, `COLUMNS`, `VIEWS`, `SCHEMATA`, `TABLE_CONSTRAINTS`). No latency, but scoped to the current/target database and with limited retention. Best for "read current truth for one database" during a sync scan.
- **`SNOWFLAKE.ACCOUNT_USAGE`** — an account-wide shared schema of views covering the whole account, including dropped objects, with historical retention. It has some latency (typically up to ~2 hours for many views) but is the surface for account-wide discovery. Relevant views include `DATABASES`, `SCHEMATA`, `TABLES`, `VIEWS`, `COLUMNS`, `TAGS`, `TAG_REFERENCES`, and grant views. `TAG_REFERENCES` is the key one for tags: it lists each tag assignment with `TAG_NAME`, `TAG_VALUE`, `OBJECT_DATABASE`, `OBJECT_SCHEMA`, `OBJECT_NAME`, `DOMAIN`, and `COLUMN_NAME`.

The trade-off for sync: `INFORMATION_SCHEMA` for freshness on a targeted object; `ACCOUNT_USAGE` for breadth and change detection across the account (subject to latency).

---

## 2. Data products

### 2.1 What a "data product" is in Snowflake

In the Snowflake ecosystem a **data product** is the thing attached to a **listing** — either a **share** (a set of database objects exposed for Secure Data Sharing) or a **Snowflake Native App** (application package). Listings are the productized wrapper that adds discoverability and go-to-market metadata on top of raw sharing.

Three layers stack up:

1. **Secure Data Sharing / Share** — the underlying access mechanism. A `SHARE` is a named container; you `GRANT` database objects (databases, schemas, tables, views) into it and add consumer accounts. Sharing is by reference (no data copy). The consumer creates a database "from share".
2. **Listing** — an enhanced form of Secure Data Sharing using the same provider/consumer model, but adding a title, subtitle, description, categories, business needs, sample queries, a data dictionary, provider profile, terms, pricing/offers, and usage attributes. The listing's data product is the share (or app) it points at.
3. **Marketplace / private / organizational surfaces** — a listing can be published publicly on the **Snowflake Marketplace**, privately to specific target accounts (a private listing), or across an organization via the **Internal Marketplace / organizational listing** (share governed data products across teams without copying data).

### 2.2 How listings relate to underlying objects and metadata

When a provider selects one or more database objects for a listing, Snowflake creates a **secure share** containing those objects. The listing's descriptive metadata is defined by a **YAML manifest**, which references the underlying objects in its `data_dictionary` section (each entry names a `database`, `schema`, object `name`, and `domain` such as TABLE / VIEW / SCHEMA / DATABASE / COLUMN). So the listing carries product-level metadata (title, description, business needs, categories, data dictionary, usage examples, refresh rate, geography/time coverage, PII flags, compliance badges) while the share carries the actual objects and their object-level metadata (comments, tags).

Governance travels with the product: shared data products carry their tags and permissions, and masking/row-access policies continue to enforce on the consumer side. (Note: a consumer cannot run classification on shared/consumer-side tables; classification only runs on the provider side.)

### 2.3 V1 vs V2 listings

- **V1 listings** use a `targets` field, targeting individual account names (`Org1.Account1`). Compatible with all accounts; no pricing plans/offers.
- **V2 listings** use `external_targets` (organizations, accounts, accounts-with-roles, `all_organizations: true` for public) and `locations` (`access_regions`), and support pricing plans and offers.

### 2.4 Identity keys for data products

- A share is identified by its name, and cross-account by `<provider_account>.<share_name>`.
- A listing name must be unique within an organization across regions. Every listing also has a **global name** (returned by `SHOW LISTINGS`, column `global_name`) — a stable, globally-unique identifier that is the best matching key for a listing across accounts/regions. Consumers reference marketplace/private listings by this global name.

---

## 3. API mechanics

### 3.1 API surfaces overview

| Surface | Base | Use for catalog sync |
| --- | --- | --- |
| SQL DDL/DML | executed via any driver, Snowsight, SnowSQL, or the SQL REST API | Authoritative create/update of comments, tags, shares, listings. Widest coverage. |
| SQL REST API (`/api/v2/statements`) | `https://<account>.snowflakecomputing.com` | Run arbitrary SQL over HTTP — the universal fallback for any metadata read/write. |
| Snowflake REST APIs (resource management) | `https://<account>.snowflakecomputing.com/api/v2/...` | Typed CRUD for `databases`, `schemas`, `tables`, `views`, `tags`, etc. Not yet 100% coverage of SQL. |
| Snowflake Python API / Snowpark | Python | Programmatic wrappers around the same resources; convenient for engine code. |

The account URL base is always `https://<account_identifier>.snowflakecomputing.com` (the `account_identifier` is the org-account name or account locator).

### 3.2 Authentication

All REST surfaces (both the SQL REST API and the resource REST APIs) share the same auth model. Requests carry an `Authorization: Bearer <token>` header and an optional `X-Snowflake-Authorization-Token-Type` header. Supported token types:

- **Key-pair (JWT)** — assign an RSA public key to the Snowflake user (`RSA_PUBLIC_KEY_FP` shows the fingerprint via `DESCRIBE USER`). Generate a short-lived JWT whose payload has `iss = <account>.<user>.SHA256:<fingerprint>`, `sub = <account>.<user>`, plus `iat`/`exp`. A JWT is valid at most one hour. Header: `X-Snowflake-Authorization-Token-Type: KEYPAIR_JWT`. This is the recommended approach for an unattended sync service.
- **OAuth** — obtain an OAuth access token (Snowflake OAuth or external IdP); send it as the bearer token with type `OAUTH`.
- **Programmatic Access Token (PAT)** — a long-lived token secret sent as the bearer token with type `PROGRAMMATIC_ACCESS_TOKEN`. Simplest for scripts.
- **Workload Identity Federation (WIF)** — cloud/OIDC attestation; bearer value `WIF.{provider}.{token}` with type `WORKLOAD_IDENTITY_FEDERATION`.

Example (PAT) hitting the databases resource endpoint:

```bash
curl --location 'https://myorganization-myaccount.snowflakecomputing.com/api/v2/databases' \
  --header 'Authorization: Bearer <token_secret>' \
  --header 'X-Snowflake-Authorization-Token-Type: PROGRAMMATIC_ACCESS_TOKEN'
```

### 3.3 Comments / descriptions — CRUD

Comments are the primary description field and are fully read/write.

Create/update at object creation or via ALTER:

```sql
-- at creation
CREATE SCHEMA my_schema COMMENT = 'Finance conformed dimensions';
CREATE OR REPLACE TABLE sales (id INT COMMENT 'primary key') COMMENT = 'Daily sales fact';

-- update an existing object comment
COMMENT ON TABLE my_db.my_sch.sales IS 'Daily sales fact (v2)';
-- update a column comment
COMMENT ON COLUMN my_db.my_sch.sales.id IS 'Surrogate key';

-- update via ALTER also works
ALTER TABLE my_db.my_sch.sales SET COMMENT = 'Daily sales fact';
```

Delete = set to NULL/empty:

```sql
COMMENT ON TABLE my_db.my_sch.sales IS '';
ALTER TABLE my_db.my_sch.sales UNSET COMMENT;
```

Read back:

```sql
-- fast, single object
SHOW TABLES LIKE 'SALES' IN SCHEMA my_db.my_sch;            -- 'comment' column
DESCRIBE TABLE my_db.my_sch.sales;                          -- column comments
-- account-wide
SELECT table_catalog, table_schema, table_name, comment
FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES;
SELECT table_catalog, table_schema, table_name, column_name, comment
FROM SNOWFLAKE.ACCOUNT_USAGE.COLUMNS;
```

Note: Snowflake warns that object metadata (comments/descriptions) must not contain personal, sensitive, export-controlled, or regulated data.

### 3.4 Tags — CRUD (classification metadata)

Create/alter the tag object:

```sql
CREATE TAG governance.tags.cost_center;
CREATE TAG governance.tags.cost_center ALLOWED_VALUES 'finance', 'engineering';
ALTER TAG governance.tags.cost_center ADD ALLOWED_VALUES 'marketing';
ALTER TAG governance.tags.cost_center DROP ALLOWED_VALUES 'engineering';
CREATE TAG data_sensitivity PROPAGATE = ON_DEPENDENCY;              -- Enterprise
```

Assign / update / unset a tag value on an object or column:

```sql
-- at creation
CREATE WAREHOUSE wh1 WITH TAG (cost_center = 'sales');
-- assign / update on existing object (SET overwrites the value)
ALTER TABLE hr.tables.empl_info SET TAG cost_center = 'marketing';
-- on a column
ALTER TABLE hr.tables.empl_info MODIFY COLUMN job_title SET TAG cost_center = 'marketing';
-- unset (remove) the assignment
ALTER TABLE hr.tables.empl_info UNSET TAG cost_center;
```

Read back tag assignments:

```sql
-- current value on one object
SELECT SYSTEM$GET_TAG('cost_center', 'hr.tables.empl_info', 'TABLE');
-- allowed values
SELECT SYSTEM$GET_TAG_ALLOWED_VALUES('governance.tags.cost_center');
-- all assignments for an object/lineage (INFORMATION_SCHEMA table functions)
SELECT * FROM TABLE(my_db.INFORMATION_SCHEMA.TAG_REFERENCES('hr.tables.empl_info', 'TABLE'));
-- account-wide, with some latency
SELECT tag_name, tag_value, object_database, object_schema, object_name, domain, column_name
FROM SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES;
```

Delete a tag object: `DROP TAG governance.tags.cost_center;` (24-hour grace period; `UNDROP TAG` restores it and its assignments).

Privileges: creating a tag needs CREATE TAG on the schema; setting/unsetting needs APPLY TAG on the account, or APPLY on the specific tag plus OWNERSHIP of the target object.

### 3.5 Shares — CRUD (the data-product substrate)

```sql
-- create
CREATE SHARE sales_s COMMENT = 'Sales data share';
-- add objects (grant into the share)
GRANT USAGE ON DATABASE sales_db TO SHARE sales_s;
GRANT USAGE ON SCHEMA sales_db.public TO SHARE sales_s;
GRANT SELECT ON TABLE sales_db.public.orders TO SHARE sales_s;
-- add / remove consumer accounts
ALTER SHARE sales_s ADD ACCOUNTS = org1.consumer_account;
-- create-or-alter (preview) supports comment upsert; cannot add/remove accounts or tags
CREATE OR ALTER SHARE sales_s COMMENT = 'Sales data share for consumers';
-- read
SHOW SHARES;
DESCRIBE SHARE sales_s;
-- delete
DROP SHARE sales_s;
```

CREATE SHARE requires the CREATE SHARE account privilege (ACCOUNTADMIN by default).

### 3.6 Listings — CRUD (the data product itself)

Listings are created from an inline YAML manifest (dollar-quoted) or from a manifest file in a stage/Git repo. A listing attaches either a `SHARE` or an `APPLICATION PACKAGE`.

Create (private listing, publish on approval):

```sql
CREATE EXTERNAL LISTING my_listing
SHARE sales_s AS
$$
title: "Sales data"
subtitle: "Daily sales by region"
description: "Sales fact tables refreshed daily."
listing_terms:
  type: "STANDARD"
targets:
  accounts: ["Org1.Account1"]
data_dictionary:
  featured:
    database: "SALES_DB"
    objects:
      - name: "ORDERS"
        schema: "PUBLIC"
        domain: "TABLE"
usage_examples:
  - title: "Total sales by region"
    description: "Aggregate example"
    query: "SELECT region, SUM(amount) FROM orders GROUP BY region"
$$ PUBLISH = TRUE REVIEW = TRUE;
```

Draft (no review, no publish): append `$$ PUBLISH=FALSE REVIEW=FALSE;`. From a stage: `... SHARE sales_s FROM '@db.public.listingstage/manifests';`.

The `PUBLISH`/`REVIEW` matrix: `TRUE/TRUE` = review then publish; `TRUE/FALSE` = error (cannot publish to Marketplace without review); `FALSE/TRUE` = review without auto-publish; `FALSE/FALSE` = draft.

Update / read / delete:

```sql
ALTER LISTING my_listing SET ...          -- edit manifest, publish/unpublish
ALTER LISTING my_listing UNPUBLISH;
SHOW LISTINGS;                            -- includes the global_name column
DESCRIBE LISTING my_listing;
SHOW VERSIONS IN LISTING my_listing;
DROP LISTING my_listing;
```

Manifest fields most relevant to catalog metadata sync (writable via the manifest): `title`, `subtitle`, `description` (Markdown, up to 7,500 chars), `categories` (single value from a fixed set), `business_needs`, `data_attributes` (refresh_rate, geography, time coverage), `data_dictionary` (featured objects), `data_preview` (PII flags), `resources` (documentation/media links), `usage_examples`, `compliance_badges`, `trial_details`, and (V2) `pricing_plans`/`offers`. Organizational (Internal Marketplace) listings use `CREATE ORGANIZATION LISTING` and do not support pricing plans/offers.

CREATE LISTING requires the CREATE LISTING account privilege (ACCOUNTADMIN by default).

### 3.7 Resource REST APIs (typed CRUD)

The Snowflake REST APIs expose OpenAPI-described endpoints under `/api/v2/`, supporting `CREATE OR ALTER` semantics for many resources. Examples relevant to catalog objects: `databases`, `schemas`, `tables`, `views`, `dynamic-tables`, `iceberg-table`, and `tags`. The database resource illustrates the pattern:

```
GET    /api/v2/databases                 -- list (query params: like, startsWith, showLimit, fromName, history)
POST   /api/v2/databases                 -- create (createMode: errorIfExists|orReplace|ifNotExists)
GET    /api/v2/databases/{name}          -- fetch one
PUT    /api/v2/databases/{name}          -- create-or-alter (full definition required)
DELETE /api/v2/databases/{name}          -- drop (ifExists param)
POST   /api/v2/databases:from-share      -- create a database from a share (?share=<provider>.<share>)
```

The object body carries `comment` and (where applicable) tag/property fields, so descriptions are settable through these typed endpoints too. Pagination/filtering on list endpoints is via `like`, `startsWith`, `fromName`, and `showLimit`. Coverage note: not every SQL capability is available in the REST APIs yet; listings and shares in particular are most reliably managed through SQL (directly or via the SQL REST API). OpenAPI specs are published in the `snowflakedb/snowflake-rest-api-specs` GitHub repo.

### 3.8 SQL REST API (`/api/v2/statements`) — universal fallback

Any SQL statement (including all the DDL above) can be executed over HTTP by POSTing to `/api/v2/statements`. This is the most complete surface because it can run anything SQL can:

```bash
curl -i -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <jwt>" \
  -H "Accept: application/json" \
  "https://<account>.snowflakecomputing.com/api/v2/statements"
```

Request body:

```json
{
  "statement": "ALTER TABLE my_db.my_sch.sales SET TAG cost_center = 'marketing'",
  "timeout": 60,
  "database": "MY_DB",
  "schema": "MY_SCH",
  "warehouse": "MY_WH",
  "role": "TAG_ADMIN"
}
```

Notes for a sync engine: `database`/`schema`/`warehouse`/`role` values are case-sensitive and must match the stored (usually uppercase) identifier case; supply `?async=true` for long-running statements and be ready to poll status; use `?requestId=<uuid>&retry=true` for idempotent retries so a resubmitted DDL is not executed twice; expect HTTP 429 under load and wrap in retry logic; bind variables are supported via a `bindings` object (not in multi-statement requests).

---

## 4. Relevance to sync (writable vs read-only, matching keys)

### 4.1 Synchronizable (readable AND writable via API)

These are the fields a two-way bridge can push into Snowflake and read back:

- **Object comments / descriptions** — tables, views, schemas, databases, and individual columns. Write via `COMMENT ON ... IS`, `ALTER ... SET COMMENT`, or the resource API body; read via `SHOW`/`DESCRIBE`, `INFORMATION_SCHEMA`, or `ACCOUNT_USAGE`. Fully bidirectional.
- **Tags and tag values** — create tags (`CREATE TAG`), assign/update (`ALTER ... SET TAG`), unset (`UNSET TAG`); read via `SYSTEM$GET_TAG`, `INFORMATION_SCHEMA.TAG_REFERENCES`, and `ACCOUNT_USAGE.TAG_REFERENCES`. Fully bidirectional and the best structured channel for classification/business metadata. Tag allowed-value lists are also writable.
- **Data-product (listing) metadata** — title, subtitle, description, categories, business needs, data attributes (refresh rate, geography/time coverage), data dictionary, usage examples, resources (doc/media links), compliance badges, trial details, and V2 pricing/offers. All writable through the listing YAML manifest (`CREATE LISTING` / `ALTER LISTING`) and readable via `SHOW`/`DESCRIBE LISTING`.
- **Share composition and comment** — objects in a share (via `GRANT ... TO SHARE`), consumer accounts (via `ALTER SHARE`), and the share comment. Writable and readable.
- **Structural objects** — databases/schemas/tables/views can be created/altered/dropped via SQL or resource REST APIs, so the catalog structure itself is writable when the bridge is authoritative for structure.

### 4.2 Read-only (or effectively read-only) for a metadata sync

- **System classification tags** (`SNOWFLAKE.CORE.SEMANTIC_CATEGORY`, `SNOWFLAKE.CORE.PRIVACY_CATEGORY`) — produced by Snowflake's classification engine. You can read them and map them to your own user-defined tags, but you should treat the system values as machine-generated rather than something to overwrite.
- **Lineage** — column/table lineage is derived and exposed for reading (Snowsight / views); it is not a field you write.
- **Ownership / grants** — readable as metadata; changed only through privilege operations (`GRANT OWNERSHIP`, `GRANT ...`), not as free-text sync.
- **Timestamps, row counts, bytes, created_on, last_altered** — system-maintained; read-only.
- **Listing operational state** (global name, publish status, consumer usage/interest metrics) — read-only outputs; publish/unpublish is controllable but the identifiers and metrics are system-owned.

### 4.3 Identity / matching keys

- **Structural objects:** fully-qualified name `DATABASE.SCHEMA.OBJECT` (and `...TABLE.COLUMN` for columns). In `ACCOUNT_USAGE`/`INFORMATION_SCHEMA` these decompose into `TABLE_CATALOG` / `TABLE_SCHEMA` / `TABLE_NAME` / `COLUMN_NAME`. Objects also have internal numeric IDs in some `ACCOUNT_USAGE` views, useful for detecting renames.
- **Tags:** the tag object's own FQN (`db.schema.tag`) plus, per assignment, the `(object domain, object FQN, column)` tuple in `TAG_REFERENCES`.
- **Shares:** share name locally; `<provider_account>.<share_name>` cross-account.
- **Listings:** listing name (unique within the organization) and, decisively for cross-account matching, the listing **global name** from `SHOW LISTINGS` (`global_name`).
- **Accounts/orgs:** `OrgName.AccountName` (from `CURRENT_ORGANIZATION_NAME()` and `SHOW ACCOUNTS`) is the target key used in listing manifests and share targeting.

### 4.4 Practical sync notes

- Prefer SQL (directly or via `/api/v2/statements`) as the write path — it has the widest coverage; use typed `/api/v2/...` resource endpoints where they exist and coverage is sufficient.
- Use `INFORMATION_SCHEMA` for fresh single-database reads and `ACCOUNT_USAGE` for account-wide discovery and change detection, accounting for `ACCOUNT_USAGE` latency.
- Make writes idempotent: `SET TAG` overwrites, `COMMENT ... IS` overwrites, `CREATE OR ALTER` upserts shares/objects, and the SQL REST API `requestId`+`retry` prevents duplicate execution.
- Listings are versioned (`SHOW VERSIONS IN LISTING`) and pass through a review/publish lifecycle; a sync that manages listings must model draft vs published state, not just field values.

---

## 5. Semantic layer

Snowflake's semantic layer is the part of Horizon that stores *business meaning* — the mapping from physical columns to business entities, metrics, and terminology — so that BI tools and AI (Cortex Analyst) share a single authoritative definition of concepts such as "net revenue." For a catalog-sync bridge this is a rich source of curated, human-authored metadata (business names, synonyms, descriptions, metric formulas) that is largely writable. It has two closely related forms: the native **Semantic View** schema object, and the older **Cortex Analyst semantic model** YAML file kept in a stage.

### 5.1 Semantic Views (the native, recommended form)

A **Semantic View** is a first-class, schema-level object (FQN `DATABASE.SCHEMA.SEMANTIC_VIEW`). Because it lives in the database, it participates in Snowflake's privilege system, tagging, `SHOW`/`DESCRIBE`, the metadata views, and Secure Data Sharing (semantic views can be shared in private, Marketplace, and organizational listings). Snowflake explicitly classifies a semantic view's definition as **metadata**. It can be consumed two ways: by **Cortex Analyst** for natural-language questions, and directly in SQL via a `SELECT ... FROM SEMANTIC_VIEW( <name> METRICS (...) DIMENSIONS (...) )` construct.

A semantic view holds these logical objects (declared inside `CREATE SEMANTIC VIEW`):

- **Logical tables (`TABLES`)** — business entities (customer, order, supplier) mapped to physical tables/views. Each logical table can carry an optional alias, `PRIMARY KEY` / `UNIQUE` constraints (used to define joins), a `COMMENT`, object `TAG`s, and `WITH SYNONYMS` (alternate business names, informational only).
- **Relationships (`RELATIONSHIPS`)** — join definitions between logical tables on shared keys (including ASOF and range-join variants). These make cross-entity analysis possible.
- **Facts (`FACTS`)** — row-level numeric attributes (e.g. individual sale amount) defined by a SQL expression; typically "helper" values used to build metrics.
- **Dimensions (`DIMENSIONS`)** — categorical/contextual attributes (who/what/where/when) defined by a SQL expression; dimensions are always public and can optionally attach a Cortex Search Service for literal search.
- **Metrics (`METRICS`)** — aggregated KPIs (e.g. `SUM(gross_revenue * (1 - discount))`), defined by a SQL expression, including window-function and semi-additive (`NON ADDITIVE BY`) variants; metrics can be marked `PRIVATE` or `PUBLIC`.

Facts, dimensions, and metrics each also accept `WITH SYNONYMS`, a per-element `COMMENT`, and `TAG`s. At the view level the definition additionally carries a `COMMENT`, `MAX_STALENESS` (for materializations), Cortex Analyst custom instructions (`AI_SQL_GENERATION`, `AI_QUESTION_CATEGORIZATION`), verified queries (`AI_VERIFIED_QUERIES`, each pairing a natural-language question with a trusted SQL answer), view-level `TAG`s, and `COPY GRANTS`. In short, the synonyms, comments/descriptions, and metric/dimension definitions are exactly the kind of business metadata a catalog wants to synchronize.

### 5.2 SQL / API surface for CRUD

Semantic views are managed through SQL DDL (directly, or over HTTP through the SQL REST API `/api/v2/statements`; the Snowflake Python and REST APIs also expose them):

- **Create / replace** — `CREATE [OR REPLACE] SEMANTIC VIEW <name> TABLES (...) [RELATIONSHIPS (...)] [FACTS (...)] [DIMENSIONS (...)] [METRICS (...)] [COMMENT=...] [...]`. Requires `CREATE SEMANTIC VIEW` on the schema plus `SELECT` on every underlying table/view.
- **Upsert the whole definition** — `CREATE OR ALTER SEMANTIC VIEW` adds/removes/modifies tables, relationships, facts, dimensions, metrics, the comment, the AI custom instructions, and verified queries in place. Caveat: it does **not** add or change tags (existing tags are preserved), and any previously-set property omitted from the statement is unset. Requires `OWNERSHIP`.
- **Targeted alter** — `ALTER SEMANTIC VIEW` can only rename the view, set/unset `COMMENT` and `MAX_STALENESS`, set/unset `TAG`s, and manage materializations. It **cannot** change tables, relationships, facts, dimensions, or metrics — those require `CREATE OR ALTER` or `CREATE OR REPLACE`.
- **Read / introspect** — `DESCRIBE SEMANTIC VIEW`, `SHOW SEMANTIC VIEWS`, and the element-level `SHOW SEMANTIC DIMENSIONS` / `SHOW SEMANTIC METRICS` / `SHOW SEMANTIC FACTS` (and `SHOW SEMANTIC DIMENSIONS FOR METRIC`).
- **Delete** — `DROP SEMANTIC VIEW`.
- **Metadata views (read-only)** — both `INFORMATION_SCHEMA` (per-database, live) and `SNOWFLAKE.ACCOUNT_USAGE` (account-wide, latency-bearing) expose a matching set of views: `SEMANTIC_VIEWS`, `SEMANTIC_TABLES`, `SEMANTIC_RELATIONSHIPS`, `SEMANTIC_FACTS`, `SEMANTIC_DIMENSIONS`, and `SEMANTIC_METRICS`. These are the introspection surface for reading a semantic view's structure during a sync scan.

### 5.3 The Cortex Analyst semantic model (stage YAML)

Before native semantic views existed, Cortex Analyst read a **semantic model** expressed as a **YAML file uploaded to a stage**. It captures the same concepts — logical tables mapped to physical tables, with `dimensions`, `facts`/`measures`, table-level `filters`, `synonyms`, `verified queries`, module-scoped custom instructions (`sql_generation`, `question_categorization`), and query-time `variables`. The Cortex Analyst REST API request can reference either a native `semantic_view`, a `semantic_model_file` (a stage path), or an inline `semantic_model` YAML string.

The key difference for sync: a stage-based YAML model is **just a file** — it has no native database integration, so it does not inherit Snowflake's object privileges, tagging, or metadata-view visibility. Its access is governed by access to the stage it sits in (any role that can read the stage can read the model, even without rights on the underlying tables). Snowflake now recommends **semantic views** for new work and provides the `SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML` stored procedure to convert an existing stage YAML model into a native semantic view. Stage YAML remains supported for backward compatibility.

### 5.4 Synchronizable vs read-only (semantic layer)

- **Writable / synchronizable (semantic views):** the entire logical definition via `CREATE OR REPLACE` / `CREATE OR ALTER` — logical-table mappings, relationships, facts, dimensions, metrics and their SQL expressions, per-element and view-level `COMMENT`s, `WITH SYNONYMS`, Cortex custom instructions, and verified queries. View-level and element tags are writable, but only through `CREATE ... ` / `ALTER SEMANTIC VIEW SET TAG` (not through `CREATE OR ALTER`). Business names, synonyms, descriptions, and metric definitions are the highest-value fields to push.
- **Writable (stage YAML models):** the whole YAML file — rewrite and re-upload it to the stage (or pass it inline to the Cortex Analyst REST API). There is no field-level API; the file is the unit of write.
- **Read-only for a sync:** the `SEMANTIC_*` `INFORMATION_SCHEMA` / `ACCOUNT_USAGE` views, plus system-owned attributes such as `created_on`, owner role, system-generated constraint names, and materialization/staleness state. When a semantic view is shared through a listing, its listing `global_name` is a read-only identifier.
- **Matching keys:** for semantic views, the object FQN `DATABASE.SCHEMA.SEMANTIC_VIEW`, and within it the element names (logical-table alias, and dimension/fact/metric names) plus their synonyms. For stage YAML models, the stage path and file name identify the model.

---

# Citations

- https://docs.snowflake.com/en/user-guide/snowflake-horizon — Snowflake Horizon Catalog overview: governance, classification, lineage, tagging, semantic views, cross-engine policy enforcement, and shared data products carrying tags/permissions.
- https://docs.snowflake.com/en/user-guide/object-tagging/introduction — Introduction to object tagging: tags as schema-level key/value objects and supported objects.
- https://docs.snowflake.com/en/user-guide/object-tagging/work — Working with tags: CREATE TAG, ALLOWED_VALUES, PROPAGATE, SET/UNSET TAG on objects and columns, SYSTEM$GET_TAG, DROP/UNDROP TAG, privileges.
- https://docs.snowflake.com/en/user-guide/classify-intro — Sensitive data classification: semantic/privacy categories, system tags SNOWFLAKE.CORE.SEMANTIC_CATEGORY / PRIVACY_CATEGORY, tag-based masking, provider-vs-consumer classification limits.
- https://docs.snowflake.com/en/sql-reference/sql/comment — COMMENT command: adding/overwriting comments on objects and columns; metadata warnings.
- https://docs.snowflake.com/en/sql-reference/account-usage — ACCOUNT_USAGE reference: account-wide, latency-bearing metadata views including TABLES, COLUMNS, TAG_REFERENCES.
- https://docs.snowflake.com/en/collaboration/collaboration-listings-about — About listings: data product = share or app attached to a listing; private/public/organizational availability; free/trial/paid access; V1 vs V2 listings.
- https://docs.snowflake.com/en/sql-reference/sql/create-listing — CREATE LISTING syntax: EXTERNAL LISTING, SHARE/APPLICATION PACKAGE, inline vs stage manifest, PUBLISH/REVIEW matrix, privileges.
- https://docs.snowflake.com/en/progaccess/listing-manifest-reference — Listing manifest YAML reference: title/subtitle/description, targets/external_targets, data_dictionary, data_attributes, categories, business_needs, resources, compliance_badges, pricing_plans/offers.
- https://docs.snowflake.com/en/progaccess/listing-progaccess-examples — Provider SQL examples for creating private, replicated, public, and draft listings.
- https://docs.snowflake.com/en/sql-reference/sql/create-share — CREATE SHARE and CREATE OR ALTER SHARE: share as the data-sharing container, GRANT ... TO SHARE, ALTER SHARE for accounts.
- https://docs.snowflake.com/en/developer-guide/snowflake-rest-api/snowflake-rest-api — Snowflake REST APIs overview: resource endpoints (databases, schemas, tables, views, tags, etc.), CREATE OR ALTER, coverage caveats, OpenAPI specs.
- https://docs.snowflake.com/en/developer-guide/snowflake-rest-api/reference/database — Database resource endpoints: GET/POST/PUT/DELETE /api/v2/databases, from-share, filtering/pagination parameters.
- https://docs.snowflake.com/en/developer-guide/snowflake-rest-api/authentication — REST API authentication: key-pair JWT (iss/sub/iat/exp), OAuth, PAT, workload identity federation, Authorization and X-Snowflake-Authorization-Token-Type headers, account URL base.
- https://docs.snowflake.com/en/developer-guide/sql-api/submitting-requests — SQL REST API: POST /api/v2/statements, request body fields, case-sensitivity, async, requestId/retry idempotency, bind variables, 429 handling.
- https://docs.snowflake.com/en/user-guide/views-semantic/overview — Overview of semantic views: schema-level object storing business concepts (logical tables, relationships, facts, dimensions, metrics), classified as metadata, usable in Cortex Analyst and SELECT, shareable in listings, and the INFORMATION_SCHEMA / ACCOUNT_USAGE SEMANTIC_* views.
- https://docs.snowflake.com/en/sql-reference/sql/create-semantic-view — CREATE SEMANTIC VIEW / CREATE OR ALTER SEMANTIC VIEW syntax: TABLES/RELATIONSHIPS/FACTS/DIMENSIONS/METRICS clauses, WITH SYNONYMS, per-element COMMENT and TAG, MAX_STALENESS, AI_SQL_GENERATION / AI_QUESTION_CATEGORIZATION, AI_VERIFIED_QUERIES, COPY GRANTS, and access-control requirements.
- https://docs.snowflake.com/en/user-guide/views-semantic/sql — Using SQL commands to create and manage semantic views: defining logical tables, relationships, facts, dimensions, metrics, private vs public elements, and management commands.
- https://docs.snowflake.com/en/sql-reference/sql/alter-semantic-view — ALTER SEMANTIC VIEW: limited to rename, set/unset COMMENT and MAX_STALENESS, set/unset TAG, and materialization management; cannot change tables/relationships/facts/dimensions/metrics.
- https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec — YAML specification for semantic views / Cortex Analyst semantic models: logical tables, dimensions, facts, metrics, filters, synonyms, verified queries, custom instructions, and variables.
- https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst — Cortex Analyst: natural-language querying over a semantic view or a stage YAML semantic model; stage-model access governed by stage access; semantic views recommended for new implementations.
- https://docs.snowflake.com/en/sql-reference/stored-procedures/system_create_semantic_view_from_yaml — SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML: converts a stage-based YAML semantic model into a native semantic view.
- https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/rest-api — Cortex Analyst REST API: request references a native semantic_view, a semantic_model_file stage path, or an inline semantic_model YAML string.
- https://docs.snowflake.com/en/sql-reference/account-usage/semantic_views — ACCOUNT_USAGE SEMANTIC_VIEWS view (and the companion SEMANTIC_TABLES / SEMANTIC_RELATIONSHIPS / SEMANTIC_FACTS / SEMANTIC_DIMENSIONS / SEMANTIC_METRICS views) for read-only introspection of semantic views.
