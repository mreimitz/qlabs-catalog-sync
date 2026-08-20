# Upstream metadata sync MVP (sources to Qlik)

## Concepts

* [Coding Agent Guide — QLabs Catalog Sync v1 Build](agent-guide.md) - Conventions, task-board usage, how-to-add-a-connector checklist, and PR/ownership rules for coding agents building the v1 upstream sync.
* [Decision: v1 scope — upstream-only, no access-control sync](decision.md) - Scopes v1 to upstream-only metadata sync with Qlik as the sole writer, defers two-way sync and access-control sync, and treats owners as best-effort metadata.
* [v1 Implementation Plan — Work Packages, Tasks & Model Recommendations](implementation-plan.md) - Complete WP-structured build plan for the upstream metadata sync MVP, with per-task dependencies, parallelization waves, acceptance criteria, and a recommended model per task.
* [Upstream metadata sync MVP (sources to Qlik)](item.md) - Ship the v1 upstream-only metadata sync: read data product metadata from source catalogs and write it into Qlik through the neutral model. Qlik is the sole writer; no two-way sync and no access-control sync in v1.
