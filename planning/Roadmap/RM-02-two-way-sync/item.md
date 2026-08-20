---
type: "Roadmap Item"
title: "Two-way sync with conflict resolution"
description: "Extend the bridge to reconcile and propagate metadata changes in both directions with a deterministic conflict-resolution policy."
tags: ["roadmap", "RM-02"]
timestamp: "2026-08-06T07:15:47Z"
status: "planned"
---

# Two-way sync with conflict resolution

## Goal

Extend the bridge to reconcile and propagate metadata changes in both directions with a deterministic conflict-resolution policy.

## Why it matters

Two-way synchronization is the core promise of QLabs Catalog Sync.

## Milestones

- [ ] Implement change detection and identity matching.
- [ ] Implement the chosen conflict-resolution policy.
- [ ] Demonstrate a reconciled bidirectional edit with no data loss.

## Linked research

- [RS-03](/Research/RS-03-neutral-metadata-model/topic.md)
- [RS-04](/Research/RS-04-sync-conflict-strategy/topic.md)
