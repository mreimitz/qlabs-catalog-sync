---
type: "Documentation"
title: "Sync Engine"
description: "The process that runs a sync: connector discovery, configuration, the state store, the upstream cycle, identity resolution, diffing, scheduling, observability and the CLI."
tags: ["documentation", "DC-02"]
timestamp: "2026-08-20T16:30:02Z"
status: "draft"
---

# Sync Engine

## Subject

The process that runs a sync: connector discovery, configuration, the state store, the upstream cycle, identity resolution, diffing, scheduling, observability and the CLI.

## Scope

**In:** Entry-point discovery and the contract-version gate, engine config and the sync-pair schema, the SQLAlchemy state store and its migration, the single-transaction sync loop, the identity map and its confirmation-gated bootstrap, the field diff engine, the manual-edit policy, orphan reporting, the scheduler, structured logging and metrics, and the run/dry-run/identity-confirm/serve commands.

**Out:** Connector-specific behaviour, the vendor APIs themselves, and the two-way conflict engine deferred to RM-02.

## Where the code lives

- `packages/qlabs-catalog-sync/`

## Delivered increments

No delivered increments have been recorded yet.
