---
type: "Decision"
title: "Decision: MVP is Databricks-to-Qlik, and how the two models map"
description: "Narrows the RM-01 MVP to a single Databricks-to-Qlik flow and locks the eight mapping decisions the build needs: UC schema as the data product, no dataset creation in Qlik, owner resolution, no deletes, no glossary, SQL-gated tags, opt-in activation, and an async contract with watermark-returning list_changed."
tags: ["decision", "RM-01", "scope", "mvp", "databricks", "qlik"]
timestamp: "2026-08-20T09:00:00Z"
status: "accepted"
---

# Decision: MVP is Databricks-to-Qlik, and how the two models map

## Context

The [v1 scope decision](decision.md) fixed the direction (upstream only, Qlik as sole writer) but
left RM-01 carrying four connectors, and the implementation plan's first revision staffed Databricks,
Collibra, and Snowflake as parallel streams gating the same release. That triples the surface area
of the first shippable thing and delays the moment the connector SDK and neutral model are proven
end to end.

Separately, the plan assumed a set of mappings it never stated. Databricks has no object literally
called a data product; Qlik datasets are QRI-bound native resources that cannot be created for an
arbitrary foreign table; Qlik `keyContacts` wants a Qlik user id where Databricks offers an email;
and the connector ABC is specified two different ways across RS-07 and RS-08 (sync vs async, and
whether `list_changed` returns a watermark). Each gap is small on paper and blocking in code.

## Decision

**The MVP is a one-way Databricks-to-Qlik metadata sync.** Collibra and Snowflake remain RM-01
deliverables but become Track B, starting only after v0.1 ships. Their tasks sit on the board as
`blocked` so they cannot be picked up while Track A is open.

The following mappings are binding for the MVP:

1. **A Unity Catalog schema is the data product.** `catalog.schema` maps to one Qlik data product;
   its tables and views map to that product's datasets. Which schemas sync is a config selector of
   `catalog.schema` glob patterns. Delta shares and Marketplace listings are deferred.
2. **The connector never creates Qlik datasets.** `datasetIds` resolve against datasets already
   present in the target space — IdentityMap first, then name match within the space. Unresolved
   members are omitted and reported, never invented.
3. **Owner emails resolve to Qlik user ids** through the users API, cached, with every miss dropped
   and reported. This stays best-effort metadata.
4. **v1 never deletes in Qlik.** A vanished source object is reported as an orphan. The Qlik
   connector implements `delete()` and lifecycle actions for contract completeness; the engine has
   no path that calls them.
5. **Glossary is out of the MVP.** Databricks has no native glossary, so the Qlik glossary write
   path moves to Track B alongside Collibra, which is its real source.
6. **UC tags are read through `INFORMATION_SCHEMA` over the Statement Execution API**, and the
   capability manifest declares `tags` supported only when a SQL warehouse is configured; otherwise
   `na`.
7. **Neutral `active` maps to Qlik activation**, which is opt-in per pair and off by default because
   activation makes a product discoverable tenant-wide.
8. **The connector contract is async, and `list_changed` returns `ListChangedResult(changes,
   next_watermark)`**, resolving the RS-07/RS-08 discrepancy in favor of RS-07. The returned
   watermark is what makes a single-transaction commit — and therefore restart safety — possible.

The MVP is also built **without live tenants**: all tests run against respx mocks, an SDK-provided
fake connector, and hand-authored cassettes. Behavior only a real tenant can confirm is registered in
a `TENANT_UNVERIFIED` list with a probe script and a human-run checklist, rather than assumed.

## Consequences

- The first release proves the SDK, the neutral model, the engine, and the write path with one source
  instead of three, and the Collibra and Snowflake connectors then land against a contract that has
  already survived a real integration.
- The Qlik connector's MVP surface shrinks to data products plus dataset and user resolution;
  glossary terms, categories, relations, links, and change-status leave the critical path.
- Qlik data products created by the sync may start with an empty or partial `datasetIds` list when
  the matching Qlik datasets do not exist yet. That is visible in the run report and is the correct
  behavior — the alternative is fabricating resources in the target.
- Owner coverage is bounded by how many Databricks owner emails correspond to Qlik users. Gaps are
  reported, not papered over.
- Nothing the sync does can remove a Qlik object in v1, which makes an early misconfiguration
  recoverable by editing config rather than restoring data.
- The `TENANT_UNVERIFIED` registry becomes the pre-production checklist, so the unproven Qlik write
  details are visible instead of buried in code comments.

# Citations

* [Decision: v1 scope — upstream-only, no access-control sync](decision.md) — the scope this decision narrows.
* [v1 Implementation Plan — Work Packages, Tasks & Model Recommendations](implementation-plan.md) — the plan these decisions bind.
* [Databricks Unity Catalog & Data Products — API Reference](/Research/RS-01-databricks-catalog-api/outputs/databricks-catalog-api-reference.md) — basis for the UC schema mapping and the SQL-only tag read path.
* [Qlik Cloud Catalog & Data Products — API Reference](/Research/RS-02-qlik-catalog-api/outputs/qlik-catalog-api-reference.md) — data-product payloads, dataset identity, users and items APIs.
* [Qlik Two-Way Sync Readiness — Gaps Closed](/Research/RS-02-qlik-catalog-api/notes/qlik-two-way-sync-readiness.md) — JSON Patch semantics, ETag concurrency, and the tenant-test open items.
* [Standalone Python Sync Service — Architecture & Tech Stack](/Research/RS-07-architecture-techstack-references/outputs/architecture-and-techstack.md) — the async adapter contract and watermark-returning list_changed.
* [Connector Plugin SDK — Design Specification (v1)](/Research/RS-08-connector-plugin-sdk/outputs/connector-sdk-spec.md) — the contract this decision reconciles against RS-07.
