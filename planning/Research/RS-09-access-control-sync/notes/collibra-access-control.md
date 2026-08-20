---
type: "Research Note"
title: "Collibra Access Control — Model & API"
description: "Collibra roles, responsibilities, and scopes with the read/write API and principal identity, assessed for cross-catalog access-control sync."
tags: ["research", "RS-09", "collibra", "access-control", "responsibilities"]
timestamp: "2026-08-06T11:30:00Z"
status: "draft"
---

# Collibra Access Control — Model & API

This note investigates Collibra's authorization model only — how it decides who may act on what
inside the catalog — for the QLabs Catalog Sync bridge (RS-09). It deliberately ignores general
metadata modelling (asset types, attributes, relations) except where those objects are the *target*
of an access-control assignment. The central finding, expanded in the last section, is that
Collibra governs *metadata stewardship*, not *data-plane access*: its "roles" say who may curate or
approve a metadata resource, not who may read rows in a table. That makes Collibra semantically
different from the data-grant models of Databricks and Snowflake and from Qlik space membership.

## 1. Authorization Model

Collibra uses a standard role-based access control (RBAC) model built on three interconnected
concepts: **users** (principals), **roles** (named collections of permissions), and **permissions**
(granular authorizations). Permissions are never assigned directly to a user; a user only ever
inherits permissions through a role. Roles split into two families by scope.

**Global roles** consist of global permissions and determine which Collibra *applications and
system-wide capabilities* a user may use — for example the `Sysadmin`, `Catalog`, `Catalog Author`,
`Glossary`, `Policy Manager`, `Data Quality Admin`, and the various `Protect` and AI-governance
roles. Global roles are not scoped to any particular data resource; they are membership lists
attached to the whole environment. Each out-of-the-box global role has a stable resource UUID
(e.g. `Sysadmin` = `00000000-0000-0000-0000-000000005027`). Assigning a user to a global role is a
global responsibility (see below with a null resource).

**Resource roles** consist of resource permissions and apply to a *resource and its children*. A
resource role grants its permissions to a user or group only through a **responsibility** — the act
of assigning that role to a principal on a specific resource. Out-of-the-box resource roles include
`Owner` (`...5040`), `Business Steward` (`...5016`), `Technical Steward` (`...5038`),
`Community Manager` (`...5015`), `Data Custodian` (`...5041`), `Stakeholder` (`...5018`),
`Data Analyst Level 1/2` (`...5061` / `...5062`), and others, each with a fixed resource UUID.

**Responsibility = the assignment triple.** A responsibility binds `(resource role) x (user or user
group) x (resource)`. The resource is a **community**, a **domain**, or an **asset** — or is absent,
which yields a *global responsibility* (i.e. a global-role membership). This triple is the atomic,
addressable object the sync engine must read and write. "William Parker is Owner of the Customer
Revenue asset" is one responsibility.

**Scopes and inheritance.** Responsibilities flow strictly downward through the
community -> domain -> asset containment hierarchy:

- A responsibility on a **community** is inherited by its subcommunities and by every domain and
  asset inside it.
- A responsibility on a **domain** is inherited by every asset in that domain.
- A responsibility on an **asset** applies only to that asset.

So a Business Steward of a community is effectively a Business Steward of all descendants. When
reading assignments the sync engine must distinguish **direct** responsibilities (assigned on this
exact resource) from **inherited** ones (originating on an ancestor). The UI marks inherited grants
with an "Inherited" tag, and the read API exposes an `includeInherited` switch. Only direct
responsibilities should ever be treated as the source of truth to mirror; inherited ones are derived
and would cause double-writes if replayed onto children.

**Governance permissions vs data access — the key distinction.** Resource permissions granted via
responsibilities are *governance* rights: view/edit the metadata of an asset, add characteristics,
approve workflow steps, act as the accountable steward, etc. They do **not** grant read or write
access to the underlying physical data. Collibra's role in the data-access story is to *govern the
request and the policy* — for example Data Access requests, or Collibra Protect, which authors
column masking / row filtering policies that are then *enforced by the source platform* (Snowflake,
Databricks, BigQuery) via Edge — rather than to be the enforcement point itself. Even
`Data Analyst Level 1/2` ("may see a data sample" / "full access to the data") are metadata-level
role markers that downstream tooling may act on, not grants that Collibra applies to a warehouse.
For access-control *sync* purposes, the plainly readable/writable surface is the responsibility
graph (metadata stewardship), and that is what this note treats as Collibra's "access control".

## 2. Principal / Identity Model

Principals are **users** and **user groups**, and a responsibility's owner may be either.

- **Users** are identified by a system UUID (the `id`) and carry a unique `userName`, plus
  first/last name and email addresses. License type (Viewer / Contributor / Creator) is *computed*
  from the highest license required by any permission in any role the user holds — it is not a
  field you set. Fixed system accounts exist (`Admin`, `System user`, `Workflow user`) and should be
  excluded from sync.
- **User groups** are logical collections identified by a UUID and a `name`. Assigning a role to a
  group means every member inherits it. Hidden built-in groups (`Everyone`, `Users`) and
  `Data Custodians` exist and should be handled carefully or excluded.

**Provisioning.** Identities can be managed three ways: **SCIM** provisioning from an IdP (automated
create/update/deprovision of users and groups, via the dedicated SCIM API), **LDAP** integration
(import and sync users/groups; LDAP also handles authentication), or **manual** creation in
Collibra. Authentication/SSO (SAML/OIDC) is layered on top of whichever provisioning path is used.
The practical consequence for sync: the Collibra `userName`/email is the only human-meaningful
handle that can be cross-referenced against a Databricks/Snowflake/Qlik principal — the Collibra
UUID is opaque and Collibra-local.

## 3. Read API — enumerating responsibilities and roles

Collibra's Core REST API v2 exposes responsibilities as first-class resources. Find (enumerate)
responsibilities with a filtered GET; fetch one by id:

```
GET /rest/2.0/responsibilities
    ?roleIds={roleUuid}          # filter by one or more role UUIDs
    &resourceIds={resourceUuid}  # filter by community/domain/asset UUID
    &ownerIds={userOrGroupUuid}  # filter by principal (user or group) UUID
    &includeInherited=false      # false = direct only; true = include inherited
    &offset=0&limit=1000         # (cursor-based paging in newer versions)

GET /rest/2.0/responsibilities/{responsibilityId}
```

Each returned responsibility carries its own `id` (the responsibility UUID) plus references to the
`role`, the `owner` (user or group, with the owner's `resourceType` distinguishing `User` vs
`UserGroup`), and the `resource` it is assigned on. Only the filter parameters you supply are
applied; unspecified filters are ignored, and results must satisfy all supplied constraints.

Supporting reads for building the assignment graph:

```
GET /rest/2.0/roles            # resource + global roles; each has id (UUID), name, isGlobal
GET /rest/2.0/users            # principals: id (UUID), userName, emailAddresses, ...
GET /rest/2.0/userGroups       # groups: id (UUID), name, members
```

The role UUIDs are stable and mostly the well-known `00000000-...-0000000050xx` values for
out-of-the-box roles, so role matching across environments can key on the UUID or the name. The
whole responsibility surface is cleanly and completely enumerable, which makes Collibra a good
*source* for a read-only mirror of stewardship assignments.

## 4. Write API — creating and removing responsibilities

Assignments are created and deleted through the same responsibilities resource. A single create:

```
POST /rest/2.0/responsibilities
Content-Type: application/json

{
  "roleId":               "00000000-0000-0000-0000-000000005016",  # required: resource/global role UUID
  "ownerId":              "<user-or-group-UUID>",                   # required: the principal
  "resourceId":           "<community|domain|asset UUID>",          # omit/null => global responsibility
  "resourceDiscriminator": "Asset"                                  # "Community" | "Domain" | "Asset"
}
```

Field notes: `ownerId` and `roleId` are required UUIDs. `resourceId` targets the resource; if it is
null a *global* responsibility (global-role membership) is created. `resourceDiscriminator` is the
current way to state the resource kind (`Community` / `Domain` / `Asset`); the older `resourceType`
field is deprecated and, if both are sent, `resourceDiscriminator` wins.

Bulk and delete operations:

```
POST   /rest/2.0/responsibilities/bulk        # add many responsibilities in one call (array body)
DELETE /rest/2.0/responsibilities/{responsibilityId}
DELETE /rest/2.0/responsibilities/bulk        # remove many by responsibility id
```

**Required permissions to write.** Managing responsibilities is itself a governed action — assigning
and removing roles/responsibilities is explicitly the duty of the `Community Manager` resource role
(for the relevant community/domain scope) or a user with the `Sysadmin` global role. The sync
service account therefore needs an appropriately scoped Community Manager responsibility or Sysadmin,
and must hold a license tier consistent with those permissions. Responsibilities are created directly
on a resource (there is no "assign on parent to fake inheritance"); inheritance is computed, never
written.

## 5. Sync Feasibility Notes

**Cleanly readable AND writable.** The responsibility graph is symmetric across read and write:
`GET /rest/2.0/responsibilities` enumerates it with role/resource/owner filters and an
`includeInherited` switch, and `POST`/`DELETE /rest/2.0/responsibilities` (plus their `/bulk`
variants) create and remove individual assignments. Roles, users, and groups are all fully readable
for reference resolution. This is one of the cleaner CRUD surfaces among the target catalogs.

**Natural matching key.** A responsibility is uniquely identified for *storage* by its own
`responsibilityId`, but that UUID is environment-local and useless for cross-system matching. The
stable *logical* key the sync engine should hash on is the triple:

```
(resourceId + roleId + ownerId)          # resource UUID x role UUID x principal UUID
```

with `resourceDiscriminator` disambiguating the resource kind and a null `resourceId` denoting a
global assignment. Because role UUIDs are constant for out-of-the-box roles, only `resourceId` and
`ownerId` need mapping tables to a peer catalog.

**Only mirror direct responsibilities.** Read with `includeInherited=false`. Inherited assignments
are derived from ancestors; replaying them onto children would create spurious direct grants and
break idempotency.

**Principal identity mismatch (the main risk).** Collibra principals are Collibra-local UUIDs;
the only portable handle is `userName`/email (for users) or group `name`. Any sync to or from
Databricks (account/SCIM principals), Snowflake (users/roles), or Qlik (IdP subject / space
members) must resolve identities through email or a shared IdP subject, tolerate users that exist in
one system but not the other, and decide group-vs-user expansion policy. A missing or ambiguous
principal mapping is the most likely cause of silent drift or accidental over-grant.

**Semantic mismatch (the strategic risk).** This is the decisive caveat. A Collibra responsibility
answers "who *stewards / may curate / is accountable for* this metadata resource", whereas a
Databricks/Snowflake grant answers "who may *read or write the actual data*", and a Qlik space
membership answers "who may open/edit apps in this workspace". These are different planes:
governance/stewardship vs data-plane access. Collibra's own data-access mechanisms (Data Access
requests, Collibra Protect masking/row policies) *author and govern* policy that is then *enforced by
the source platform via Edge* — Collibra is not the enforcement point. Therefore a naive two-way
sync that maps a Collibra `Owner`/`Business Steward` responsibility to a warehouse `SELECT` grant
(or vice versa) would be a category error and a security hazard. The defensible design is to treat
Collibra as the *stewardship/ownership* layer and sync responsibilities only against equivalent
stewardship/ownership concepts in peers, keeping true data-plane grants on a separate, explicitly
mapped track (or out of Collibra sync entirely). Any bridge should encode this plane distinction as
a first-class rule rather than a field mapping.

# Citations

- Defining users, roles, and permissions — https://productresources.collibra.com/docs/collibra/latest/Content/Settings/UsersAndGroups/co_user-roles-permissions.htm
- Global roles (with out-of-the-box role UUIDs) — https://productresources.collibra.com/docs/collibra/latest/Content/Settings/RolesAndPermissions/Roles/GlobalRoles/to_global-roles.htm
- Resource roles (with out-of-the-box role UUIDs) — https://productresources.collibra.com/docs/collibra/latest/Content/Settings/RolesAndPermissions/Roles/ResourceRoles/to_resource-roles.htm
- Responsibilities (assignment model and inheritance) — https://productresources.collibra.com/docs/collibra/latest/Content/Responsibilities/to_responsibilities.htm
- Permissions — https://productresources.collibra.com/docs/collibra/latest/Content/Settings/RolesAndPermissions/Permissions/co_permissions.htm
- SCIM provisioning API — https://developer.collibra.com/api/rest/scim/
- Core REST API (Version 2) overview — https://developer.collibra.com/api/references/data-governance
- Collibra Developer Portal (API index) — https://developer.collibra.com/api
- Getting started with the REST API — https://developer.collibra.com/developer-tutorials/getting-started-with-the-rest-api/
