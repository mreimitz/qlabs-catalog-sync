# CLAUDE.md — Research Project Operating Rules

This file governs how Claude Cowork (and any agent) behaves in this project.
**These are hard rules, not suggestions.** They are also enforced mechanically by
hooks in `.claude/hooks/` — but you must follow them proactively, not wait to be blocked.

---

## 1. Purpose

This is a research project workspace. Two top-level domains exist:

- **`Research/`** — where investigation happens. One subfolder per research topic.
- **`Roadmap/`** — where plans, sequencing, and intent are documented.

Everything created during work lands in one of these two trees. Nothing of substance
belongs loose in the project root.

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
- `RS-00-template` and `RM-00-template` are reserved scaffolds. **Never write real content into them.**
  Copy them to the next free number instead.

---

## 3. Research folder rules (HARD RULE — enforced by hook)

> **Every research document MUST live inside a `Research/RS-NN-*/` topic folder.**
> Writing a file directly into `Research/` (loose, outside a topic folder) is forbidden.

- To start a topic, copy `Research/RS-00-template/` to `Research/RS-NN-<slug>/` and fill in its `README.md`.
- All sources, notes, and outputs for that topic stay inside its folder:
  - `sources/` — raw captured material (PDFs, fetched pages, exports, transcripts).
  - `notes/` — your working notes, synthesis, intermediate thinking.
  - `outputs/` — finished deliverables (memos, tables, decks, briefs).
- The topic `README.md` is the front door: objective, scope, status, and an index of what's inside.
- Do not scatter a topic's files across multiple folders. One topic = one folder = one tag.

The `enforce-structure.sh` PreToolUse hook will **block** any write that violates this. If you
are blocked, do not try to work around it — create the proper `RS-NN` folder and retry.

---

## 4. Roadmap folder rules (HARD RULE — enforced by hook)

- `Roadmap/ROADMAP.md` is the **single master plan**. It holds the live list of all `RM` items
  and links to all active `RS` topics. Keep it current.
- Detailed planning for any item goes in its own `Roadmap/RM-NN-<slug>/` subfolder, never loose in `Roadmap/`.
- `ROADMAP.md` and `README.md` are the only files allowed directly in `Roadmap/`.
- Each roadmap item links to the research it depends on or produces (by `RS-NN` tag).

---

## 5. Workflow for a new research topic

When the user wants to start research, **run the intake** rather than guessing scope.

1. Trigger the intake skill (`/new-research`, or the `research-intake` skill). It asks a short,
   editable sequence of questions to pin down objective, scope, sources, deliverable, and success criteria.
2. Allocate the next free `RS-NN`.
3. Copy `Research/RS-00-template/` → `Research/RS-NN-<slug>/`.
4. Fill the topic `README.md` from the intake answers.
5. Add a one-line entry + link in `Roadmap/ROADMAP.md` (and an `RM-NN` folder if it needs its own plan).
6. Only then begin gathering sources and writing notes — inside the new folder.

---

## 6. Working style

- Capture sources before synthesizing; keep a provenance trail in `sources/` so claims are traceable.
- Distinguish raw capture (`sources/`), thinking (`notes/`), and deliverables (`outputs/`).
- When citing web material, paraphrase; never paste large verbatim passages.
- Cross-link by tag. The tag graph (which RS feeds which RM) is the project's memory.
- Keep `ROADMAP.md` and each topic `README.md` status field honest: `planned` / `active` / `blocked` / `done` / `archived`.

---

## 7. What NOT to do

- ❌ Do not write loose files into `Research/` or `Roadmap/` roots.
- ❌ Do not put real content in the `*-00-template` folders.
- ❌ Do not reuse a retired tag number.
- ❌ Do not start a topic without an objective and scope recorded in its `README.md`.
- ❌ Do not bypass a hook block by renaming a path to dodge the check — fix the structure instead.
