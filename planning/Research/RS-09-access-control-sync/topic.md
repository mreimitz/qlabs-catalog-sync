---
type: "Research Topic"
title: "Access control and authorization sync"
description: "Understand each catalog's authorization model (principals, groups, roles, spaces, grants) and its read/write API for permissions, then determine realistic options for synchronizing access control across catalogs."
tags: ["research", "RS-09"]
timestamp: "2026-08-06T10:47:38Z"
status: "active"
---

# Access control and authorization sync

## Objective

Understand each catalog's authorization model (principals, groups, roles, spaces, grants) and its read/write API for permissions, then determine realistic options for synchronizing access control across catalogs.

## Why now / what it feeds

Access control is a security-sensitive dimension not yet researched; feasibility and risk shape whether and how it belongs in the neutral model and connector SDK before RM-01.

## Scope

**In:** Per-vendor authorization models and permission read/write APIs (Databricks, Qlik, Snowflake, Collibra); principal/identity models; cross-vendor identity mapping; sync options and risks.

**Out:** Building an identity provider; data-row/column masking policy engines beyond metadata; final conflict policy (RS-04).

## Deliverable

Per-vendor access-control notes plus a synthesis of sync options with risks and a recommendation.

## Success criteria

Each vendor's permission model and API is documented, and a clear, risk-aware recommendation for access-control sync scope is produced.
