---
type: "Roadmap Item"
title: "Access observe and report (post-v1)"
description: "Read access/authorization state from all catalogs into a neutral, read-only access graph and report cross-catalog access and drift on data products. Deferred until after the v1 upstream metadata MVP."
tags: ["roadmap", "RM-04"]
timestamp: "2026-08-06T15:57:38Z"
status: "planned"
---

# Access observe and report (post-v1)

## Goal

Read access/authorization state from all catalogs into a neutral, read-only access graph and report cross-catalog access and drift on data products. Deferred until after the v1 upstream metadata MVP.

## Why it matters

Access sync is security-sensitive and requires an identity-resolution layer; deferring it keeps v1 shippable while preserving the RS-09 design for a safe, read-only first increment.

## Milestones

- [ ] Build the identity-resolution layer (principal correlation on email/IdP subject).
- [ ] Add read-only Principal and AccessBinding entities and connector read paths.
- [ ] Deliver access visibility and drift reporting on data products.

## Linked research

- [RS-09](/Research/RS-09-access-control-sync/topic.md)
