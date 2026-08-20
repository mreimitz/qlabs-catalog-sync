---
type: "Agent Instruction"
title: "QLabs Catalog Sync Operating Rules"
description: "Mandatory structure, OKF conformance, research, and roadmap rules for agents on the QLabs Catalog Sync project."
tags: ["agent", "instruction", "okf"]
timestamp: "2026-08-20T10:45:00Z"
status: "active"
---

# QLabs Catalog Sync Operating Rules

This concept governs how Claude Cowork, Claude Code, and other agents behave in this project.
These are hard rules. Hooks, generators, pre-commit, and CI enforce them, but agents must follow
them proactively.

---

## 1. Purpose

This repository is the knowledge bundle for **QLabs Catalog Sync** — a two-way bridge that
synchronizes data product metadata across data catalogs, starting with Databricks and Qlik and
extending later to Snowflake, Collibra, and other endpoints.

It is one Open Knowledge Format v0.1 bundle. Four knowledge domains exist:

- **`Research/`** — where investigation happens (catalog APIs, the metadata model, sync and
  conflict strategy). One subfolder per research topic.
- **`Roadmap/`** — where plans, sequencing, and intent for building the bridge are documented.
  Finished items move to `Roadmap/completed/`.
- **`Docu/`** — what has actually been built, organized by subject. One subfolder per part of
  the system.
- **`.claude/`** — agent controls, generation templates, and conformance tooling.

Work flows one way through them: research informs a roadmap item, the roadmap item is built,
and on completion the delivery is recorded in `Docu/` and the item is retired to
`Roadmap/completed/`. Section 5 makes that a hard rule.

While the design is being worked out, this bundle stays research- and planning-shaped. It will
later grow a code project for the sync engine and its catalog endpoints.

Every `.md` file is either an OKF concept with strict frontmatter or a reserved `index.md` /
`log.md`. Use `python3 .claude/scripts/okf.py validate` after material changes.

`tools/` is scaffold infrastructure, not an OKF knowledge domain. It contains code, configuration,
and tests only. Markdown is forbidden under `tools/`, and the root knowledge index must not list it.

---

## 2. Tagging convention (MANDATORY)

Every research topic, roadmap item, and documentation subject gets a stable, zero-padded tag.

| Domain        | Tag prefix | Example  | Meaning                |
| ------------- | ---------- | -------- | ---------------------- |
| Research      | `RS`       | `RS-01`  | Research item 1        |
| Roadmap       | `RM`       | `RM-01`  | Roadmap item 1         |
| Documentation | `DC`       | `DC-01`  | Documentation subject 1|

Rules:
- Folder names are `RS-NN-short-slug` / `RM-NN-short-slug` / `DC-NN-short-slug`
  (e.g. `RS-04-token-pricing-models`).
- `NN` is two digits, zero-padded, **never reused** even after a topic is archived. A completed
  roadmap item keeps its number forever; moving it to `Roadmap/completed/` does not free it.
- The tag is the primary key. Reference items by tag in notes, commits, and roadmap entries
  (e.g. "blocked by RS-03", "feeds RM-02").
- Tags are allocated by `.claude/scripts/okf.py`; do not create or reuse them manually.

---

## 3. Research folder rules (HARD RULE — enforced by hook)

> **Every research document MUST live inside a `Research/RS-NN-*/` topic folder.**
> Writing a file directly into `Research/` (loose, outside a topic folder) is forbidden.

- Start topics with `/new-research`; the transactional generator creates the complete structure.
- All sources, notes, and outputs for that topic stay inside its folder:
  - `sources/` — raw captured material (PDFs, fetched pages, exports, transcripts).
  - `notes/` — your working notes, synthesis, intermediate thinking.
  - `outputs/` — finished deliverables (memos, tables, decks, briefs).
- `topic.md` is the authoritative topic concept; `index.md` is its navigation front door.
- Non-Markdown source artifacts require a same-stem `Source Reference` Markdown concept.
- Research notes and outputs require a `# Citations` section, using `None.` when appropriate.
- Do not scatter a topic's files across multiple folders. One topic = one folder = one tag.

The pre-write hook rejects invalid documents. The post-write validator detects cross-file
violations. Fix violations rather than bypassing either layer.

---

## 4. Roadmap folder rules (HARD RULE — enforced by hook)

- `Roadmap/roadmap.md` is the **single master plan concept**. It holds the live list of all `RM` items
  and links to all active `RS` topics. Keep it current.
- Detailed planning for any item goes in its own `Roadmap/RM-NN-<slug>/` subfolder, never loose in `Roadmap/`.
- `index.md` and `roadmap.md` are the only Markdown files allowed directly in `Roadmap/`.
- Each roadmap item links to the research it depends on or produces (by `RS-NN` tag).
- `Roadmap/` holds only unfinished work. Completed items live in `Roadmap/completed/RM-NN-<slug>/`
  and are put there by the generator, never by hand.

---

## 5. Implementation lifecycle (HARD RULE — enforced by hook and validator)

> **Every piece of implementation work follows the same path: it is on the roadmap, it gets
> built, its delivery is documented in `Docu/`, and the roadmap item moves to
> `Roadmap/completed/`. An item is not finished until all four have happened.**

1. **Plan it.** Work that is not an `RM-NN` roadmap item does not get built. Create it with
   `/new-roadmap`.
2. **Build it.** Execute against the item's task board in `tools/agent-plan/tasks.json`. A task
   is done only after its `verify` command passes.
3. **Document it.** `Docu/` is organized by subject, not by roadmap item — one folder per part
   of the system, created with `/new-docu`. A documentation concept records **what shipped
   versus what was planned**: the delivery, how it differed from the plan, where the code lives,
   and what was deliberately left out. One roadmap item usually writes into several subjects.
4. **Retire it.** Run `/complete-roadmap`. In one transaction it moves the item folder to
   `Roadmap/completed/`, sets its status to `done`, ticks its milestones, writes a
   `### RM-NN` increment into each named documentation subject, and revalidates the bundle.

The generator refuses to complete an item while the bundle is invalid, while any task on its
board is unfinished, or without at least one documentation subject to record the delivery.

Enforcement is mechanical, not advisory:

- `PROFILE032` — a roadmap item with status `done` outside `Roadmap/completed/`.
- `PROFILE035` — an item under `Roadmap/completed/` whose status is not `done`.
- `PROFILE036` — a completed item that no documentation subject records.
- `PROFILE037` / `PROFILE039` — documentation missing or not filling its
  `## Delivered increments` section.
- `PROFILE038` — an increment that does not link the item it claims to document.

The first two reach the pre-write hook, so flipping a status by hand is blocked at the moment
of the edit rather than discovered later.

After completing an item, apply the stale-reference report the generator prints — it lists the
repository-root guides and task-board `inputs` entries that still point at the old path — and
confirm with `check-references` that none remain.

---

## 6. Workflow for a new research topic

When the user wants to start research, **run the intake** rather than guessing scope.

1. Trigger the intake skill (`/new-research`, or the `research-intake` skill). It asks a short,
   editable sequence of questions to pin down objective, scope, sources, deliverable, and success criteria.
2. Allocate the next free `RS-NN`.
3. Run the generator to allocate and atomically create `Research/RS-NN-<slug>/`.
4. Confirm `topic.md`, indexes, subdirectories, and log are complete.
5. Synchronize `Roadmap/roadmap.md` and managed indexes.
6. Only then begin gathering sources and writing notes — inside the new folder.

For file-driven intake, `/doc-intake` asks for a research-entry name, converts one local file or a
directory recursively, and creates the complete RS topic only after every visible file converts.

---

## 7. Working style

- Capture sources before synthesizing; keep a provenance trail in `sources/` so claims are traceable.
- Distinguish raw capture (`sources/`), thinking (`notes/`), and deliverables (`outputs/`).
- When citing web material, paraphrase; never paste large verbatim passages.
- Cross-link by tag. The tag graph (which RS feeds which RM) is the project's memory.
- Keep `roadmap.md`, `topic.md`, and `item.md` statuses honest.
- Update a concept's UTC `timestamp` whenever its meaning changes.
- Prefer bundle-root links; for example, an `RS-03` topic links to its repository-root concept path.

---

## 8. What NOT to do

- ❌ Do not write loose files into `Research/` or `Roadmap/` roots.
- ❌ Do not create `README.md`; use reserved `index.md` for navigation.
- ❌ Do not create live Markdown from unfinished templates.
- ❌ Do not create Markdown under `tools/`; tooling is outside the OKF knowledge graph.
- ❌ Do not reuse a retired tag number.
- ❌ Do not start a topic without objective and scope recorded in `topic.md`.
- ❌ Do not hand-move a roadmap item into `Roadmap/completed/`; use `/complete-roadmap`.
- ❌ Do not set a roadmap item's status to `done` by hand; the hook rejects it.
- ❌ Do not complete a roadmap item without recording what shipped in a `Docu/DC-NN-*/doc.md`.
- ❌ Do not write loose files into `Docu/`; every document belongs to a `DC-NN` subject.
- ❌ Do not bypass a hook block by renaming a path to dodge the check — fix the structure instead.
- ❌ Do not finish work while either conformance layer reports a violation.
