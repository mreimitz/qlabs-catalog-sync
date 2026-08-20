---
type: "Roadmap Item"
title: "Upstream metadata sync MVP (sources to Qlik)"
description: "Ship the v1 upstream-only metadata sync: read data product metadata from source catalogs and write it into Qlik through the neutral model. Qlik is the sole writer; no two-way sync and no access-control sync in v1."
tags: ["roadmap", "RM-01"]
timestamp: "2026-08-06T12:30:00Z"
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

The v1 scope, sequencing, and per-task model recommendations are detailed in the implementation plan.
Access-control synchronization is explicitly deferred to RM-04.

## Milestones

- [ ] Extract the connector SDK (contract, neutral model, helpers, conformance kit).
- [ ] Build the engine core (discovery, state store, upstream sync loop, scheduler).
- [ ] Build the Qlik write connector and the Databricks read connector.
- [ ] Pilot a Databricks-to-Qlik data-product sync; add Collibra glossary and Snowflake sources.
- [ ] Package, deploy, and release v0.1.

## Plan and decisions

- [v1 Implementation Plan (Work Packages)](implementation-plan.md)
- [Decision: v1 scope — upstream-only, no access control](decision.md)

## Linked research

- [RS-01 Databricks](/Research/RS-01-databricks-catalog-api/topic.md)
- [RS-02 Qlik](/Research/RS-02-qlik-catalog-api/topic.md)
- [RS-03 Neutral model](/Research/RS-03-neutral-metadata-model/topic.md)
- [RS-05 Snowflake](/Research/RS-05-snowflake-catalog-api/topic.md)
- [RS-06 Collibra](/Research/RS-06-collibra-catalog-api/topic.md)
- [RS-07 Architecture](/Research/RS-07-architecture-techstack-references/topic.md)
- [RS-08 Connector SDK](/Research/RS-08-connector-plugin-sdk/topic.md)
