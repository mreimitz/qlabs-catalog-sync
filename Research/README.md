# Research/

One subfolder per research topic. **Nothing loose lives here** — every document belongs inside
a topic folder. This rule is enforced by `.claude/hooks/enforce-structure.sh`.

## Naming

`RS-NN-<slug>` — e.g. `RS-01-vendor-landscape`, `RS-02-pricing-elasticity`.

- `RS` = Research, `NN` = zero-padded number, never reused.
- `RS-00-template` is the scaffold. Copy it; don't edit it.

## Start a topic

Run `/new-research` (or trigger the `research-intake` skill). It interviews you, allocates the next
`RS-NN`, copies the template, fills the README, and registers the topic in `Roadmap/ROADMAP.md`.

## Inside each topic

- `sources/` — raw captured material, with provenance.
- `notes/` — working notes and synthesis.
- `outputs/` — finished deliverables.
- `README.md` — the front door: objective, scope, status, index.
