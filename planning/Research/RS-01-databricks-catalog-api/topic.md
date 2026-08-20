---
type: "Research Topic"
title: "Databricks Unity Catalog metadata API"
description: "Understand how to read and write data product metadata (descriptions, tags, owners, classifications, schema-level annotations) in Databricks Unity Catalog, including auth, rate limits, and supported fields."
tags: ["research", "RS-01"]
timestamp: "2026-08-06T07:15:32Z"
status: "active"
---

# Databricks Unity Catalog metadata API

## Objective

Understand how to read and write data product metadata (descriptions, tags, owners, classifications, schema-level annotations) in Databricks Unity Catalog, including auth, rate limits, and supported fields.

## Why now / what it feeds

Databricks is one of the two initial sync endpoints; the read/write surface defines what QLabs Catalog Sync can move.

## Scope

**In:** Unity Catalog REST APIs and SDK for catalogs, schemas, tables, and data products; metadata fields; authentication; pagination and limits.

**Out:** Non-metadata data movement, lineage compute, and catalogs other than Databricks.

## Deliverable

A capability memo mapping Databricks metadata fields to read/write operations, with auth and limit notes.

## Success criteria

Every metadata field the bridge intends to sync is confirmed as readable and writable via a documented API call.
