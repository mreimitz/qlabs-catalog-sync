---
type: "Research Note"
title: "Snowflake Access Control — Model & API"
description: "Snowflake RBAC roles/grants model, principal identity, and the read/write permission API, assessed for cross-catalog access-control sync."
tags: ["research", "RS-09", "snowflake", "access-control", "rbac"]
timestamp: "2026-08-06T11:30:00Z"
status: "draft"
---

# Snowflake Access Control — Model & API

Working note for RS-09. Focus is strictly on **authorization** — how Snowflake models,
reads, and writes access-control grants — and what that implies for a two-way catalog
sync bridge (Databricks/Qlik/Snowflake). General metadata is out of scope here.

The headline for sync design: Snowflake authorization is **role-centric**. Privileges are
granted to *roles*, and roles are granted to *users* (or other roles). Snowflake is emphatic
that there is no super-user or super-role that bypasses authorization; every action needs an
explicit grant reachable by an active role. This role indirection is the single most important
mismatch to reconcile against per-user / per-space models used elsewhere.

## 1. Authorization model (RBAC + DAC + UBAC)

Snowflake combines three models:

- **RBAC (primary):** privileges are assigned to roles; roles are assigned to users (or nested
  under other roles). This is the normal path and the one a sync bridge should treat as canonical.
- **DAC:** every securable object is owned by exactly one role (the OWNERSHIP privilege). The
  owning role can grant/revoke access on that object. Ownership defaults to the role that created
  the object and can be transferred with `GRANT OWNERSHIP`.
- **UBAC:** privileges *can* be granted directly to a user, but those direct grants are only
  honored when the session has `USE SECONDARY ROLES = ALL`. UBAC is an extension, not the norm.

**Core nouns:**

- **Securable object** — an entity access can be granted on. Access is denied unless a grant allows it.
- **Role** — an entity privileges are granted to.
- **Privilege** — a specific level of access on an object.
- **User** — a recognized identity (person or service); can also receive privileges directly (UBAC).

**Securable object hierarchy (container nesting):**

```
Organization
  └── Account
        └── Database
              └── Schema
                    └── Schema-level objects: TABLE, VIEW, MATERIALIZED VIEW, STAGE,
                        FUNCTION, PROCEDURE, SEQUENCE, STREAM, TASK, PIPE, FILE FORMAT, ...
        └── Account-level objects: WAREHOUSE, ROLE, USER, RESOURCE MONITOR,
            INTEGRATION, DATABASE, ...
```

To *use* a nested object you typically need a chain of privileges: e.g. reading a table needs
`SELECT` on the table **plus** `USAGE` on its schema **and** its database. This hierarchical
USAGE requirement matters for sync: a single "can read table X" fact in another catalog may map
to several Snowflake grants at different levels.

**Common privileges** (privilege sets are per-object-type):

- `USAGE` — permission to reference/enter a container (database, schema, warehouse, role, etc.).
- `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES` — table/view DML-ish access.
- `CREATE <object>` — create objects of a type within a container (e.g. `CREATE TABLE` on a schema).
- `MODIFY`, `MONITOR`, `OPERATE` — management-style privileges (warehouses, tasks, etc.).
- `OWNERSHIP` — full control of a single object; only one role holds it at a time.
- Global/account privileges such as `MANAGE GRANTS`, `CREATE ROLE`, `CREATE USER`, `CREATE DATABASE`.

**Grants come in two shapes:**

1. **Privilege → role/user** (`GRANT SELECT ON TABLE db.sch.t TO ROLE analyst`).
2. **Role → role/user** (`GRANT ROLE analyst TO ROLE reporting` / `... TO USER jdoe`), which
   builds the **role hierarchy**. A role higher in the hierarchy inherits all privileges of the
   roles below it. (Note: a role that *owns* another role does **not** inherit its privileges —
   inheritance only flows through the granted-role hierarchy.)

**Primary vs secondary roles.** A session has exactly one active **primary role** and any number
of active **secondary roles**. `CREATE <object>` authorization comes from the primary role only,
and new objects are owned by that primary role. For all other actions, the union of privileges
across the active primary + secondary roles (and everything they inherit) authorizes the action.

**System-defined roles** (cannot be dropped; Snowflake-granted privileges cannot be revoked):

- `ORGADMIN` / `GLOBALORGADMIN` — org-level account lifecycle and usage (GLOBALORGADMIN in the
  organization account; ORGADMIN is being phased out).
- `ACCOUNTADMIN` — top of the account; encapsulates SYSADMIN + SECURITYADMIN. Not a bypass
  super-role, but the most powerful; grant to very few users.
- `SECURITYADMIN` — holds global `MANAGE GRANTS` (can grant/revoke any grant); manages users/roles;
  inherits USERADMIN.
- `USERADMIN` — dedicated to user/role management (`CREATE USER`, `CREATE ROLE`).
- `SYSADMIN` — creates and manages warehouses, databases, and database objects; recommended parent
  of the custom-role hierarchy.
- `PUBLIC` — pseudo-role granted automatically to every user and role; objects it owns are visible
  to everyone.

**Future grants.** Instead of granting on existing objects one-by-one, a future grant pre-authorizes
privileges on objects *not yet created* in a container, e.g. `GRANT SELECT ON FUTURE TABLES IN SCHEMA
db.sch TO ROLE analyst`. At most one future grant of `OWNERSHIP` per object type is allowed. Future
grants are a distinct grant category a sync bridge must recognize — they are rules, not per-object facts.

**Managed access schemas.** In a managed access schema, object owners lose the ability to grant on
their objects; only the **schema owner** or a role with `MANAGE GRANTS` can grant. This centralizes
grant authority and changes *who* may perform the write half of a sync.

**Shares / listings.** Cross-account data sharing uses shares; grants to a share expose objects to
consumers. Database roles can be granted to a share (with restrictions — see below), and Secure Data
Sharing / Marketplace listings layer on top. Grants to shares are visible via the
`ACCOUNT_USAGE.GRANTS_TO_SHARES` view. These are a separate authorization surface from ordinary
role grants and should be treated as their own sync category if in scope.

## 2. Principal / identity model

- **Users** — identities (human or service). A user has a login, a default role, and default
  secondary roles. Users can receive role grants and (via UBAC) direct privilege grants.
- **Account roles** — roles scoped to the whole account; the normal RBAC unit. Only **account
  roles** can be *activated* in a session (as primary or secondary).
- **Database roles** — roles scoped to a single database; privileges granted to a database role
  apply to objects in that database. A database role **cannot** be activated directly in a session —
  it must be granted to an account role to take effect. Account roles cannot be granted *to* database
  roles. Database roles are named `DATABASE.ROLE_NAME` (database-qualified).
- Other role flavors exist (instance roles, application roles, system application roles, service
  roles); most are feature-specific and less relevant to a metadata sync.

**Identifier / naming semantics (critical for matching identities across catalogs):**

- Identifiers must begin with a letter and, if unquoted, contain no spaces/special characters.
- **Unquoted identifiers are stored uppercased and compared case-insensitively.** Double-quoted
  identifiers are stored verbatim and are **case-sensitive**. So `analyst`, `Analyst`, and
  `ANALYST` are the same role, but `"analyst"` is a distinct, case-sensitive name. A sync bridge
  must normalize accordingly or it will create duplicates / miss matches.
- User login lookups (LOGIN_NAME) are case-insensitive.

**SCIM provisioning.** Snowflake supports SCIM 2.0 to provision users and groups→roles from an IdP
(Okta, Microsoft Entra ID/Azure AD, or a generic/custom provisioner via a SCIM security integration).
Group→role mapping is one-to-one from IdP group to Snowflake role. Provisioner type values
(`OKTA_PROVISIONER`, `AAD_PROVISIONER`, `GENERIC_SCIM_PROVISIONER`) are case-sensitive/uppercase.
If an org provisions identities via SCIM, the IdP — not Snowflake — is often the source of truth for
user↔role membership, which the bridge should respect rather than fight.

**Key modeling difference:** in Snowflake, *permissions attach to roles, not users.* A user's
effective access is the transitive closure of the roles granted to them. Any external model that
thinks in "user X can do Y on object Z" must be projected onto Snowflake's role layer (and vice versa,
role-held privileges must be fanned out to users to answer per-user questions).

## 3. Read API (enumerating grants)

Two complementary surfaces: live SQL (`SHOW GRANTS`) and the ACCOUNT_USAGE views.

**Live, real-time — `SHOW GRANTS` family:**

```sql
-- Privileges + roles granted TO a role (the role's direct grants):
SHOW GRANTS TO ROLE analyst;

-- Roles/privileges granted TO a user (direct grants and role memberships):
SHOW GRANTS TO USER jdoe;

-- Everyone/everything that has a grant ON a specific object:
SHOW GRANTS ON TABLE mydb.myschema.mytable;
SHOW GRANTS ON DATABASE mydb;

-- Where a given role has itself been granted (upward in the hierarchy):
SHOW GRANTS OF ROLE analyst;

-- Future grants configured on a container:
SHOW FUTURE GRANTS IN SCHEMA mydb.myschema;
SHOW FUTURE GRANTS IN DATABASE mydb;
```

`SHOW GRANTS` output columns include `privilege`, `granted_on` (object type), `name` (object FQN),
`granted_to`, `grantee_name`, `grant_option`, and `granted_by`. It is real-time and authoritative,
but returns one result set per statement and requires you to know which roles/objects to enumerate
(no single "all grants in the account" SHOW command — you iterate over roles/objects).

**Bulk / analytical — ACCOUNT_USAGE views (in the shared `SNOWFLAKE` database):**

```sql
-- All privilege grants to account roles, database roles, app roles, instance roles, users:
SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES;

-- Role memberships granted to users (which roles each user has):
SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS;

-- Grants exposed via shares:
SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_SHARES;
```

Notes on the views:

- `GRANTS_TO_ROLES` is the workhorse for a full-account grant inventory (privilege, granted_on,
  name, granted_to, grantee_name, grant_option, deleted_on, etc.). It covers privilege-to-role and
  role-to-role edges.
- `GRANTS_TO_USERS` covers role→user membership only; it does **not** include direct privilege grants
  or non-account-role grants to users — use `GRANTS_TO_ROLES` for those.
- Both retain **history**: revocations set `DELETED_ON` (from NULL to a timestamp) rather than
  deleting rows; re-granting adds a new row. Filter `DELETED_ON IS NULL` for the current state.
- **Latency:** ACCOUNT_USAGE views can lag up to ~120 minutes (2 hours). For an up-to-the-second
  read, fall back to `SHOW GRANTS`. Access to these views typically requires ACCOUNTADMIN or a role
  granted the relevant SNOWFLAKE database roles.

**REST surface for reads.** There is no dedicated "grants" REST endpoint; you read grants by executing
the SQL above through the **Snowflake SQL API** (`POST /api/v2/statements`, see §4). Some object CRUD
exists under the newer Snowflake REST APIs (`/api/v2/databases`, `/api/v2/roles`, ...), but grant
enumeration in practice goes through SQL.

## 4. Write API (granting / revoking)

All grant writes are SQL DDL; there is no bespoke REST grant endpoint — you run these statements,
optionally through the SQL API.

**Create/alter principals and grant privileges:**

```sql
CREATE ROLE IF NOT EXISTS analyst;
CREATE DATABASE ROLE IF NOT EXISTS mydb.reader;

-- Privilege -> role
GRANT SELECT ON TABLE mydb.myschema.mytable TO ROLE analyst;
GRANT USAGE ON DATABASE mydb TO ROLE analyst;
GRANT USAGE ON SCHEMA mydb.myschema TO ROLE analyst;

-- Future grant (applies to objects created later)
GRANT SELECT ON FUTURE TABLES IN SCHEMA mydb.myschema TO ROLE analyst;

-- Role -> role / role -> user (build hierarchy / assign to principals)
GRANT ROLE analyst TO ROLE reporting;
GRANT ROLE analyst TO USER jdoe;

-- Revoke mirrors grant
REVOKE SELECT ON TABLE mydb.myschema.mytable FROM ROLE analyst;
REVOKE ROLE analyst FROM USER jdoe;
REVOKE SELECT ON FUTURE TABLES IN SCHEMA mydb.myschema FROM ROLE analyst; -- keeps already-granted objects

-- Ownership transfer (single-holder; special semantics)
GRANT OWNERSHIP ON TABLE mydb.myschema.mytable TO ROLE new_owner
  [ COPY CURRENT GRANTS | REVOKE CURRENT GRANTS ];
```

**Executing via the SQL REST API:**

```
POST https://<account>.snowflakecomputing.com/api/v2/statements
Authorization: Bearer <token>            # OAuth or key-pair JWT
Content-Type: application/json

{
  "statement": "GRANT SELECT ON TABLE mydb.myschema.mytable TO ROLE analyst",
  "timeout": 60,
  "warehouse": "MY_WH",
  "role": "SECURITYADMIN"
}
```

The SQL API submits statements for execution, lets you poll status, and cancel; DDL like GRANT/REVOKE
runs the same as any statement. Multi-statement requests are supported for batching.

**Write-side semantics that matter for sync:**

- **Idempotency:** `GRANT`/`REVOKE` are effectively idempotent for the *end state* — re-granting an
  existing privilege is a no-op success, and revoking a non-existent grant succeeds/no-ops. `CREATE
  ROLE IF NOT EXISTS` avoids errors on re-run. This makes convergent (desired-state) sync practical.
- **Authorization to write:** the writer role needs `MANAGE GRANTS` (default: SECURITYADMIN),
  OWNERSHIP of the object, or, in managed access schemas, must be the schema owner. Plan a dedicated
  sync service role with the minimum grant-management privileges.
- **OWNERSHIP is special:** exactly one role owns an object; transfer (not additive) semantics, with
  `COPY CURRENT GRANTS` / `REVOKE CURRENT GRANTS` controlling what happens to existing grants. Treat
  ownership changes as a distinct, higher-risk operation.
- **grant option:** `WITH GRANT OPTION` lets a grantee re-grant; carry this flag through in sync.

## 5. Sync feasibility notes

**Cleanly readable AND writable.** Privilege-to-role and role-to-role/user grants are fully readable
(`SHOW GRANTS`, `GRANTS_TO_ROLES`, `GRANTS_TO_USERS`) and fully writable (`GRANT`/`REVOKE`). The
model is symmetric and idempotent, so a desired-state reconciler is feasible. Use `SHOW GRANTS` for
low-latency reads and ACCOUNT_USAGE for bulk inventory (accepting up to ~2h lag). Future grants,
ownership, and share grants are readable/writable too but are **separate categories** that must be
modeled explicitly rather than folded into ordinary per-object grants.

**Natural matching key for a grant.** A privilege grant is uniquely identified by the tuple:

```
(granted_on/object_type, object_FQN, privilege, grantee_type, grantee_name [, grant_option])
```

e.g. `(TABLE, MYDB.MYSCHEMA.MYTABLE, SELECT, ROLE, ANALYST)`. For role memberships the key is
`(ROLE, granted_role, grantee_type, grantee_name)`. FQNs and role/user names must be
**case-normalized** using Snowflake's unquoted-uppercase vs quoted-case-sensitive rule before
matching, or the bridge will produce false diffs and duplicates. Future grants key on the container
FQN + object type + privilege + role, not a concrete object.

**Big risks / mismatches:**

- **Role-centric vs per-user/per-space (the core problem).** Snowflake attaches privileges to
  **roles**; Qlik (and space-based models) often think in terms of a *user* or a *space* having a
  capability. There is no clean 1:1 mapping. Projecting Snowflake→Qlik means computing each user's
  effective privileges by walking the role hierarchy (transitive closure) and flattening to per-user
  facts — losing the role structure. Projecting Qlik→Snowflake means *inventing* roles to represent
  each distinct permission set (or space), then granting them to users. Round-tripping is lossy and
  can create role sprawl. This asymmetry should be treated as a first-class design decision, not an
  implementation detail.
- **Principal identity mismatch.** Matching a Snowflake user/role to a Qlik/Databricks principal is
  not guaranteed: different name spaces, case-handling rules (unquoted-uppercased vs quoted vs
  external system's own casing), and the fact that Snowflake *roles* have no natural counterpart in
  a per-user system. Where SCIM provisions identities from a shared IdP, the IdP's stable ID is the
  best cross-catalog join key; otherwise email/login normalization is a fragile fallback.
- **Multi-level USAGE chains.** "Can read table X" in Snowflake implies USAGE on schema and database
  too. A naive object-level sync that copies only the table grant will produce non-functional access.
  The bridge must understand the container-USAGE dependency.
- **Ownership and managed schemas.** OWNERSHIP is single-holder and transfer-based; managed access
  schemas move grant authority to the schema owner. Both constrain *who* the sync service can act as
  and how writes behave — mis-modeling them risks failed writes or unintended ownership moves.
- **Latency and history.** ACCOUNT_USAGE lag (~2h) and its soft-delete history (`DELETED_ON`) mean a
  reconciler reading only those views can act on stale state; use `SHOW GRANTS` for authoritative
  point-in-time checks before writes, and always filter `DELETED_ON IS NULL`.
- **Future grants are rules, not facts.** They must sync as policy objects; expanding them into
  per-object grants would drift as new objects appear.

# Citations

- Overview of Access Control — https://docs.snowflake.com/en/user-guide/security-access-control-overview
- Access control privileges — https://docs.snowflake.com/en/user-guide/security-access-control-privileges
- Access control best practices — https://docs.snowflake.com/en/user-guide/security-access-control-considerations
- Configuring access control (managed access schemas, custom roles) — https://docs.snowflake.com/en/user-guide/security-access-control-configure
- GRANT <privileges> ... TO ROLE — https://docs.snowflake.com/en/sql-reference/sql/grant-privilege
- GRANT <privileges> ... TO USER — https://docs.snowflake.com/en/sql-reference/sql/grant-privilege-user
- GRANT ROLE — https://docs.snowflake.com/en/sql-reference/sql/grant-role
- GRANT OWNERSHIP — https://docs.snowflake.com/en/sql-reference/sql/grant-ownership
- REVOKE <privileges> ... FROM ROLE — https://docs.snowflake.com/en/sql-reference/sql/revoke-privilege
- SHOW GRANTS — https://docs.snowflake.com/en/sql-reference/sql/show-grants
- USE SECONDARY ROLES — https://docs.snowflake.com/en/sql-reference/sql/use-secondary-roles
- SNOWFLAKE database roles — https://docs.snowflake.com/en/sql-reference/snowflake-db-roles
- GRANTS_TO_ROLES view (ACCOUNT_USAGE) — https://docs.snowflake.com/en/sql-reference/account-usage/grants_to_roles
- GRANTS_TO_USERS view (ACCOUNT_USAGE) — https://docs.snowflake.com/en/sql-reference/account-usage/grants_to_users
- GRANTS_TO_SHARES view (ACCOUNT_USAGE) — https://docs.snowflake.com/en/sql-reference/account-usage/grants_to_shares
- Account Usage — https://docs.snowflake.com/en/sql-reference/account-usage
- Snowflake SQL API (submitting requests, /api/v2/statements) — https://docs.snowflake.com/en/developer-guide/sql-api/submitting-requests
- Snowflake SQL API reference — https://docs.snowflake.com/en/developer-guide/sql-api/reference
- Snowflake REST APIs — https://docs.snowflake.com/en/developer-guide/snowflake-rest-api/snowflake-rest-api
- Snowflake SCIM support — https://docs.snowflake.com/en/user-guide/scim-intro
- CREATE SECURITY INTEGRATION (SCIM) — https://docs.snowflake.com/en/sql-reference/sql/create-security-integration-scim
- User management — https://docs.snowflake.com/en/user-guide/admin-user-management
- Identifier requirements — https://docs.snowflake.com/en/sql-reference/identifiers-syntax
