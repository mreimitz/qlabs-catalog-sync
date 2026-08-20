---
type: "Research Topic"
title: "Neutral metadata model and field mapping"
description: "Design a catalog-neutral internal metadata model for data products and define bidirectional field mappings between it and each endpoint (Databricks, Qlik) so new catalogs can attach later."
tags: ["research", "RS-03"]
timestamp: "2026-08-06T07:15:33Z"
status: "active"
---

# Neutral metadata model and field mapping

## Objective

Design a catalog-neutral internal metadata model for data products and define bidirectional field mappings between it and each endpoint (Databricks, Qlik) so new catalogs can attach later.

## Why now / what it feeds

A shared model decouples the sync engine from any single catalog and is required before two-way sync is meaningful.

## Scope

**In:** Core data product entity, attributes, identity/matching keys, and mapping rules to/from Databricks and Qlik; extensibility for Snowflake and Collibra.

**Out:** Concrete API client code and conflict-resolution policy (covered separately).

## Deliverable

A metadata model specification with field mapping tables per endpoint.

## Success criteria

Each endpoint's metadata fields map losslessly to and from the neutral model, or gaps are explicitly documented.
