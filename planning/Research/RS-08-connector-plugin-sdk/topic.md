---
type: "Research Topic"
title: "Connector plugin SDK and extensibility"
description: "Design a Python connector SDK and plugin framework so each catalog (Databricks, Qlik, Snowflake, Collibra, and future systems) is an independently packaged, entry-point-discovered plugin implementing a stable, versioned contract."
tags: ["research", "RS-08"]
timestamp: "2026-08-06T10:43:27Z"
status: "active"
---

# Connector plugin SDK and extensibility

## Objective

Design a Python connector SDK and plugin framework so each catalog (Databricks, Qlik, Snowflake, Collibra, and future systems) is an independently packaged, entry-point-discovered plugin implementing a stable, versioned contract.

## Why now / what it feeds

The architecture (RS-07) and neutral model (RS-03) are drafted; the extensibility mechanism must be designed before RM-01 so connectors are pluggable from the start and RM-03 can add endpoints without core changes.

## Scope

**In:** Entry-point plugin discovery/loading; SDK base classes and helpers; connector contract and lifecycle; capability negotiation; config/secrets injection; SDK versioning/compatibility; conformance test kit; example connector skeleton.

**Out:** Out-of-process/gRPC plugins; per-vendor API details (covered in RS-01/02/05/06); the final conflict policy (RS-04).

## Deliverable

A connector plugin SDK design spec with the contract, discovery mechanism, versioning policy, conformance kit, and a minimal connector example.

## Success criteria

A new connector can be built as a separate pip package against the documented SDK, auto-discovered by the core, and certified by the conformance kit without modifying the engine.
