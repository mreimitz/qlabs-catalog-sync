---
description: Start a new research topic via the structured intake interview, then scaffold its RS-NN folder.
argument-hint: [optional one-line topic description]
---

Use the `research-intake` skill to start a new research topic.

If the user provided a description here: "$ARGUMENTS" — treat it as the starting objective,
pre-fill what you can, and only ask the intake questions that remain genuinely open.
If nothing was provided, run the full intake from `.claude/skills/research-intake/questions.md`.

Then scaffold the new `Research/RS-NN-<slug>/` folder from the template, fill its README from the
answers, and register it in `Roadmap/ROADMAP.md` — exactly as the skill describes.
