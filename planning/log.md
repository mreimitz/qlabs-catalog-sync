# Project Knowledge Update Log

## 2026-08-06

* **Rebrand**: Repurposed the scaffold into the QLabs Catalog Sync project — a two-way data product metadata bridge (Databricks ↔ Qlik, future Snowflake/Collibra endpoints).
* **Cleanup**: Removed the vendored `plugin/research-scaffold/` package; it was scaffold tooling, not project content.
* **Structure**: Seeded initial Roadmap items and Research topics for the sync bridge.
* **Research**: Added RS-05 (Snowflake) and RS-06 (Collibra) topics; documented detailed catalog + data-product API references (assets, data products, auth, CRUD, and semantic layer) for Databricks, Qlik, Snowflake, and Collibra in each topic's outputs.
* **Access control**: Added RS-09 with per-vendor access-model notes and a sync-options synthesis; recorded the v1-scope decision (upstream-only, Qlik sole writer, no access-control sync) and added RM-04 for the deferred access phase.
* **Planning**: Wrote the RS-03 neutral model, RS-07 architecture/tech-stack + reference projects, RS-08 connector SDK, the RM-01 WP implementation plan, the machine-readable task board (tools/agent-plan), and the coding-agent guide.
* **Restructure**: Relocated this entire OKF bundle into the repository's `planning/` subfolder so the repository root can host the standalone Python code monorepo; OKF now governs `planning/` only, and the code project at the root is governed separately.

## 2026-07-24

* **Migration**: Converted the scaffold into a repository-wide OKF v0.1 knowledge bundle.
* **Validation**: Added the strict Research Scaffold OKF Profile and offline validator.
* **Automation**: Added transactional RS/RM generators, deterministic indexes, and tag allocation.
* **Enforcement**: Wired validation into Claude hooks, pre-commit, and GitHub Actions.
* **Testing**: Added unit, guard, and end-to-end generator coverage.
* **Tooling**: Added local file and recursive-directory intake backed by Microsoft MarkItDown.
* **Boundary**: Kept non-OKF scaffold tooling under a Markdown-free `tools/` directory.
