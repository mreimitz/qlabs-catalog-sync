# Roadmap/

Plans, sequencing, and intent. `ROADMAP.md` is the single master plan. Only `ROADMAP.md` and this
`README.md` may sit directly here — everything else goes in an `RM-NN-<slug>/` folder. Enforced by
`.claude/hooks/enforce-structure.sh`.

## Naming

`RM-NN-<slug>` — e.g. `RM-01-launch-plan`, `RM-02-vendor-selection`.

- `RM` = Roadmap, `NN` = zero-padded number, never reused.
- `RM-00-template` is the scaffold. Copy it; don't edit it.

## Add an item

Run `/new-roadmap`. It allocates the next `RM-NN`, copies the template, fills the README, and links
it in `ROADMAP.md` (and to any related `RS` research by tag).
