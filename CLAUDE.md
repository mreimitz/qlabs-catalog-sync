---
type: "Agent Instruction"
title: "Research Project Operating Rules"
description: "Mandatory structure, OKF conformance, research, and roadmap rules for agents."
tags: ["agent", "instruction", "okf"]
timestamp: "2026-07-24T12:20:07Z"
status: "active"
---

# Research Project Operating Rules

This concept governs how Claude Cowork, Claude Code, and other agents behave in this project.
These are hard rules. Hooks, generators, pre-commit, and CI enforce them, but agents must follow
them proactively.

---

## 1. Purpose

This repository is one Open Knowledge Format v0.1 bundle. Three knowledge domains exist:

- **`Research/`** — where investigation happens. One subfolder per research topic.
- **`Roadmap/`** — where plans, sequencing, and intent are documented.
- **`.claude/`** — agent controls, generation templates, and conformance tooling.

Every `.md` file is either an OKF concept with strict frontmatter or a reserved `index.md` /
`log.md`. Use `python3 .claude/scripts/okf.py validate` after material changes.

`tools/` is scaffold infrastructure, not an OKF knowledge domain. It contains code, configuration,
and tests only. Markdown is forbidden under `tools/`, and the root knowledge index must not list it.

---

## 2. Tagging convention (MANDATORY)

Every research topic and every roadmap item gets a stable, zero-padded tag.

| Domain   | Tag prefix | Example  | Meaning          |
| -------- | ---------- | -------- | ---------------- |
| Research | `RS`       | `RS-01`  | Research item 1  |
| Roadmap  | `RM`       | `RM-01`  | Roadmap item 1   |

Rules:
- Folder names are `RS-NN-short-slug` / `RM-NN-short-slug` (e.g. `RS-04-token-pricing-models`).
- `NN` is two digits, zero-padded, **never reused** even after a topic is archived.
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

---

## 5. Workflow for a new research topic

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

## 6. Working style

- Capture sources before synthesizing; keep a provenance trail in `sources/` so claims are traceable.
- Distinguish raw capture (`sources/`), thinking (`notes/`), and deliverables (`outputs/`).
- When citing web material, paraphrase; never paste large verbatim passages.
- Cross-link by tag. The tag graph (which RS feeds which RM) is the project's memory.
- Keep `roadmap.md`, `topic.md`, and `item.md` statuses honest.
- Update a concept's UTC `timestamp` whenever its meaning changes.
- Prefer bundle-root links; for example, an `RS-03` topic links to its repository-root concept path.

---

## 7. What NOT to do

- ❌ Do not write loose files into `Research/` or `Roadmap/` roots.
- ❌ Do not create `README.md`; use reserved `index.md` for navigation.
- ❌ Do not create live Markdown from unfinished templates.
- ❌ Do not create Markdown under `tools/`; tooling is outside the OKF knowledge graph.
- ❌ Do not reuse a retired tag number.
- ❌ Do not start a topic without objective and scope recorded in `topic.md`.
- ❌ Do not bypass a hook block by renaming a path to dodge the check — fix the structure instead.
- ❌ Do not finish work while either conformance layer reports a violation.
