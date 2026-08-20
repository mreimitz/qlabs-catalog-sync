---
type: "Research Topic"
title: "Collibra catalog metadata API"
description: "Understand how to read and write data product metadata in Collibra (Data Marketplace / Data Products, assets, communities, domains), including auth and API surface."
tags: ["research", "RS-06"]
timestamp: "2026-08-06T07:19:34Z"
status: "active"
---

# Collibra catalog metadata API

## Objective

Understand how to read and write data product metadata in Collibra (Data Marketplace / Data Products, assets, communities, domains), including auth and API surface.

## Why now / what it feeds

Collibra is a planned future endpoint; its governance-centric model informs the neutral metadata model's extensibility.

## Scope

**In:** Collibra assets, data products, communities/domains, attributes/relations; Core REST API and GraphQL; authentication; limits.

**Out:** Collibra workflow engine internals and non-catalog governance features.

## Deliverable

A capability memo mapping Collibra metadata fields to read/write operations, with auth and limit notes.

## Success criteria

Every metadata field the bridge intends to sync is confirmed as readable and writable via a documented API call, or gaps are documented.
