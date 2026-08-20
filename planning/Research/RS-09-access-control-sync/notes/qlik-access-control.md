---
type: "Research Note"
title: "Qlik Cloud Access Control — Model & API"
description: "Qlik Cloud spaces, roles, and assignment model with the read/write API and principal identity, assessed for cross-catalog access-control sync."
tags: ["research", "RS-09", "qlik", "access-control", "spaces"]
timestamp: "2026-08-06T11:30:00Z"
status: "draft"
---

# Qlik Cloud Access Control — Model & API

This note captures how Qlik Cloud governs *access* (authorization) to catalog items, datasets,
and data products, and how that access can be read and written through the public REST API. It is
scoped to authorization only; general metadata modeling is covered elsewhere in this topic. It is
written to support the QLabs Catalog Sync bridge, which must read and, potentially, mirror
access-control state across catalogs.

## 1. Authorization model

Qlik Cloud authorization is built on two layers that combine:

- **Tenant (security) roles** — global capabilities in the tenant (who may administer, who may
  create spaces, who may audit).
- **Spaces** — the containers that actually govern access to content. Almost every governable
  resource (analytics apps, datasets, data connections, data products, Talend Data Integration
  projects) lives in exactly one space, and *access to the resource is derived from access to its
  space plus the role the principal holds there*.

### Space types

- **Personal space** — each user's private area. Content here is owned by that single user; there
  is no membership model to sync (sharing means moving/copying content into a shared or managed
  space).
- **Shared space** — a collaborative area for co-development. Members are added with a role and can
  create, edit, view, or consume content depending on that role.
- **Managed space** — a governed distribution area. Content is published into it and consumed by a
  controlled audience; contribution is tightly restricted. Used for "publish to consumers" flows.
- **Data space** — the governed area for Qlik Talend Data Integration projects and data assets. It
  has its own role set oriented around producing, operating, and consuming data.

At the API level the space `type` enum is `shared`, `managed`, or `data`. Personal spaces are not
created or managed through the spaces collection.

### Space roles (the grant that matters)

A space role is a named bundle of permissions on the space and everything inside it. Roles are
assigned to *members* (users or groups) when they are added to the space. The **space owner**
implicitly receives all of the space's assignable roles and is *not* represented as an explicit
assignment.

The user-facing role names differ by space type; the API expresses them as lowercase role tokens.
Representative mapping:

- Shared space (UI → intent): Can manage, Can edit, Can edit data in applications, Can view, Can
  consume data. Plus the implicit Owner.
- Managed space (UI → intent): Can manage, Can contribute, Can view, Has restricted view, Can
  consume data.
- Data space (UI → intent): roles oriented around managing the space, producing/operating data
  integration projects, previewing, and consuming data.

The canonical set of role tokens used across the assignment API is:

```
consumer, contributor, dataconsumer, datapreview,
facilitator, operator, producer, publisher,
basicconsumer, codeveloper
```

Not every token is valid in every space type. The **authoritative, per-space list of what can be
granted** is returned by the space itself:

```
GET /api/v1/spaces/{spaceId}
# inspect response: meta.assignableRoles  (array of role tokens valid in THIS space)
```

This is important for sync: never hard-code the role set — read `meta.assignableRoles` for the
target space and validate against it before writing.

### How "a user has access to a space" actually works

A principal has access to a space if any of the following is true:

1. They are the **owner** of the space (implicit full access; no assignment record).
2. They have an **assignment** on the space (a record binding their user id to one or more role
   tokens).
3. They are a member of a **group** that has an assignment on the space (the group's roles apply to
   every member transitively).
4. A tenant role effectively exposes it (e.g. `TenantAdmin` can administer spaces regardless of
   membership).

Groups are the primary scaling mechanism: rather than assigning roles to hundreds of users, an
assignment is made to a group and membership drives effective access. This means the *effective*
access of a user is the union of their direct assignment and every group assignment they inherit —
a critical nuance when computing a flattened grant set for another catalog.

### Tenant roles

Tenant roles gate global capability, not per-resource access. Key defaults include `TenantAdmin`
(full tenant administration), `AuditAdmin` (audit/event access), `Developer`, and the space-creator
roles such as `SharedSpaceCreator`, `ManagedSpaceCreator`, and the data-space creator role. Custom
tenant roles can also be defined. Tenant roles are assigned to users and groups (see §3–4) and each
role object carries an `id`, `name`, `type` (`default` or `custom`), and `level` (`admin` or
`user`).

## 2. Principal / identity model

Three principal types can hold access:

- **Users** — interactive identities. Each user object exposes an internal `id` (Qlik's stable
  primary key, an opaque string), a `subject` (the identifier asserted by the identity provider —
  the stable cross-login key), `status`, `email`, `name`, `tenantId`, `clientId`, and
  `assignedRoles`.
- **Groups** — collections used for both tenant-role and space-role assignment. There are three
  kinds:
  - *Identity Provider (IdP) managed groups* — created/synchronized from the IdP. Membership is
    controlled by the IdP claims and **cannot** be edited in Qlik. They are created dynamically when
    a user carrying that group in their claims first logs in (or ahead of time via SCIM).
  - *Custom groups* — created and fully managed inside Qlik via API (`providerType: custom`).
    Membership is editable through the API.
  - *Qlik system groups* — managed by Qlik; e.g. the `Everyone` group with the fixed id
    `000000000000000000000001` (assigning a role to it grants that capability to all users).
  A group object exposes `id`, `name`, `status`, `providerType` (`custom` or `idp`), `idpId` (for
  IdP groups), and `assignedRoles`.
- **Machine identities / OAuth clients (bots)** — an OAuth machine-to-machine client, on its first
  token request against a tenant, materializes a non-interactive **bot** user. Its `subject` has the
  stable form `qlikbot\{OAUTH_CLIENT_ID}` (consistent across all tenants in a region) and it acts
  with `TenantAdmin`-equivalent privilege. In assignments the principal `type` can be `bot`. M2M
  impersonation additionally lets a client act *as* a specified user (by `userId` or `subject`).

### Provisioning

- **OIDC / interactive IdP** — one interactive IdP is active at a time. Users and their group
  memberships arrive via claims at login; the mapped `subject` claim is the durable identifier.
- **SCIM** — for compatible IdPs (e.g. Microsoft Entra ID), users and groups are provisioned/
  synchronized automatically. SCIM matching keys off the user's **email address** to create and then
  reconcile the user and their group memberships.

Stable identifiers to rely on:
- User: internal `id` (best for API calls within one tenant) and `subject` (best for correlating
  the same human across logins / IdP assertions).
- Group: `id` (and `name`, unique per provider type).
- Bot: `subject` = `qlikbot\{clientId}`.

## 3. Read API — enumerating access

**List space assignments** (membership + roles for one space):

```
GET /api/v1/spaces/{spaceId}/assignments
# query: assigneeId, type (user|group), limit, next/prev cursors
# NOTE: the owner is NOT returned here (owner has all assignableRoles implicitly)
```

Each item in `data[]`:

```json
{
  "id": "…",              // assignment id
  "type": "user",          // user | group | bot
  "assigneeId": "…",       // the userId or groupId being granted
  "roles": ["consumer"],   // role tokens granted in this space
  "spaceId": "…",
  "tenantId": "…",
  "createdAt": "…", "createdBy": "…",
  "updatedAt": "…", "updatedBy": "…",
  "links": { "self": {"href": "…"}, "space": {"href": "…"} }
}
```

**Retrieve one assignment:**

```
GET /api/v1/spaces/{spaceId}/assignments/{assignmentId}
```

**Discover which roles are grantable in a space** (needed to validate writes):

```
GET /api/v1/spaces/{spaceId}      # read meta.assignableRoles and type
GET /api/v1/spaces                # enumerate spaces; supports ?roles= filter on caller's role
```

**List users, groups, and roles (tenant-wide principal + role catalogs):**

```
GET /api/v1/users        # fields incl. id, subject, email, status, assignedRoles; filterable
GET /api/v1/groups       # IdP-managed + custom groups (systemGroups omitted by default)
GET /api/v1/groups?systemGroups=true   # system groups only (e.g. Everyone)
GET /api/v1/roles        # tenant/security role catalog (default + custom)
```

Filtering follows an RFC 7644-style syntax, e.g. `filter=subject eq 'user1234'` or
`filter=assignedRoles.name eq 'ManagedSpaceCreator'` to find every principal holding a given tenant
role. Users/groups/roles use cursor-based pagination. The List Groups endpoint does **not** return
IdP/custom groups and system groups in the same call — two requests are required for a full picture.

To reconstruct effective access for a resource: (1) resolve the resource's `spaceId`; (2) read the
space's assignments; (3) for group assignments, expand membership (custom-group membership is on the
user's `assignedGroups`; IdP-group membership comes from the IdP) to reach the underlying users; (4)
union with the owner and any tenant-role holders.

## 4. Write API — assigning and revoking access

**Create a space assignment** (add a user/group/bot to a space with roles):

```
POST /api/v1/spaces/{spaceId}/assignments
Content-Type: application/json
{
  "type": "group",                 // user | group | bot
  "assigneeId": "<userId|groupId>",
  "roles": ["consumer", "publisher"]
}
# 201 Created -> returns the assignment object (see §3)
# Rate limit: Tier 2 (100 requests/minute)
```

Constraints: **only one assignment may exist per space per principal**; owners must not be assigned
(they already hold all roles); `roles` must be non-empty and every token must be in the space's
`meta.assignableRoles`.

**Update an assignment's roles** (replace the role set for an existing member):

```
PUT /api/v1/spaces/{spaceId}/assignments/{assignmentId}
{ "roles": ["consumer"] }
```

**Revoke access** (remove a member from a space):

```
DELETE /api/v1/spaces/{spaceId}/assignments/{assignmentId}
```

**Assign / revoke tenant (security) roles** — done on the principal, not the space, and only via
*full replacement* of the role array:

```
# User: append/remove by rewriting the whole list
PATCH /api/v1/users/{userId}
[{ "op": "replace", "path": "/assignedRoles",
   "value": [ {"name": "AuditAdmin"}, {"name": "ManagedSpaceCreator"} ] }]

# Group: same shape
PATCH /api/v1/groups/{groupId}
[{ "op": "replace", "path": "/assignedRoles",
   "value": [ {"name": "TenantAdmin"}, {"name": "AuditAdmin"} ] }]
# 204 No Content on success. Role names are case sensitive.
```

**Custom group membership** — edited on the *user* resource (not the group):

```
# add
PATCH /api/v1/users/{userId}
[{ "op": "add", "path": "/assignedGroups/-", "value": "<customGroupId>" }]

# remove
PATCH /api/v1/users/{userId}
[{ "op": "remove-value", "path": "/assignedGroups", "value": "<customGroupId>" }]

# replace whole set (by id or by name+providerType:"custom")
PATCH /api/v1/users/{userId}
[{ "op": "replace", "path": "/assignedGroups",
   "value": [ { "name": "CG-Finance", "providerType": "custom" } ] }]
```

IdP-managed group memberships **cannot** be modified through this API (attempts return HTTP 400);
they are owned by the IdP.

**Create / delete custom groups:**

```
POST   /api/v1/groups        { "name": "...", "status": "active",
                               "providerType": "custom",
                               "assignedRoles": [ {"name": "AuditAdmin"} ] }
DELETE /api/v1/groups/{id}    # custom group only when it has no members; 204 on success
```

Required permission for the write paths above: an identity holding `TenantAdmin` (for tenant roles,
users, groups) or, for space assignments, an identity with a managing role on the target space
(space owner / a role with manage permission) or `TenantAdmin`.

## 5. Sync feasibility notes

**Cleanly readable AND writable via API:**

- Space assignments (user/group/bot + role tokens) — full CRUD via
  `/api/v1/spaces/{id}/assignments`. This is the highest-fidelity, most sync-friendly surface.
- Tenant role assignments on users and groups — readable and writable, but *replace-only* (must
  read-modify-write the whole `assignedRoles` array; no atomic single-role add/remove).
- Custom group membership — readable and writable (on the user's `assignedGroups`).

**Read-only / externally owned (cannot be mirrored back into Qlik):**

- IdP-managed group membership and the IdP itself — governed outside Qlik; a sync engine can *read*
  the resulting effective access but must not attempt to write these memberships.
- Space ownership — implicit and not exposed as an assignment; transfer is a separate operation.

**Natural matching key for an assignment:** the tuple **`(tenantId, spaceId, assigneeId, type)`**
uniquely identifies a space grant, with `roles[]` as the mutable payload — reinforced by the
platform rule that only one assignment can exist per space per principal. `(principal, tenantRole)`
is the analogous key for tenant-level grants. For the *human* behind the principal, the durable
correlation key is the user `subject` (IdP-asserted), while `id` is the right key for in-tenant API
operations.

**Principal resolution is a two-step problem:** a space assignment stores only `assigneeId` +
`type`. To render a grant meaningful in another catalog you must resolve `assigneeId` to a user
(`subject`, `email`, `name`) or group (`name`, members) via the Users/Groups APIs, then flatten
group assignments to member users.

**Big risks for cross-catalog sync:**

1. **Model mismatch.** Qlik's grant model is *space-scoped role bundles*, not per-object ACLs. Other
   catalogs (Databricks table/schema grants, Snowflake object privileges, Collibra communities/
   domains) often grant at finer or coarser granularity. There is no clean 1:1 map; a space role
   must be *interpreted* into target privileges, and the inverse mapping is lossy.
2. **Effective vs. declared access.** True access is the union of owner + direct assignment + group
   assignments + tenant roles. A naive read of only `/assignments` understates access; a sync must
   expand groups and account for owners and tenant admins.
3. **Identity mismatch across IdPs.** The stable key is `subject`/`email` as asserted by *Qlik's*
   IdP. Another catalog keyed on a different IdP, a different email casing, or SCIM externalId will
   not line up automatically. Email-based matching (as SCIM uses) is the most portable bridge but is
   fragile (aliases, changed addresses, shared mailboxes).
4. **Replace-only tenant-role writes** create race/lost-update risk: concurrent writers can clobber
   each other's role changes because there is no add/remove primitive. Sync must read-modify-write
   with care (and ideally a reconciliation/diff step).
5. **IdP-owned data is one-directional.** IdP group membership can be read but not written; a
   bidirectional sync that tries to push membership changes into IdP-managed groups will fail (400).
6. **Bots and impersonation** appear as principals (`qlikbot\{clientId}`) with elevated privilege;
   they should generally be filtered out of, or specially handled in, human-oriented access sync.

Recommended sync anchor: treat `(spaceId, assigneeId, type)` as the primary key with `roles[]` as
the diffable value; carry both Qlik `id` and `subject`/`email` for every principal so downstream
matching can fall back from exact-id to email correlation.

# Citations

- https://qlik.dev/apis/rest/spaces/
- https://qlik.dev/apis/rest/users/
- https://qlik.dev/apis/rest/groups/
- https://qlik.dev/apis/rest/roles/
- https://qlik.dev/manage/access-control/
- https://qlik.dev/manage/access-control/groups/
- https://qlik.dev/manage/access-control/manage-roles/
- https://qlik.dev/manage/access-control/space-roles/
- https://qlik.dev/manage/access-control/default-roles/
- https://qlik.dev/manage/access-control/custom-roles/
- https://qlik.dev/authenticate/oauth/
- https://qlik.dev/authenticate/oauth/getting-started-oauth-m2m/
- https://qlik.dev/authenticate/oauth/guiding-principles-oauth-impersonation/
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/Spaces/managing-shared-spaces.htm
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/Spaces/managing-managed-spaces.htm
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/DataIntegration/DataSpaces/permissions-data-space.htm
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/Admin/SaaS-roles.htm
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/Admin/permissions-admins-users.htm
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/Admin/auto-provisioning-using-SCIM.htm
- https://help.qlik.com/en-US/cloud-services/Subsystems/Hub/Content/Sense_Hub/Admin/mc-creating-oidc-idp.htm
