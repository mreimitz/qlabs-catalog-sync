# Port prompt — bring the governed research / roadmap / delivery-docs concept into `elabs-ai-workbench`

Paste everything below the line into a Claude Code session opened on
`/Users/czq/Documents/DEV/elabs/elabs-ai-workbench`. Run it on a branch, not on `main`.

The three `*.patch` files next to this prompt are verified: they apply cleanly with `git apply`,
and after applying them the bundle validates on both layers with all 34 of its own tests green
(checked 2026-08-20 on system `python3` 3.9.6).

---

# Task: install the OKF planning bundle and migrate this repo's research, roadmap and guide into it

You are working in `/Users/czq/Documents/DEV/elabs/elabs-ai-workbench`. Do the whole job in one
run: install the tooling, migrate every existing document into it, wire the enforcement, and leave
the repo validating clean. Work on a branch. Commit in logical chunks as you go.

## Why

This repo already has the three knowledge domains — `research/`, `roadmap/`, `user-guide/` — but
nothing keeps them honest. Folders have no stable identity, a plan can be "finished" without
anyone recording what actually shipped, `ROADMAP.md` openly admits it is out of date, and
`README.md`/`CHANGELOG.md` drift from the software.

The source repo `/Users/czq/Documents/DEV/qlabs/qlabs-catalog-sync` solves this with a strict
Open Knowledge Format bundle in `planning/`. Its shape:

- **Three tagged domains.** `Research/RS-NN-slug/`, `Roadmap/RM-NN-slug/`,
  documentation subjects under a third root. Two-digit, zero-padded, allocated atomically by a
  generator, **never reused** — the tag is the primary key and the cross-links between tags are
  the project's memory.
- **Every Markdown file is a typed concept** with required frontmatter (`type`, `title`,
  `description`, `tags`, `timestamp`, `status`), or a reserved `index.md` / `log.md`.
- **One legal lifecycle.** Work is on the roadmap → it gets built → the delivery is recorded in a
  documentation subject as "what shipped versus what was planned" → the roadmap item is retired
  into `Roadmap/completed/` by a transactional command. An item is not done until all four
  happened.
- **Enforcement is mechanical.** A pre-write hook rejects a nonconformant file at the moment of
  the edit; a post-write validator catches cross-file violations; a dependency-free CLI
  (`okf.py`) is the only sanctioned writer for anything structural.

You are porting that, with four deliberate differences, all decided already — do not re-litigate
them:

1. The documentation domain root is named **`user-guide`**, not `Docu`, and a subject holds both
   the delivery record (`doc.md`) and that part of the system's user-facing guide pages.
2. Completion gates on **`STATUS.md` work-package ledgers** — the format `/next-wp` already
   maintains — instead of the source repo's `tasks.json` boards.
3. The bundle lives at **`planning/`** in this repo, and the existing `research/`, `roadmap/`,
   `user-guide/` trees move into it.
4. The hooks are wired at the **repo root** so they apply in normal sessions (the source repo's
   root hooks are `exit 0` stubs; it leans on pre-commit instead, and this repo has no
   pre-commit).

## Ground rules

- **Never write to `/Users/czq/Documents/DEV/qlabs/qlabs-catalog-sync`.** You read files from it.
  That is all.
- **Once the bundle exists, never hand-edit a concept's structure.** Create items with the
  generators (`new-research`, `new-roadmap`, `new-docu`), retire them with `complete-roadmap`,
  regenerate navigation with `sync-indexes`. Never `mkdir` a tagged folder, never edit
  `.claude/tag-registry.json`, never move an item into `Roadmap/completed/` yourself, never set a
  roadmap item's status to `done` by hand — the hook rejects all of that, and renaming a path to
  dodge a check is itself a violation.
- **`index.md` is the navigation file; `README.md` is banned bundle-wide** (`PROFILE025`). Every
  `README.md` you migrate must be *converted*, not moved.
- **Honest reporting.** Nothing is "done", "green" or "passing" unless you ran the command and
  saw the output. Paste real output in your report.
- **Do not invent tags, statuses or document types.** Use the vocabulary in
  `planning/.claude/okf-profile.json`.
- **Flag, do not fix, anything outside this port.** Collect those in your final report.

## What is in this repo right now (observed 2026-08-20 — verify before trusting it)

- `research/` — 8 topic folders (`agentic-session-sota`, `full-validation`, `langfuse-landscape`,
  `langsmith-observability`, `skill-registry`, `token-context-comparison`, `unified-run-sessions`)
  plus a loose `EXPLORATION_FINDINGS.md`. Topics hold numbered Markdown files and a `README.md`.
- `roadmap/` — 13 loose numbered planning docs (`00-product-brief.md` … `12-testing-inspector-devtools.md`),
  31 plan folders with a `STATUS.md` checkbox ledger, plus `findings/` (17 files, no ledger),
  `research/` (one file), `release/` (README only). Plan folders hold `README.md`, `STATUS.md`,
  `conventions.md`, `wp-N.M-*.md` specs and `phase-N/` subfolders.
- `user-guide/` — 25 numbered manual pages, `README.md`, `product-page.md`,
  `AI-Workbench-Overview.pdf`, `ai-workbench-landing.html`, `assets/`, `images/`.
- Ledgers that are **fully ticked** (every box `[x]`): `assistant-hub-ux`, `interface-craft`,
  `server-types`, `toolbar-reach`, `unified-sessions`. These are the retirement candidates.
- Root: `README.md`, `CHANGELOG.md`, `ROADMAP.md` (self-declared historical), `CLAUDE.md`,
  `.claude/{rules,commands,skills,hooks,settings.json}`, one GitHub workflow.

Start by re-running the inventory. If it differs from the above, follow what you find and say so
in the report.

---

## Phase 1 — install the bundle

**1.1 Copy the tooling** from the source bundle at
`/Users/czq/Documents/DEV/qlabs/qlabs-catalog-sync/planning` into `planning/` here:

```
.claude/scripts/okf.py            the validator + generators (dependency-free, ~2300 lines)
.claude/hooks/enforce-structure.sh   pre-write guard
.claude/hooks/validate-okf.sh        post-write validator
.claude/templates/*.tmpl          research topic/note/output, roadmap item, documentation,
                                  decision, source reference
.claude/commands/*.md             new-research, new-roadmap, new-docu, complete-roadmap,
                                  doc-intake, research-status, sync-okf, validate-okf
.claude/skills/research-intake/   SKILL.md + questions.md
.claude/okf-profile.json          the machine-readable profile
.claude/settings.json             the bundle's own session hooks
.claude/index.md                  navigation for the controls
.claude/tests/test_okf.py         the bundle's own test suite (patch 02 targets it)
tools/{bootstrap.py,doc_intake.py,markitdown_adapter.py,requirements.txt,pyproject.toml,tooling.json,tests/}
```

Do **not** copy `tools/agent-plan/` — that is the `tasks.json` board machinery this port replaces
with ledgers. Do **not** copy the source repo's content (`Research/`, `Roadmap/`, `Docu/`,
`project.md`, `CLAUDE.md`, `log.md`, `index.md`); those you author fresh in 1.4.

**1.2 Apply the three patches** from
`/Users/czq/Documents/DEV/qlabs/qlabs-catalog-sync/docs/okf-port/`, from this repo's root:

```bash
git apply -v \
  /Users/czq/Documents/DEV/qlabs/qlabs-catalog-sync/docs/okf-port/01-okf-py.patch \
  /Users/czq/Documents/DEV/qlabs/qlabs-catalog-sync/docs/okf-port/02-test-okf-py.patch \
  /Users/czq/Documents/DEV/qlabs/qlabs-catalog-sync/docs/okf-port/03-okf-profile-json.patch
```

What each does:

- **`01-okf-py.patch`** — two changes. It introduces `DOCU_ROOT = "user-guide"` and routes every
  documentation-root reference through it (the `DC` domain definition, directory titles, the two
  `DC-*/doc.md` globs, `new-docu`'s target, the path policy). And it replaces the task-board gate
  with a ledger gate: `discover_status_ledgers()` finds `Roadmap/RM-*/STATUS.md`, `ledger_gate()`
  refuses the completion while any `- [ ]` box is open in the item's own ledger, and the CLI flags
  become `--ledger` / `--no-ledger`.
- **`02-test-okf-py.patch`** — the bundle's own test suite, updated for both.
- **`03-okf-profile-json.patch`** — points `required_static_paths` at `user-guide/index.md` and
  adds three types so this repo's existing artifacts can validate in place:
  `Guide Page` (a user-facing manual page inside a subject), `Work Package Spec` (a `wp-N.M-*.md`),
  `Status Ledger` (a `STATUS.md`).

**1.3 Reset the tag registry.** Write `planning/.claude/tag-registry.json` as:

```json
{
  "allocated_dc": [],
  "allocated_rm": [],
  "allocated_rs": [],
  "next_dc": 1,
  "next_rm": 1,
  "next_rs": 1
}
```

**1.4 Create the domain roots and author the five root concepts.** Create the empty directories
`planning/Research/`, `planning/Roadmap/`, `planning/Roadmap/completed/`, `planning/user-guide/`.
Then write, using the source repo's equivalents as models but with this repo's own subject matter:

- `planning/project.md` — type `Project`. What the workbench is. Seed it from
  `roadmap/00-product-brief.md` and the capability table in `CLAUDE.md`.
- `planning/CLAUDE.md` — type `Agent Instruction`. The bundle's operating rules: the tagging
  convention, the research/roadmap/user-guide folder rules, the implementation lifecycle, the
  working style, the "what NOT to do" list. Adapt the source's version — replace every `Docu`
  with `user-guide`, every `tasks.json` gate with the `STATUS.md` ledger gate, and every
  `tools/agent-plan` reference with `/next-wp`.
- `planning/okf-profile.md` — type `Standard Profile`. The prose companion to the JSON profile;
  document the three added types and the ledger gate.
- `planning/index.md` — reserved, declares `okf_version: "0.1"` (generated by `sync-indexes`, but
  it must exist).
- `planning/log.md` — reserved, newest-first `## YYYY-MM-DD` headings.
- `planning/Roadmap/roadmap.md` — type `Roadmap`, title "Master Roadmap", status `active`, with
  any placeholder body. **This one must be seeded by hand.** `sync-indexes` regenerates its body
  (the live list of every item, topic and subject) but silently skips the file when it does not
  exist, and `validate` then fails with `PROFILE024 Roadmap/roadmap.md: required scaffold file is
  missing`. Seed it before the first sync.

Then run `python3 planning/.claude/scripts/okf.py --root planning sync-indexes` and
`… validate`. Both layers must PASS on the empty bundle before you migrate anything, a second
`sync-indexes` must report `Synchronized 0 file(s).`, and a throwaway `new-research` +
`new-docu` must both come out conformant. (This exact sequence was rehearsed against a fresh
copy of the tooling — if it does not pass for you, something in the copy or the patches is
incomplete; fix that before migrating.)

One thing that will *not* pass yet: `planning/.claude/tests/test_okf.py` throws about nine errors
at this point, because its integration fixtures name the source repo's own roadmap items. That is
expected and Phase 5 fixes it. `planning/tools/tests` must pass immediately (7 tests).

---

## Phase 2 — migrate every document

**The rule for every item, no exceptions:** run the generator to create the tagged folder, then
move the legacy files into it, then convert them, then `sync-indexes`. The generator writes
`topic.md` / `item.md` / `doc.md`, the folder's `log.md`, and the required subdirectories. You
never create a tagged folder by hand.

Tags are allocated in the order you create items, so create them in the order you want the numbers.
Every generator takes `--slug`; pass the existing folder name (`--slug security-posture` →
`RM-NN-security-posture`) so people still recognize their plan after the move. Print the full
assignment table in your report.

### 2.1 Research → `planning/Research/RS-NN-slug/`

One RS topic per existing folder in `research/`, plus one for `EXPLORATION_FINDINGS.md` (or fold
it into the topic it belongs to, if it clearly belongs to one — say which you chose).

```bash
python3 planning/.claude/scripts/okf.py --root planning new-research \
  --title "<title>" --objective "<what the investigation answers>" \
  --why-now "<the decision or downstream work it feeds>" \
  --scope-in "<included>" --scope-out "<excluded>" \
  --deliverable "<what it produces and for whom>" \
  --success-criteria "<how you know it is finished>"
```

Then, inside the created folder:

- the topic's `README.md` → fold its content into `topic.md` under the template's headings, and
  delete it (`README.md` is banned);
- analysis / working files → `notes/`, typed `Research Note`;
- finished deliverables (a comparison table, a recommendation, a spec-shaped output) → `outputs/`,
  typed `Research Output`;
- raw captured material → `sources/`, and every non-Markdown artifact there needs a same-stem
  `Source Reference` companion that links to it (`PROFILE019`/`029`/`030`);
- **every `Research Note`, `Research Output` and `Decision` needs a `# Citations` section** —
  write `None.` when there is nothing to cite.

### 2.2 Roadmap plans → `planning/Roadmap/RM-NN-slug/`

One RM item per plan folder in `roadmap/` (31 of them), plus `release/`, plus `findings/` if you
judge it a plan rather than research (say which you chose).

```bash
python3 planning/.claude/scripts/okf.py --root planning new-roadmap \
  --title "<title>" --goal "<goal>" --why-it-matters "<reason>" \
  --milestone "<milestone>" --research "RS-NN"
```

Repeat `--milestone` per phase or work package, and `--research` for every topic the plan depends
on — those cross-links are the point of the tag graph, so spend the effort to get them right.
Then, inside the created folder:

- the plan's `README.md` → fold into `item.md` (goal / why it matters / milestones / linked
  research), then delete it;
- `STATUS.md` → **keep the name and the checkbox format** (the gate and `/next-wp` both read it);
  add frontmatter with `type: "Status Ledger"`, `status: "active"`;
- `wp-N.M-*.md` → add frontmatter with `type: "Work Package Spec"` (`draft`/`review`/`final`/
  `superseded`/`archived`);
- `conventions.md`, `kickoff-prompt.md`, amendments → `Work Package Spec`, or `Decision` where the
  document really is a decision record (a `Decision` needs `## Context`, `## Decision`,
  `## Consequences` and `# Citations`);
- `phase-N/` and `_superseded/` subfolders stay where they are, contents converted the same way;
- set the item's `status` honestly: `active` if its ledger has ticked and open boxes, `planned` if
  nothing is ticked, `blocked` if it is waiting on someone. Never `done` — that is
  `complete-roadmap`'s job (Phase 3).

The 13 loose `roadmap/NN-*.md` docs are not plans and cannot stay loose (`Roadmap/` accepts only
`index.md` and `roadmap.md`). Route each by what it actually is:

| Document | Where it goes |
| --- | --- |
| `00-product-brief.md` | seeds `planning/project.md`; archive the original as a `Research Output` if it holds detail the project concept drops |
| `01-architecture.md`, `03-data-model.md` | a `user-guide/DC-NN` subject — they describe what exists |
| `04-token-counting-strategy.md`, `07-open-questions.md`, `10-testing-ui-concept.md` | `RS` topics — they are investigations |
| `02-implementation-plan.md`, `05-ui-plan.md`, `06-docker-plan.md`, `08-expanded-target.md`, `09-testing.md`, `11-testing-implementation-plan.md`, `12-testing-inspector-devtools.md` | fold into the RM item that owns the work; if the work already shipped, into the DC subject that records it |

Where a doc is wholly superseded, say so in the report rather than silently dropping it — nothing
gets deleted without being named.

### 2.3 Guide → `planning/user-guide/DC-NN-slug/`

Subjects are **parts of the system**, not roadmap items: the scanner, the tool playground, the
Testing console, the Skills registry, the assistant, observability, the CLI, the MCP server,
packaging/operations, and so on. One roadmap item usually writes into several subjects; a subject
accumulates increments across many items.

```bash
python3 planning/.claude/scripts/okf.py --root planning new-docu \
  --title "<title>" --subject "<one line: what this part of the system is>" \
  --scope-in "<what this subject documents>" --scope-out "<what it does not>" \
  --code-location "apps/api/src/<area>/"
```

Then, inside the created folder:

- the manual pages that belong to this part → moved in, frontmatter `type: "Guide Page"`, status
  `current` (or `superseded` for pages describing what no longer exists);
- keep the numbered filenames; they are how readers navigate;
- binaries and assets (`AI-Workbench-Overview.pdf`, `ai-workbench-landing.html`, `images/`,
  `assets/`) live inside the subject folder that owns them. They need **no** `Source Reference`
  companion — that rule applies only to `Research/RS-*/sources/`.
- `doc.md` stays the delivery record: subject, scope in/out, where the code lives, and
  `## Delivered increments` (empty until Phase 3 fills it). A subject stays `draft` until it
  records at least one increment — `review`/`current` with an empty increments section is a
  violation (`PROFILE037`/`PROFILE039`).
- `user-guide/README.md` and `product-page.md`: convert the README into the domain's `index.md`
  content (regenerated by `sync-indexes`, so fold anything worth keeping into a subject);
  `product-page.md` is marketing — make it a `Guide Page` in the subject that fits, or leave it
  outside the bundle and say so.

Run `sync-indexes` and `validate` after each batch of a few items, not once at the end. Fix
violations as they appear; they compound.

---

## Phase 3 — retire the plans that are actually finished

Five ledgers have no open boxes: `assistant-hub-ux`, `interface-craft`, `server-types`,
`toolbar-reach`, `unified-sessions`. Verify that is still true after migration, then for each:

1. Make sure every subject its delivery touched exists (`new-docu` first if not).
2. Retire it in one transaction:

```bash
python3 planning/.claude/scripts/okf.py --root planning complete-roadmap \
  --tag RM-NN --docu DC-NN --docu DC-MM \
  --shipped "<one line: what the software now does, from the outside>" \
  --deviation "<how the delivery differed from the plan>" \
  --gap "<what was deliberately left out>" \
  --code-path "apps/web/src/<area>/" \
  --docu-status current
```

The command moves the folder to `Roadmap/completed/`, sets the status to `done`, ticks the
milestones, writes a `### RM-NN` increment into each named subject, re-points bundle links,
regenerates the indexes and validates — rolling everything back on any failure. It **refuses**
while any box in the item's ledger is open; that refusal is correct, so report it rather than
reaching for `--no-ledger` (which exists only for an item that never had a ledger).

Write `--shipped`, `--deviation` and `--gap` from the ledger's own evidence — those ledgers record
what was verified, what deviated, and what was left unverified. Do not paraphrase the WP titles.

Afterwards the command prints every reference outside the bundle still pointing at the old path.
Apply those edits yourself, then confirm:

```bash
python3 planning/.claude/scripts/okf.py --root planning check-references --tag RM-NN
```

Everything else imports as `active`, `planned` or `blocked` and gets retired later through the
same command.

---

## Phase 4 — wire the enforcement

**4.1 Root hooks.** Add to `.claude/settings.json`, alongside the six existing `.mjs` hooks
(append, do not replace):

```json
{
  "PreToolUse": [
    {
      "matcher": "Write|Edit|MultiEdit|NotebookEdit",
      "hooks": [{ "type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/okf-pre.sh" }]
    }
  ],
  "PostToolUse": [
    {
      "matcher": "Write|Edit|MultiEdit|NotebookEdit",
      "hooks": [{ "type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/okf-post.sh" }]
    }
  ]
}
```

Write the two wrappers so they target the bundle root:

```bash
#!/usr/bin/env bash
# .claude/hooks/okf-pre.sh — reject a nonconformant write inside planning/
exec python3 "${CLAUDE_PROJECT_DIR:-$(pwd)}/planning/.claude/scripts/okf.py" \
  --root "${CLAUDE_PROJECT_DIR:-$(pwd)}/planning" hook-pre
```

```bash
#!/usr/bin/env bash
# .claude/hooks/okf-post.sh — validate the bundle after a write
python3 "${CLAUDE_PROJECT_DIR:-$(pwd)}/planning/.claude/scripts/okf.py" \
  --root "${CLAUDE_PROJECT_DIR:-$(pwd)}/planning" validate || exit 2
```

A write outside `planning/` exits 0 — the pre-hook only judges paths under its root. Verify both
directions yourself (a `README.md` under `planning/` must be blocked; an edit under
`apps/web/src/` must pass) and paste the output. `chmod +x` both.

**4.2 `package.json` scripts:**

```json
"okf": "python3 planning/.claude/scripts/okf.py --root planning",
"okf:validate": "python3 planning/.claude/scripts/okf.py --root planning validate",
"okf:sync": "python3 planning/.claude/scripts/okf.py --root planning sync-indexes",
"okf:test": "python3 -m unittest discover -s planning/.claude/tests -p 'test_*.py'"
```

**4.3 CI.** Add a job (or a step in the existing workflow) running `pnpm okf:validate` and
`pnpm okf:test`. The runner already has `python3`; the bundle needs no install.

**4.4 Root `CLAUDE.md`.** Add a section — **Knowledge, planning and the delivery record** — that
states the lifecycle as a hard rule: work that is not an `RM-NN` item does not get built; it is
built against its `STATUS.md` ledger; its delivery is recorded in a `user-guide/DC-NN` subject;
it is retired with `complete-roadmap`. Add the tagging convention, the "never hand-edit the
bundle" rule, and the four commands. Then update:

- section 2 (repository layout) — add the `planning/` tree;
- section 9 (conventions) — where a new document goes, by kind;
- section 10 (map of `.claude/`) — the new hooks and the bundle's commands.

**4.5 `/next-wp`.** Update `.claude/skills/next-wp/SKILL.md` and `.claude/commands/next-wp.md`:

- ledgers now live at `planning/Roadmap/RM-NN-<slug>/STATUS.md`; the plan argument resolves to a
  tag or slug there;
- add a close-out step: when the last box in a ledger ticks, the plan is not finished — create or
  update its `user-guide/DC-NN` subjects, run `complete-roadmap`, and apply the stale-reference
  report;
- add the docs step below.

**4.6 The docs rule** (this is the "auto-update README" part — be precise about what it is).
In the source repo it is **a written rule plus an orchestrator step, not a script**: when a work
package closes, the front page must be brought in line in the same change, and every claim is
verified by running it rather than by reading the task title. Port it the same way, into both
`CLAUDE.md` and the `/next-wp` skill:

> When a work package's last box ticks, in the same commit: update the capability table in
> `README.md`, move what the WP made real out of "planned" and into what the app does today,
> correct anything the WP made false, and add a `CHANGELOG.md` entry. Verify each claim against
> the running app or a passing test — never from the WP description. A ledger box does not tick
> while the front page still describes software that does not match.

**4.7 Root `ROADMAP.md`.** It already declares itself historical. Replace its body with a pointer
to the generated `planning/Roadmap/roadmap.md` (the live index of every item, topic and subject),
keeping the vision section if it still reads true.

---

## Phase 5 — fix the ported test fixtures

`planning/.claude/tests/test_okf.py` came from the source repo and its integration tests are
fixtured on *that* repo's items. After migration, re-point them at yours:

- the completion tests use `RM-01-one-way-sync-mvp` — replace with the tag+slug of an item that is
  **still under `Roadmap/`**, not one you retired in Phase 3: the test copies the live bundle to a
  temp dir and completes that item there, so it must not already be in `Roadmap/completed/`. Its
  ledger content does not matter; the test writes its own ticked `STATUS.md` into the copy;
- one test asserts a *second* item's `item.md` links the first, proving links get re-pointed on
  the move — replace with a real pair from your graph, or add the link if none exists;
- `test_hook_blocks_in_place_done_flip` reads a live item path — point it at any active item;
- the waiver test needs a tag whose item exists but has **no `STATUS.md`** (`RM-02` in the
  source) — after migration that is any plan that never had a ledger, e.g. the one you made from
  `roadmap/release/`.

Then the suite must be green:

```bash
python3 -m unittest discover -s planning/.claude/tests -p "test_*.py"
```

---

## Verification — run all of these and paste the real output

```bash
python3 planning/.claude/scripts/okf.py --root planning validate       # both layers PASS
python3 planning/.claude/scripts/okf.py --root planning status         # the full RS/RM/DC table
python3 planning/.claude/scripts/okf.py --root planning sync-indexes   # second run: 0 file(s)
python3 -m unittest discover -s planning/.claude/tests -p "test_*.py"  # all tests OK
python3 -m unittest discover -s planning/tools/tests -p "test_*.py"    # doc-intake tests OK
python3 planning/.claude/scripts/okf.py --root planning check-references --tag RM-NN  # per retired tag
pnpm typecheck && pnpm test && pnpm build && pnpm lint                 # unchanged from baseline
git status                                                             # nothing moved by accident
```

Take the `pnpm` baseline **before** you start so you can prove the port changed nothing there.
Also verify by hand: the pre-hook blocks a `README.md` written under `planning/`, blocks a
by-hand `status: "done"` flip on an item, and allows an ordinary edit under `apps/`.

## Report

Structure the final report as:

**What changed** — what the project now does differently, from the outside. Plain language.

**Next step** — the single most useful thing to do next, concrete (a command, a decision, or
"nothing, this is done").

**Problems** — only real ones; "None." if there are none.

Below those three, include: the full tag assignment table (every RS / RM / DC with its source
path), which items you retired, every document you could not place and why, and the flag-only
list. One thing already known for that list: `CLAUDE.md` section 2 still claims
`mcp-token-footprint/` is the project root, which stopped being true when the app moved to the
repo root — flag it, do not fix it as part of this port.
