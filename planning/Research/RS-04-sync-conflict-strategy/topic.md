---
type: "Research Topic"
title: "Two-way sync and conflict resolution strategy"
description: "Define how metadata changes are detected, reconciled, and propagated in both directions, including conflict detection, resolution policies, and change tracking."
tags: ["research", "RS-04"]
timestamp: "2026-08-06T07:15:33Z"
status: "active"
---

# Two-way sync and conflict resolution strategy

## Objective

Define how metadata changes are detected, reconciled, and propagated in both directions, including conflict detection, resolution policies, and change tracking.

## Why now / what it feeds

Two-way sync is the core value of the bridge and depends on the metadata model and endpoint capabilities.

## Scope

**In:** Change detection, matching/identity, conflict policies (last-write-wins, source-of-truth, manual), idempotency, and sync scheduling.

**Out:** Endpoint API mechanics and the neutral model definition (covered separately).

## Deliverable

A sync-and-conflict strategy design note with chosen default policy and rationale.

## Success criteria

A worked example shows a bidirectional edit reconciled deterministically without data loss.
