---
type: "Documentation"
title: "Connector SDK"
description: "The published contract every catalog connector implements, plus the shared plumbing and the conformance kit that certifies one."
tags: ["documentation", "DC-01"]
timestamp: "2026-08-20T16:30:20Z"
status: "current"
---

# Connector SDK

## Subject

The published contract every catalog connector implements, plus the shared plumbing and the conformance kit that certifies one.

## Scope

**In:** The neutral metadata model, the async Connector ABC and its contract types, the capability manifest, the canonical checksum and field envelope, HTTP/auth/config helpers, typed exceptions, secret redaction, the FakeConnector test double, and the conformance kit.

**Out:** Any individual connector's own mapping or wire code, the engine, and anything a connector chooses to do beyond the contract.

## Where the code lives

- `packages/qlabs-catalog-sync-sdk/`

## Delivered increments

### RM-01 — Upstream metadata sync MVP (Databricks to Qlik)

Completed 2026-08-20. Roadmap item: [RM-01](/Roadmap/completed/RM-01-one-way-sync-mvp/item.md).

**Shipped:** A one-way Databricks-to-Qlik metadata sync that runs as a service or a single cycle: a Unity Catalog schema becomes a Qlik data product and its tables become datasets, only fields that actually differ are written, re-running over unchanged source performs no API writes, nothing is ever deleted or activated, and no reference is invented — unresolved dataset members and unmatched owners are reported instead. Dry-run emits a reviewable JSON plan and applies nothing; identity binds only after a human confirms.

**Planned vs delivered:** Built entirely without live tenants, against mocks and hand-authored cassettes from the API research, so every tenant-specific behaviour is registered rather than assumed (28 entries, 15 blocking before production). Two dependency amendments were needed after the up-front pin (pyjwt for the JWT auth provider, pyyaml so the operator config can carry comments). Four collisions between parallel work were reconciled on main — a split exception hierarchy, a duplicated Clock, the manifest missing from the SDK surface, and an auth/HTTP protocol mismatch both connectors had independently shimmed. The conformance kit and the integration pilot each found real defects that unit tests could not: a manifest promising tags it never read, a non-idempotent update, and a per-pair schema selector that never fired and would have synced every catalog in the metastore. Two packaging bugs that made any non-editable install unusable were found only when the container installed the built wheels.

**Known gaps:** Never run against a live Databricks workspace or Qlik tenant; docs/tenant-verification.md is the pre-production gate. Activation reconciliation is implemented and tested but unreachable — wiring it would widen the connector contract, and D7 makes activation off by default anyway, so v1 never activates or deactivates. No adaptive rate-limit backoff: per-request Retry-After retry exists, but nothing lengthens a cadence under sustained 429s. A crash between Qlik confirming a brand-new product and the engine recording it can duplicate that product on retry — the dual-write problem, mitigated by creation being opt-in and off. One reporting gap remains as a documented failing test: a source field the target cannot carry is reported on the update path but not on the create path. Collibra, Snowflake and the Qlik glossary are Track B (RM-05).

**Where the code lives:**

- `packages/qlabs-catalog-sync-sdk/`
- `packages/qlabs-catalog-sync/`
- `packages/qlabs-connector-qlik/`
- `packages/qlabs-connector-databricks/`
