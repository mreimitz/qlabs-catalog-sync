---
type: "Roadmap Item"
title: "Upstream metadata sync MVP (sources to Qlik)"
description: "Ship the v1 upstream-only metadata sync: read data product metadata from source catalogs and write it into Qlik through the neutral model. Qlik is the sole writer; no two-way sync and no access-control sync in v1."
tags: ["roadmap", "RM-01"]
timestamp: "2026-08-20T10:00:00Z"
status: "active"
---

# Upstream metadata sync MVP (sources to Qlik)

## Goal

Ship a working v1 that syncs data product metadata **upstream** from source catalogs (Databricks
first, then Collibra and Snowflake) into **Qlik**, through the RS-03 neutral model and the RS-08
connector SDK. Qlik is the only write target; source connectors are read-only.

## Why it matters

Delivers immediate cross-catalog value with the simplest safe design: one writer, no bidirectional
conflict engine, and Qlik's lack of change events made irrelevant for the sync direction. It proves
the connector SDK and neutral model end to end and sets up later phases (two-way, more endpoints,
access).

## Scope

The MVP is the Databricks-to-Qlik flow; Collibra and Snowflake are Track B within this same item and
start only once v0.1 is tagged. The full scope, sequencing, mapping decisions, and per-task model
recommendations are in the implementation plan and the MVP decision. Access-control synchronization
is explicitly deferred to RM-04.

## Milestones

**Track A — the MVP (Databricks to Qlik), ships v0.1:**

- [ ] Repair the repository gate and pin every runtime dependency up front.
- [ ] Extract the connector SDK (contract, neutral model, helpers, conformance kit, fake connector).
- [ ] Build the engine core (discovery, state store, upstream sync loop, scheduler, dry-run).
- [ ] Build the Qlik write connector (data products) and the Databricks read connector (UC schemas).
- [ ] Pilot the Databricks-to-Qlik data-product sync, prove idempotency and restart safety.
- [ ] Package, deploy, and release v0.1.

**Track B — after v0.1:**

- [ ] Qlik glossary write path plus the Collibra read connector and its glossary pilot.
- [ ] Snowflake read connector and its pilot.

## Plan and decisions

- [v1 Implementation Plan (Work Packages)](implementation-plan.md)
- [Decision: v1 scope — upstream-only, no access control](decision.md)
- [Decision: MVP is Databricks-to-Qlik, and how the two models map](decision-databricks-to-qlik-mvp.md)

## Linked research

- [RS-01 Databricks](/Research/RS-01-databricks-catalog-api/topic.md)
- [RS-02 Qlik](/Research/RS-02-qlik-catalog-api/topic.md)
- [RS-03 Neutral model](/Research/RS-03-neutral-metadata-model/topic.md)
- [RS-05 Snowflake](/Research/RS-05-snowflake-catalog-api/topic.md)
- [RS-06 Collibra](/Research/RS-06-collibra-catalog-api/topic.md)
- [RS-07 Architecture](/Research/RS-07-architecture-techstack-references/topic.md)
- [RS-08 Connector SDK](/Research/RS-08-connector-plugin-sdk/topic.md)
