---
type: "Research Note"
title: "Databricks Access Control — Model & API"
description: "Databricks Unity Catalog authorization model, principal identity, and the read/write permission API, assessed for cross-catalog access-control sync."
tags: ["research", "RS-09", "databricks", "access-control", "authorization"]
timestamp: "2026-08-06T11:30:00Z"
status: "draft"
---

# Databricks Access Control — Model & API

This note covers only authorization in Databricks Unity Catalog (UC): the privilege
model, how principals are identified, and the read/write APIs for grants. It is written
to assess feasibility of a two-way access-control sync bridge (QLabs Catalog Sync).
General metadata modeling is out of scope here.

## Authorization Model

Unity Catalog is the account-level governance layer. Data and metadata live in a
top-level **metastore**, and objects sit in a three-level namespace
`catalog.schema.table`. Every object in the hierarchy is a **securable object**, and
access control works by granting *privileges* on these securables. UC is defined once at
the account level and shared across the workspaces attached to a metastore; this is
distinct from the older workspace-scoped ACLs (which still govern non-UC objects such as
notebooks, jobs, and clusters via a separate Permissions API).

### Securable objects and their privileges

The hierarchy has **container objects** (which have children and drive inheritance) and
leaf objects. Container objects are the **catalog** (children = schemas) and the
**schema** (children = tables, views, volumes, functions, models, etc.). The
**metastore** is the top container but behaves specially (see inheritance). Leaf/other
securables include table, view, materialized view, volume, function, model, and a set of
governance/sharing securables (external location, storage credential, service credential,
connection, share, provider, recipient, clean room, secret, external metadata, service).

Representative privileges per securable type (paraphrased from the privileges reference):

- **Metastore:** `CREATE CATALOG`, `CREATE CONNECTION`, `CREATE EXTERNAL LOCATION`,
  `CREATE STORAGE CREDENTIAL`, `CREATE SERVICE CREDENTIAL`, `CREATE SHARE`,
  `CREATE PROVIDER`, `CREATE RECIPIENT`, `CREATE CLEAN ROOM`, `MANAGE ALLOWLIST`,
  `SET SHARE PERMISSION`, `USE MARKETPLACE ASSETS`, `USE PROVIDER`, `USE RECIPIENT`,
  `USE SHARE`.
- **Catalog:** `ALL PRIVILEGES`, `USE CATALOG`, `USE SCHEMA`, `SELECT`, `MODIFY`,
  `BROWSE`, `APPLY TAG`, `MANAGE`, plus the `CREATE *` privileges (e.g. `CREATE SCHEMA`,
  `CREATE TABLE`, `CREATE FUNCTION`, `CREATE VOLUME`, `CREATE MODEL`, `EXECUTE`) that
  cascade to child schemas.
- **Schema:** `ALL PRIVILEGES`, `USE SCHEMA`, `SELECT`, `MODIFY`, `APPLY TAG`, `MANAGE`,
  plus creation privileges such as `CREATE TABLE`, `CREATE FUNCTION`, `CREATE VOLUME`,
  `CREATE MODEL`, `CREATE MATERIALIZED VIEW`, `CREATE SECRET`, `EXECUTE`, `READ VOLUME`,
  `WRITE VOLUME`.
- **Table:** `ALL PRIVILEGES`, `SELECT`, `MODIFY`, `APPLY TAG`, `MANAGE`.
- **View / materialized view:** `ALL PRIVILEGES`, `SELECT`, `APPLY TAG`, `MANAGE`
  (materialized view adds `REFRESH`).
- **Volume:** `ALL PRIVILEGES`, `READ VOLUME`, `WRITE VOLUME`, `APPLY TAG`, `MANAGE`.
- **Function / model / service:** `ALL PRIVILEGES`, `EXECUTE`, `MANAGE`
  (`APPLY TAG` and `CREATE MODEL VERSION` for models).
- **External location:** `READ FILES`, `WRITE FILES`, `CREATE EXTERNAL TABLE`,
  `CREATE EXTERNAL VOLUME`, `CREATE MANAGED STORAGE`, `EXTERNAL USE LOCATION`, `BROWSE`,
  `MANAGE`, `ALL PRIVILEGES`.
- **Storage/service credential, connection:** credential and federation privileges such
  as `READ FILES`, `WRITE FILES`, `ACCESS`, `CREATE CONNECTION`, `USE CONNECTION`,
  `CREATE FOREIGN CATALOG`, `MANAGE`.

### Usage privileges (a hard prerequisite)

`USE CATALOG` and `USE SCHEMA` are *usage* privileges: they grant no data access by
themselves but are required to traverse into child objects. To read a table a principal
needs all three of `USE CATALOG` on the catalog, `USE SCHEMA` on the schema, and `SELECT`
on the table. This matters for sync: a `SELECT` grant is meaningless in isolation, so an
effective-access comparison must account for the usage chain, not just the leaf grant.

### ALL PRIVILEGES and MANAGE

`ALL PRIVILEGES` is a shorthand that *implies* every applicable privilege for the object
type; individual privileges are not materialized. Notably it excludes
`EXTERNAL USE SCHEMA`, `EXTERNAL USE LOCATION`, and `MANAGE` to avoid accidental
exfiltration or escalation. `MANAGE` lets a principal grant/revoke, transfer ownership,
and drop an object without being the owner (and can be self-granted data access). When
granted on a container, `MANAGE` is inherited by all children.

### Ownership

Every securable has exactly one **owner** (a user, group, or service principal); the
creator is the initial owner. An owner implicitly has all capabilities on that object,
but Databricks does *not* materialize `ALL PRIVILEGES` for the owner — so ownership will
**not** appear in `SHOW GRANTS` or the grants API output. Ownership does not inherit
downward, though owning a container gives implicit `MANAGE` over its children. For sync,
owner is a separate field to reconcile out-of-band from the privilege grant list.

### Privilege inheritance

Granting a privilege on a container applies it to all current and future children: a
grant on a catalog flows to every schema, table, view, volume, and function beneath it; a
grant on a schema flows to its contained objects. **Exception:** metastore-level grants
(e.g. `CREATE CATALOG`, `CREATE EXTERNAL LOCATION`) do **not** inherit to data objects —
they are metastore-scoped operations only. This is central to sync feasibility: the same
effective access can arise from a leaf grant or from an inherited container grant, so the
grants API returns two distinct views (direct vs. effective; see Read API).

### Account vs. workspace scope

UC identities and grants are account-scoped. Access via UC also requires the workspace to
be identity-federated and the metastore attached. Row filters / column masks and dynamic
views enforce fine-grained, per-row/per-column access; these are policy functions and
views, **not** grants, so they are out-of-band for a grant-level sync and should be
flagged rather than synced.

## Principal / Identity Model

Three principal types exist, all managed at the account level and assignable to groups:

- **Users** — identified by a login **email address** (also the `userName`); backed by a
  SCIM `id` and often an `externalId` from the IdP.
- **Groups** — account groups identified by `displayName`; backed by a SCIM `id`.
  (Workspace-local groups are the legacy exception and cannot receive UC grants.)
- **Service principals** — machine identities identified by an **application ID**
  (`applicationId`, a UUID) with a human-readable `displayName`; backed by a SCIM `id`.

Identities are provisioned at the account level, ideally via **account-level SCIM** (or
automatic identity management) from an external IdP, then made available to individual
workspaces through **identity federation**. Federation means you configure a
user/group/SP once in the account console and assign it to federated workspaces, rather
than re-creating it per workspace. SCIM keeps the account in sync with the IdP as the
source of truth (deactivating users on offboarding, etc.).

**Stable identifier.** In grant payloads UC references a principal by its human-facing
name: a user's email/username, a group's display name, or a service principal's
application ID. These names are what `SHOW GRANTS` and the grants API emit and accept.
The truly immutable key is the SCIM `id` (numeric/UUID) exposed by the account SCIM
(Users/Groups/ServicePrincipals) API — emails and display names can be renamed, so for a
durable cross-system mapping the sync engine should resolve each grant principal to its
SCIM `id` (or the SP `applicationId`, which is also stable) rather than trusting the name
alone.

## Read API

Two complementary ways to enumerate grants.

### Unity Catalog Grants REST API

Direct (explicitly-set) grants on one securable:

```
GET /api/2.1/unity-catalog/permissions/{securable_type}/{full_name}
GET /api/2.1/unity-catalog/permissions/{securable_type}/{full_name}?principal={principal}
```

Effective grants, which resolve inheritance from parent containers down to the target:

```
GET /api/2.1/unity-catalog/effective-permissions/{securable_type}/{full_name}
GET /api/2.1/unity-catalog/effective-permissions/{securable_type}/{full_name}?principal={principal}
```

`securable_type` is a value such as `catalog`, `schema`, `table`, `volume`, `function`,
`external_location`, `storage_credential`, `connection`, etc. `full_name` is the
fully-qualified name (e.g. `main.sales.orders` for a table). Both return a
`privilege_assignments` array of `{ principal, privileges[] }`. The effective endpoint
additionally shows, per privilege, the securable it was inherited from — essential for
distinguishing a locally-set grant from one cascading from a catalog/schema.

### SHOW GRANTS SQL

```sql
SHOW GRANTS ON TABLE main.sales.orders;
SHOW GRANTS `user@example.com` ON SCHEMA main.sales;
SHOW GRANTS ON CATALOG main;
```

`SHOW GRANTS` lists direct grants (principal, privilege, and the securable). Remember
that owners and their implicit capabilities are **not** listed; query ownership
separately (e.g. `DESCRIBE ... EXTENDED` / information-schema / the object's `owner`
field via the catalog APIs).

## Write API

### Unity Catalog Grants REST API (PATCH)

Grants are updated with a single differential PATCH; there is no full-replace PUT:

```
PATCH /api/2.1/unity-catalog/permissions/{securable_type}/{full_name}
Content-Type: application/json

{
  "changes": [
    {
      "principal": "finance_team",
      "add": ["USE CATALOG", "USE SCHEMA", "SELECT"],
      "remove": ["MODIFY"]
    },
    {
      "principal": "user@example.com",
      "add": ["BROWSE"]
    }
  ]
}
```

Each `changes` entry names one principal and independently lists privileges to `add`
and/or `remove`. The call returns the updated set of privilege assignments for the
securable.

### GRANT / REVOKE SQL

```sql
GRANT USE CATALOG, USE SCHEMA, SELECT ON CATALOG sales TO `finance_team`;
GRANT MODIFY ON TABLE main.sales.orders TO `user@example.com`;
REVOKE SELECT ON SCHEMA main.sales FROM `finance_team`;
```

### Transactional / idempotency behavior

- The PATCH is a **delta** operation, not a declarative replace: it only touches the
  principals/privileges named in `changes`; principals not mentioned are untouched. To
  make a target match a desired state, the sync engine must first read current grants and
  compute the add/remove diff itself.
- Operations are effectively **idempotent** at the assignment level: adding a privilege a
  principal already holds, or removing one they don't have, is a no-op rather than an
  error, which is friendly to retry-based sync.
- Grants can only be set at the level where they belong; you cannot revoke an *inherited*
  privilege at a child — you must revoke it at the container where it was granted.
- Only owners, holders of `MANAGE`, or metastore/account admins can change grants, and
  the acting principal must satisfy the usage-privilege chain above the object. Grants
  reference principals by name, so the principal must already exist in the account
  (provision identities first).

## Sync Feasibility Notes

- **Cleanly readable and writable via API.** Standard UC data-object grants
  (catalog / schema / table / view / volume / function) are both fully enumerable
  (grants + effective-permissions endpoints, or `SHOW GRANTS`) and fully writable
  (PATCH, or `GRANT`/`REVOKE`). This is the sweet spot for a bidirectional bridge.
- **Natural matching key for a grant.** The tuple
  **(securable_type + full_name, principal, privilege)** uniquely identifies a grant and
  is the right join key across catalogs. For the principal element, resolve the UC-facing
  name (email / group name / SP application ID) to the stable **SCIM `id`** (or SP
  `applicationId`) so renames don't break the mapping.
- **Read direct grants, not just effective.** Sync the *direct* grant set to avoid
  double-writing inherited privileges. Use the effective endpoint only to reason about
  actual access and to detect divergence, not as the source to replicate.
- **Risk — principal identity mismatch across systems.** The single biggest hazard:
  Databricks keys on email / group name / application ID, while the peer catalog (Qlik,
  Snowflake, Collibra) uses its own identifiers. Without a reliable identity-resolution
  layer (ideally the shared IdP / SCIM `externalId`), grants can be mapped to the wrong
  principal or silently dropped.
- **Risk — privilege escalation.** Coarse privileges (`ALL PRIVILEGES`, `MANAGE`,
  container-level `SELECT`/`MODIFY`) are easy to over-translate. A naive mapping that
  promotes a narrow peer permission into a container-level UC grant would grant far more
  than intended. Model translations conservatively and require explicit mapping for
  `MANAGE` / `ALL PRIVILEGES` / write privileges.
- **Risk — inheritance ambiguity.** Because a leaf's effective access may come from a
  container grant, comparing effective access across systems can flag false diffs, and
  writing at the wrong level (leaf vs. container) can either fail to remove access or
  over-broaden it. The engine must track the *grant level*, not just the resulting
  access.
- **Out-of-band, not grant-syncable.** Ownership (single-principal, not in
  `SHOW GRANTS`), row filters / column masks, and dynamic views enforce access but are
  not privilege grants; treat them as separate reconciliation items or explicit
  non-sync flags rather than folding them into grant sync.

# Citations

- https://docs.databricks.com/aws/en/data-governance/unity-catalog/access-control/permissions-concepts
- https://docs.databricks.com/aws/en/data-governance/unity-catalog/access-control/privileges-reference
- https://docs.databricks.com/aws/en/data-governance/unity-catalog/manage-privileges/
- https://docs.databricks.com/aws/en/sql/language-manual/security-show-grant
- https://docs.databricks.com/api/workspace/grants/get
- https://docs.databricks.com/api/workspace/grants/geteffective
- https://docs.databricks.com/api/workspace/grants/update
- https://docs.databricks.com/aws/en/admin/users-groups/best-practices
- https://docs.databricks.com/aws/en/admin/users-groups/
