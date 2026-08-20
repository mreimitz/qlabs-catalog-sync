---
type: "Research Topic"
title: "Architecture, tech stack, and reference implementations"
description: "Define a standalone Python sync-service architecture and tech stack for QLabs Catalog Sync, and collect focused GitHub reference projects that already implement data-product sync, automated catalog metadata creation, or metadata maintenance."
tags: ["research", "RS-07"]
timestamp: "2026-08-06T10:04:43Z"
status: "active"
---

# Architecture, tech stack, and reference implementations

## Objective

Define a standalone Python sync-service architecture and tech stack for QLabs Catalog Sync, and collect focused GitHub reference projects that already implement data-product sync, automated catalog metadata creation, or metadata maintenance.

## Why now / what it feeds

The neutral metadata model (RS-03) is drafted; the build needs an architecture, a concrete Python tech stack, and prior art to learn from before RM-01 implementation.

## Scope

**In:** Standalone Python service architecture (endpoint adapters, identity map store, poll-based sync loop, conflict interface); tech-stack libraries; small/focused GitHub reference implementations and their patterns.

**Out:** Building on a full metadata platform (OpenMetadata/DataHub) as the backbone; large monolithic projects; final conflict policy (RS-04).

## Deliverable

Two outputs: an architecture + tech-stack design, and an annotated shortlist of reference GitHub projects.

## Success criteria

A viable Python architecture and stack are specified, and at least a handful of relevant, focused reference repos are documented with what to borrow from each.
