---
type: "Roadmap Item"
title: "Pluggable endpoint framework for future catalogs"
description: "Generalize the endpoint interface so new catalogs (Snowflake data catalog, Collibra, others) can be added without changing the core sync engine."
tags: ["roadmap", "RM-03"]
timestamp: "2026-08-06T07:15:48Z"
status: "planned"
---

# Pluggable endpoint framework for future catalogs

## Goal

Generalize the endpoint interface so new catalogs (Snowflake data catalog, Collibra, others) can be added without changing the core sync engine.

## Why it matters

Extensibility to more catalogs is the long-term direction of the product.

## Milestones

- [ ] Define the endpoint interface contract against the neutral model.
- [ ] Refactor Databricks and Qlik into conforming endpoint plugins.
- [ ] Prototype a third endpoint to validate the abstraction.

## Linked research

- [RS-03](/Research/RS-03-neutral-metadata-model/topic.md)
