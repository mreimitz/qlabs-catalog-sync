---
description: Create a new roadmap item (RM-NN) with its own planning folder, and link it in ROADMAP.md.
argument-hint: [short name of the plan / initiative]
---

Create a new roadmap item.

1. Read `Roadmap/ROADMAP.md` and find the highest existing `RM-NN`; the new tag is the next integer, zero-padded.
2. Derive a 2–4 word hyphen slug from "$ARGUMENTS" (ask if it's empty).
3. Copy `Roadmap/RM-00-template` to `Roadmap/RM-NN-<slug>/` and fill its `README.md`:
   goal, why it matters, the steps/milestones, and which `RS-NN` research topics it depends on or produces.
4. Add a one-line entry for it under the roadmap index in `Roadmap/ROADMAP.md`, linking the folder and any related `RS` tags.
5. Confirm the tag, path, and one-line goal back to the user.
