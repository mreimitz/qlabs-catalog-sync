---
type: "Roadmap Item"
title: "Upstream metadata sync MVP (Databricks to Qlik)"
description: "Ship the v1 upstream-only metadata sync: read data product metadata from Databricks and write it into Qlik through the neutral model. Qlik is the sole writer; no two-way sync and no access-control sync in v1."
tags: ["roadmap", "RM-01"]
timestamp: "2026-08-20T16:30:20Z"
status: "done"
---

# Upstream metadata sync MVP (Databricks to Qlik)

## Goal

Ship a working v1 that syncs data product metadata **upstream** from **Databricks** into **Qlik**,
through the RS-03 neutral model and the RS-08 connector SDK. Qlik is the only write target; source
connectors are read-only. Further source connectors follow in
[RM-05](/Roadmap/RM-05-track-b-connectors-glossary/item.md) on the same contract.

## Why it matters

Delivers immediate cross-catalog value with the simplest safe design: one writer, no bidirectional
conflict engine, and Qlik's lack of change events made irrelevant for the sync direction. It proves
the connector SDK and neutral model end to end and sets up later phases (two-way, more endpoints,
access).

## Scope

This item is exactly the Databricks-to-Qlik flow, ending at a tagged v0.1. The Collibra and
Snowflake read connectors and the Qlik glossary write path were originally Track B inside this item;
they are now [RM-05](/Roadmap/RM-05-track-b-connectors-glossary/item.md), so this one completes at
the point the software actually ships. The full scope, sequencing, mapping decisions, and per-task
model recommendations are in the implementation plan and the MVP decision. Access-control
synchronization is explicitly deferred to RM-04.

The executable board is [tools/agent-plan/tasks.json](/tools/agent-plan/tasks.json), 52 tasks
across WP0-WP4 and WP7-WP9.

## Milestones

- [x] Repair the repository gate and pin every runtime dependency up front.
- [x] Extract the connector SDK (contract, neutral model, helpers, conformance kit, fake connector).
- [x] Build the engine core (discovery, state store, upstream sync loop, scheduler, dry-run).
- [x] Build the Qlik write connector (data products) and the Databricks read connector (UC schemas).
- [x] Pilot the Databricks-to-Qlik data-product sync, prove idempotency and restart safety.
- [x] Package, deploy, and release v0.1.
- [x] Record the delivery in `Docu/` and retire this item to `Roadmap/completed/`.

## Plan and decisions

- [v1 Implementation Plan (Work Packages)](implementation-plan.md)
- [Decision: v1 scope — upstream-only, no access control](decision.md)
- [Decision: MVP is Databricks-to-Qlik, and how the two models map](decision-databricks-to-qlik-mvp.md)

## Linked research

- [RS-01 Databricks](/Research/RS-01-databricks-catalog-api/topic.md)
- [RS-02 Qlik](/Research/RS-02-qlik-catalog-api/topic.md)
- [RS-03 Neutral model](/Research/RS-03-neutral-metadata-model/topic.md)
- [RS-07 Architecture](/Research/RS-07-architecture-techstack-references/topic.md)
- [RS-08 Connector SDK](/Research/RS-08-connector-plugin-sdk/topic.md)

## Follow-on

- [RM-05 Track B source connectors and the Qlik glossary](/Roadmap/RM-05-track-b-connectors-glossary/item.md)
  — Collibra, Snowflake, and the glossary write path, on the contract this item freezes.
