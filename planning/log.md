# Project Knowledge Update Log

## 2026-08-20

* **Structure**: Added `Docu/` as a fourth knowledge domain, organized by subject with `DC-NN` tags, and `Roadmap/completed/` as the home for finished roadmap items.
* **Lifecycle**: Made the implementation path a hard rule — build what the roadmap specifies, record what shipped in `Docu/`, then retire the item — enforced by PROFILE032 and PROFILE035 through PROFILE039, with the first two blocking at the pre-write hook.
* **Automation**: Added the `new-docu` and `complete-roadmap` generators and the read-only `check-references` scan. Completion refuses unless the bundle validates, every task on the item's board is done, and a documentation subject is named to receive the delivery.
* **Fixes**: `sync-indexes` wrote parent indexes before their children existed, so a newly created subdirectory was never listed; the tag registry raised on any domain it predated.
* **Scope split**: Split RM-01's Track B — Collibra, Snowflake, and the Qlik glossary write path — into RM-05 with its own task board, so RM-01 completes when v0.1 actually ships rather than waiting on work that starts afterwards.
* **Tooling**: `complete-roadmap` now re-points bundle Markdown links to a moved item in the same transaction, so a later roadmap item may link an earlier one without blocking its completion. `ready_queue.py` now loads every `tasks*.json` board so cross-item dependencies resolve, and takes `--roadmap` to scope output to one item.

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
