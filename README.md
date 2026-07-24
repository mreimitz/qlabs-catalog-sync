# Research Project Template (Claude Cowork)

A reusable scaffold for research projects driven by Claude Cowork / Claude Code. It enforces a
clean structure — research in `Research/`, plans in `Roadmap/` — with stable tags and a guided
intake so every topic starts properly scoped instead of as a pile of loose files.

## Layout

```
.
├── CLAUDE.md                  # the operating rules (read first; agents obey this)
├── README.md                  # this file
├── .claude/
│   ├── settings.json          # registers the enforcement + session hooks
│   ├── hooks/
│   │   ├── enforce-structure.sh   # wrapper called by the hook
│   │   └── enforce-structure.py   # the guard logic (blocks rule-breaking writes)
│   ├── skills/
│   │   └── research-intake/
│   │       ├── SKILL.md        # the intake skill (scope → scaffold)
│   │       └── questions.md    # EDIT THIS to customize the interview
│   └── commands/
│       ├── new-research.md     # /new-research  → start a topic via intake
│       ├── new-roadmap.md      # /new-roadmap   → add a roadmap item
│       └── research-status.md  # /research-status → status board
├── Research/                  # RS-NN-<slug> topic folders (one per topic)
│   ├── README.md
│   └── RS-00-template/         # scaffold — copy, don't edit
│       ├── README.md
│       ├── sources/  notes/  outputs/
└── Roadmap/                   # plans
    ├── ROADMAP.md              # the single master plan
    ├── README.md
    └── RM-00-template/         # scaffold — copy, don't edit
        └── README.md
```

## Tags

- **Research:** `RS-NN` (e.g. `RS-01` = research item 1). Folder: `Research/RS-NN-<slug>/`.
- **Roadmap:** `RM-NN` (e.g. `RM-01` = roadmap item 1). Folder: `Roadmap/RM-NN-<slug>/`.
- Numbers are zero-padded and never reused. Cross-reference items by tag everywhere.

## Daily use

| You type | What happens |
| --- | --- |
| `/new-research [topic]` | Runs the intake interview, then scaffolds `Research/RS-NN-<slug>/` and links it in the roadmap. |
| `/new-roadmap [name]` | Creates `Roadmap/RM-NN-<slug>/` and links it in `ROADMAP.md`. |
| `/research-status` | Prints a status board of all RS + RM items. |

You don't have to use the commands — describing a research question in plain language will trigger
the `research-intake` skill on its own.

## How enforcement works

`.claude/hooks/enforce-structure.sh` runs before every file write (a `PreToolUse` hook). It blocks
writes that would (a) drop a loose file into `Research/` or `Roadmap/`, or (b) write into a reserved
`*-00-template` scaffold. If a write is blocked, the fix is always to create the proper `RS-NN` /
`RM-NN` folder and retry — never to dodge the check.

## Customize

- **Intake questions:** edit `.claude/skills/research-intake/questions.md` — single source of truth.
- **Change the Research tag** (e.g. `RS` → `RA`): update `CLAUDE.md`, the `RS-` regexes in
  `enforce-structure.sh`, rename `RS-00-template`, and the references in the skill/commands.
- **Add hooks:** extend `.claude/settings.json`.

## To start a real project

Copy this whole folder to a new location and start working. Delete the example rows in
`ROADMAP.md`. Leave the `*-00-template` folders in place.
