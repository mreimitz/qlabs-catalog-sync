# Upstream metadata sync MVP (sources to Qlik)

## Concepts

* [Coding Agent Guide — QLabs Catalog Sync v1 Build](agent-guide.md) - Conventions, task-board usage, worktree and ownership rules, how-to-add-a-connector checklist, and PR rules for coding agents building the Databricks-to-Qlik MVP and the RM-01 upstream sync.
* [Decision: MVP is Databricks-to-Qlik, and how the two models map](decision-databricks-to-qlik-mvp.md) - Narrows the RM-01 MVP to a single Databricks-to-Qlik flow and locks the eight mapping decisions the build needs: UC schema as the data product, no dataset creation in Qlik, owner resolution, no deletes, no glossary, SQL-gated tags, opt-in activation, and an async contract with watermark-returning list_changed.
* [Decision: v1 scope — upstream-only, no access-control sync](decision.md) - Scopes v1 to upstream-only metadata sync with Qlik as the sole writer, defers two-way sync and access-control sync, and treats owners as best-effort metadata.
* [v1 Implementation Plan — Work Packages, Tasks & Model Recommendations](implementation-plan.md) - Executable build plan for the Databricks-to-Qlik upstream sync MVP, with locked mapping decisions, per-task dependencies and file ownership, parallelization waves, acceptance gates, and a recommended model per task.
* [Upstream metadata sync MVP (sources to Qlik)](item.md) - Ship the v1 upstream-only metadata sync: read data product metadata from source catalogs and write it into Qlik through the neutral model. Qlik is the sole writer; no two-way sync and no access-control sync in v1.
