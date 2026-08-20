---
type: "Research Topic"
title: "Snowflake data catalog metadata API"
description: "Understand how to read and write data product metadata in Snowflake (Horizon Catalog / Internal Marketplace data products), including auth, supported fields, and API surface."
tags: ["research", "RS-05"]
timestamp: "2026-08-06T07:19:34Z"
status: "active"
---

# Snowflake data catalog metadata API

## Objective

Understand how to read and write data product metadata in Snowflake (Horizon Catalog / Internal Marketplace data products), including auth, supported fields, and API surface.

## Why now / what it feeds

Snowflake is a planned future endpoint for the sync bridge; its catalog surface must be understood to design the neutral model for extensibility.

## Scope

**In:** Snowflake Horizon Catalog objects, data products/listings, tags, and metadata; SQL and REST API surfaces; authentication; limits.

**Out:** Snowflake compute/query tuning and non-catalog features.

## Deliverable

A capability memo mapping Snowflake metadata fields to read/write operations, with auth and limit notes.

## Success criteria

Every metadata field the bridge intends to sync is confirmed as readable and writable via a documented API call, or gaps are documented.
