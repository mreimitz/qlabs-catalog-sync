---
type: "Research Topic"
title: "Qlik catalog metadata API"
description: "Understand how to read and write data product metadata in Qlik (Qlik Cloud data catalog / Talend), including auth, supported fields, and API surface."
tags: ["research", "RS-02"]
timestamp: "2026-08-06T07:15:33Z"
status: "active"
---

# Qlik catalog metadata API

## Objective

Understand how to read and write data product metadata in Qlik (Qlik Cloud data catalog / Talend), including auth, supported fields, and API surface.

## Why now / what it feeds

Qlik is the second initial sync endpoint; its metadata surface must be paired with Databricks for the first release.

## Scope

**In:** Qlik Cloud catalog and data product metadata APIs; fields; authentication; rate limits and pagination.

**Out:** Qlik app/dashboard development, reload orchestration, and non-Qlik catalogs.

## Deliverable

A capability memo mapping Qlik metadata fields to read/write operations, with auth and limit notes.

## Success criteria

Every metadata field the bridge intends to sync is confirmed as readable and writable via a documented API call.
