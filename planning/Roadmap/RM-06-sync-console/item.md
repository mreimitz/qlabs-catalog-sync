---
type: "Roadmap Item"
title: "Catalog sync console: endpoint management and sync selection"
description: "Ship the operator console for the MVP: configure and manage connector endpoints and sync pairs from a browser, decide exactly which source objects sync through an ordered include/exclude rule set, preview the planned writes before applying them, and watch runs. Configuration moves from environment variables into the state database; credentials stay external and are only referenced."
tags: ["roadmap", "RM-06"]
timestamp: "2026-08-20T12:34:07Z"
status: "planned"
---

# Catalog sync console: endpoint management and sync selection

## Goal

Ship the operator console for the MVP: configure and manage connector endpoints and sync pairs from a browser, decide exactly which source objects sync through an ordered include/exclude rule set, preview the planned writes before applying them, and watch runs. Configuration moves from environment variables into the state database; credentials stay external and are only referenced.

## Why it matters

Without it the MVP is configurable only by editing environment variables and restarting a container, and the only way to learn what a selection change will do is to run it against a customer tenant. The console makes the blast radius of a scope change visible before anything is written, which is the difference between a tool an operator trusts and one they do not.

## Scope

This item is the operator-facing half of the MVP: the configuration store the console writes to, the
selection rule engine, the REST API over the engine, and the SPA itself. It builds directly on the
engine RM-01 produces — the state store, the sync loop, the scheduler and the dry-run planner — so
its tasks depend on RM-01's tasks rather than running beside them. The configuration store depends
only on the state store and config loading, both already done, so it is ready now; everything after
it waits on [WP2](/Roadmap/RM-01-one-way-sync-mvp/implementation-plan.md).

**It is part of the MVP.** v0.1 is not tagged until this item's board is finished: RM-01's release
task depends on it. The two items are sequential, not parallel.

The executable board is [tools/agent-plan/tasks-rm-06.json](/tools/agent-plan/tasks-rm-06.json),
28 tasks across WP10-WP14.

Out of scope for v0.1, deliberately: per-field selection, installing connector *packages* from the
browser (endpoints are instances of connectors already present in the image), OIDC and a role model,
configuration export/import, and browser-driven end-to-end tests.

## Plan and decisions

- [Console and selection implementation plan (Work Packages WP10-WP14)](implementation-plan.md)
- [Decision: configuration lives in the state store, and selection is an ordered rule set](decision-console-config-and-selection.md)

## Milestones

- [ ] Move endpoints, sync pairs and selection rules into the state store, with credentials held outside it as named references.
- [ ] Build the selection rule engine, and use the same evaluator for the live preview and the real sync.
- [ ] Expose the engine over a typed REST API with a generated TypeScript client and single-admin authentication.
- [ ] Build the console SPA on the @elabs-ai component packages: endpoints, pairs, selection, dry-run and runs.
- [ ] Ship the console inside the engine container and pilot the Databricks-to-Qlik sync entirely through it.
- [ ] Record the delivery in Docu/ and retire this item to Roadmap/completed/.

## Linked research

- [RS-07](/Research/RS-07-architecture-techstack-references/topic.md)
- [RS-08](/Research/RS-08-connector-plugin-sdk/topic.md)
- [RS-03](/Research/RS-03-neutral-metadata-model/topic.md)
- [RS-01](/Research/RS-01-databricks-catalog-api/topic.md)

## Relationship to RM-01

- [RM-01 Upstream metadata sync MVP](/Roadmap/RM-01-one-way-sync-mvp/item.md) — the engine this
  console configures. Its task T9.4 (tag v0.1) depends on this item's final task, so the two ship
  together.
