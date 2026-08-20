---
type: "Research Output"
title: "Access-Control Sync — Options, Risks & Recommendation"
description: "Cross-vendor comparison of authorization models and a risk-aware recommendation for how far QLabs Catalog Sync should synchronize access control, with neutral-model and SDK impact."
tags: ["research", "RS-09", "access-control", "authorization", "recommendation"]
timestamp: "2026-08-06T12:00:00Z"
status: "draft"
---

# Access-Control Sync — Options, Risks & Recommendation

This synthesizes the four per-vendor access-control notes (Databricks, Qlik, Snowflake, Collibra)
into a decision about how far QLabs Catalog Sync should go in synchronizing authorization, and how
that maps onto the RS-03 neutral model and the RS-08 connector SDK. The short version: reading and
reporting access is safe and valuable and should be in the product; writing access two-way is not,
and should be avoided in favor of narrowly-scoped, opt-in, one-way provisioning later.

## 1. The four models are different in kind

Every catalog has an authorization system, but they are not variations on one model — they are four
different *kinds* of model, which is the crux of the problem.

| System | Model kind | Grant target | Granularity | Plane |
| --- | --- | --- | --- | --- |
| Databricks | Object-privilege grants | Principal (user/group/service principal) | Per securable, down to table/column | Data access |
| Snowflake | Role-based (RBAC) | **Role** (privileges to roles; roles to users) | Per object, via role indirection | Data access |
| Qlik Cloud | Space-scoped role bundles | User/group assigned a role **in a space** | Container (space), not per item | App/data access |
| Collibra | Responsibilities | User/group given a role scoped to community/domain/asset | Per resource | **Metadata stewardship, not data access** |

Consequences:

- **Databricks** grants a privilege directly to a principal on an object. Clean tuple:
  (securable full_name, principal, privilege).
- **Snowflake** never grants to a user directly — privileges go to roles, roles to users. Any
  mapping to a per-user or per-space model is lossy both ways (role-flattening one direction,
  role-invention the other).
- **Qlik** governs access by *space membership* at container granularity; there is no per-dataset
  grant. A Qlik space does not correspond to a single table or a single Snowflake role.
- **Collibra** responsibilities express who *curates or is accountable for* metadata — not who can
  read data. Collibra authors access *policy* (Data Access requests, Protect masking) enforced in the
  source platform via Edge. Treating a Collibra responsibility as a data grant is a category error.

## 2. Two orthogonal hard problems

### 2.1 Identity resolution (the real blocker)

There is no shared principal identifier across these systems. Principals are identified by email or
username (Databricks users), group id/name, service-principal application id, Snowflake **role**
names, Qlik IdP `subject`, and Collibra opaque UUIDs. Group-vs-role-vs-user semantics also differ.
Any access sync — even read-only reporting that correlates "the same person across systems" —
requires an **identity-resolution layer** that:

- correlates human principals on a stable attribute (email is the least-bad key; IdP `subject` where
  both sides share an IdP), with an explicit, auditable mapping store;
- treats groups and roles as first-class, separately mapped principal types;
- has an explicit policy for unmatched principals (never silently drop or, worse, grant to the wrong
  match).

Email correlation is fragile (aliases, case, deprovisioned accounts), so false matches are a
security event, not a cosmetic bug. This layer is a prerequisite for anything beyond single-system
reporting.

### 2.2 Model impedance and granularity

Even with perfect identity mapping, the models do not translate losslessly: object-privilege vs
role-privilege vs space-membership vs stewardship, and container vs object granularity. There is no
universal grant representation that round-trips across all four without loss or invention.

## 3. Read/write API feasibility (per note)

Mechanically, reading and writing access is *feasible* on the three data-plane systems and on
Collibra:

- **Databricks** — read via UC permissions REST (`GET .../permissions/{type}/{full_name}`,
  `effective-permissions`) or `SHOW GRANTS`; write via delta `PATCH` (add/remove) or `GRANT`/`REVOKE`
  (idempotent, read-current-state-first).
- **Snowflake** — read via `SHOW GRANTS` (real-time) or `ACCOUNT_USAGE` (bulk, ~2h lag, soft-delete);
  write via `GRANT`/`REVOKE ... TO ROLE` over the SQL REST API (idempotent).
- **Qlik** — read/write space assignments via `/api/v1/spaces/{id}/assignments` (POST/PUT/DELETE);
  tenant roles via full-replace PATCH (lost-update risk); IdP-managed group membership is read-only.
- **Collibra** — read/write responsibilities via Core REST v2 `/rest/2.0/responsibilities`
  (POST/DELETE, bulk); mirror only direct (non-inherited) responsibilities.

So the constraint is not "can the APIs do it" — it is "should we, given identity fragility and model
impedance."

## 4. The options

### Option A — Observe & report (read-only) — RECOMMENDED for v1

The engine reads access state from every endpoint into a neutral, read-only access graph and surfaces
it as part of data-product metadata: "who can access this product and its backing datasets, where,
and at what level." It computes and reports **drift** (e.g., a Qlik space grants access that the
backing Databricks tables do not, or vice versa) and flags unmatched principals.

- Risk: low (no writes). Value: high — answers real governance questions and is the natural first use
  of the identity-resolution layer.
- Requires: identity resolution (section 2.1), a neutral access entity (section 5), connector read of
  grants.

### Option B — One-way provisioning from a source of truth — opt-in, later

Designate one authoritative source (a specific catalog, or an external IdP/policy system) and push
grants in a single direction for **narrowly-scoped, explicitly-allowlisted** flows — for example,
when a principal joins a Qlik space that fronts a data product, grant that principal read on the
Databricks tables backing it.

- Risk: medium. Feasible only with: identity resolution, a per-flow allowlist (which resource, which
  privilege, which direction), scoped least-privilege translation, dry-run + audit, and ideally a
  human approval gate. Best confined to systems that share the grant paradigm (Databricks and
  Snowflake data-plane grants) rather than crossing paradigms (space to per-object).

### Option C — Two-way access sync — NOT recommended

Symmetric reconciliation of authorization across all four systems is where the impedance and identity
problems compound: privilege escalation, oscillation, lossy role/space/object translation, and the
Collibra category error. This should be avoided. If ever attempted, only within a homogeneous subset,
never for Collibra responsibilities, and always behind human approval.

## 5. Neutral-model and SDK impact

To support Option A now and leave room for Option B later:

- Add a **Principal** entity to RS-03: neutralId, type (user/group/service/role), and per-endpoint
  identities (email, group name, application id, Snowflake role, Collibra UUID). This is owned by the
  identity-resolution layer.
- Add a read-only **AccessBinding** entity: `resourceRef` (data product / dataset), `principalRef`,
  a neutral `level` (a coarse enum such as read / curate / manage rather than a full privilege
  algebra), `sourceEndpoint`, and a `nativeDetail` blob preserving the exact native grant. Mode is
  read-only in v1.
- In the RS-08 capability manifest, add `EntityType.ACCESS_BINDING` and `EntityType.PRINCIPAL` with
  `mode: "ro"` for the initial connectors; a connector may later declare `rw` to opt into Option B.
- Map Collibra responsibilities to **stewardship/ownership** (the existing Party/owner concept in
  RS-03), explicitly *not* to AccessBinding. Collibra data-access policy (requests, Protect) is out of
  scope for grant sync.
- Snowflake AccessBinding read must resolve role indirection to effective user access for reporting,
  while preserving the role structure in `nativeDetail`.

## 6. Risks to keep on the register

Privilege escalation from automated or coarse grants; identity false-matches from email correlation;
group-vs-role-vs-user semantic drift; container-vs-object granularity loss; Snowflake role
indirection; Collibra governance-vs-data-plane category error; audit/compliance obligations for any
automated authorization change; and blast radius if a sync bug propagates a wrong grant widely.

## 7. Recommendation

Ship **Option A (observe & report)** in the roadmap first: model access as a read-only neutral
AccessBinding + Principal graph, build the identity-resolution layer as its foundation, and deliver
drift/audit reporting on data products. Defer any writing of access to an explicit, opt-in,
one-way, allowlisted **Option B** capability — most safely between the two data-plane grant systems
(Databricks and Snowflake) — behind dry-run and approval. Do **not** pursue two-way access sync, and
keep Collibra access modeled as stewardship, not data grants. This keeps the security-sensitive
surface read-only by default while still delivering the cross-catalog access visibility the question
is really about.

## 8. Open questions

- Which system (if any) is the intended source of truth for provisioning, or is an external IdP/policy
  engine the real authority?
- Is the near-term need genuine *provisioning*, or *visibility and drift detection* (which Option A
  already satisfies)?
- Compliance/audit requirements that would gate any automated authorization change.
- Whether Principal/identity resolution should reuse an existing IdP/SCIM source rather than be built.

# Citations

* [Databricks Access Control — Model & API](/Research/RS-09-access-control-sync/notes/databricks-access-control.md) — UC privilege model and permission read/write API.
* [Qlik Cloud Access Control — Model & API](/Research/RS-09-access-control-sync/notes/qlik-access-control.md) — spaces, roles, and assignment API.
* [Snowflake Access Control — Model & API](/Research/RS-09-access-control-sync/notes/snowflake-access-control.md) — RBAC roles/grants and read/write API.
* [Collibra Access Control — Model & API](/Research/RS-09-access-control-sync/notes/collibra-access-control.md) — responsibilities, scopes, and the governance-vs-data-plane distinction.
* [Neutral Metadata Model Specification (v1)](/Research/RS-03-neutral-metadata-model/outputs/neutral-metadata-model-spec.md) — model the Principal and AccessBinding entities extend.
* [Connector Plugin SDK — Design Specification (v1)](/Research/RS-08-connector-plugin-sdk/outputs/connector-sdk-spec.md) — capability manifest that gates access entities as read-only.
