---
type: "Project"
title: "QLabs Catalog Sync"
description: "A two-way bridge that synchronizes data product metadata across data catalogs, starting with Databricks and Qlik."
tags: ["project", "data-catalog", "metadata-sync", "databricks", "qlik"]
timestamp: "2026-08-20T10:45:00Z"
status: "active"
---

# QLabs Catalog Sync

QLabs Catalog Sync is a two-way integration bridge that keeps data product metadata
consistent across data catalogs. The first release synchronizes metadata entries between
**Databricks** and **Qlik**. The architecture is endpoint-based so that additional catalogs —
the **Snowflake** data catalog, **Collibra**, and others — can be added later without changing
the core sync engine.

The repository root is an Open Knowledge Format bundle. Every Markdown document below it is
either an OKF concept or a reserved `index.md` or `log.md` file. During this phase the project
is organized as research and planning; it will grow into a code project as the design settles.

## What it does

- Reads data product metadata from a source catalog and writes it to a target catalog.
- Reconciles metadata in both directions, detecting and resolving conflicts.
- Uses a neutral internal metadata model so each catalog is a pluggable endpoint.
- Begins with the Databricks ↔ Qlik pair; future endpoints attach to the same model.

## Knowledge domains

- [Research](/Research/) contains tagged investigations — catalog APIs, the metadata model,
  and sync/conflict strategy — with their sources, notes, and outputs.
- [Roadmap](/Roadmap/) contains the master plan and tagged initiatives that sequence the build.
  Finished initiatives move to `Roadmap/completed/`.
- [Documentation](/Docu/) records what has actually been built, one tagged subject per part of
  the system.
- [Claude controls](/.claude/) contains the commands, skills, hooks, templates, and profile that
  keep the knowledge tree valid.

## Daily workflows

- Use `/new-research` to scope and create an `RS-NN` research topic.
- Use `/doc-intake` to convert a local file or directory into a new `RS-NN` topic.
- Use `/new-roadmap` to create an `RM-NN` roadmap item.
- Use `/new-docu` to create a `DC-NN` documentation subject.
- Use `/complete-roadmap` to retire a finished roadmap item: it records the delivery in `Docu/`
  and moves the item into `Roadmap/completed/`.
- Use `/research-status` to inspect current work.
- Use `/validate-okf` to validate official OKF and the strict local profile.
- Use `/sync-okf` to regenerate managed indexes and the master roadmap view.

## Stable tags

Research folders use `RS-NN-short-slug`, roadmap folders `RM-NN-short-slug`, and documentation
folders `DC-NN-short-slug`. Numbers are zero-padded, allocated atomically, and never reused — a
completed roadmap item keeps its number after moving to `Roadmap/completed/`.

## Project tooling

Non-OKF helper code lives under `tools/`. It is intentionally absent from the knowledge indexes
and may not contain Markdown. As the sync engine takes shape, its implementation will live in a
dedicated code area outside the OKF knowledge graph.
