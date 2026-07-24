---
name: research-intake
description: Runs a structured intake interview to start a new research topic, then scaffolds the correct RS-NN folder. Use this whenever the user wants to begin, kick off, scope, or set up a new piece of research, investigation, or analysis — even if they just describe a question they want answered rather than explicitly saying "new research". Always use this instead of dumping files loosely; it enforces this project's Research/RS-NN structure and tagging.
---

# Research Intake

Turns a vague "I want to look into X" into a properly scoped, properly filed research topic.
It asks a short sequence of questions, then creates the topic folder and seeds its README.

## When to run

- The user wants to start/scope a new research topic or investigation.
- The user describes a question or decision they need answered with research.
- You're about to create research files and no `RS-NN` topic folder exists yet for them.

If a topic folder already exists and the user just wants to add to it, skip the intake and work inside that folder.

## The intake sequence

The questions live in `questions.md` next to this file. **Read `questions.md` and ask those
questions** — it is the single, editable source of truth, so the user can customize the intake
without touching this skill. Ask them conversationally, a few at a time, not as a wall of text.

Guidance while interviewing:
- Pull answers from the existing conversation first; only ask what's genuinely unknown.
- If the user is vague on scope, push gently for what's explicitly *out* of scope — that's the
  most valuable answer and the easiest to skip.
- Keep it short. The goal is a usable scope, not a contract. 3–8 questions is the target.
- If the user says "just go", infer sensible defaults, state them, and proceed.

## After the interview — scaffold the topic

1. **Pick the next free tag.** List `Research/` and find the highest existing `RS-NN`; the new one
   is the next integer, zero-padded. Never reuse a retired number. (`RS-00` is the template — skip it.)
2. **Choose a slug.** 2–4 lowercase words, hyphenated, derived from the objective
   (e.g. `RS-03-competitor-pricing-tiers`).
3. **Copy the template:** `cp -r Research/RS-00-template Research/RS-NN-<slug>`.
4. **Fill the README.** Open `Research/RS-NN-<slug>/README.md` and replace every `{{...}}`
   placeholder with the intake answers. Set status to `active`.
5. **Register it in the roadmap.** Add a one-line entry under the research index in
   `Roadmap/ROADMAP.md` linking to the new folder by tag. If the topic needs its own multi-step
   plan, also copy `Roadmap/RM-00-template` to a new `RM-NN-<slug>` and link the two by tag.
6. **Confirm** to the user: show the tag, the path, and the one-line objective. Then start work
   *inside* the new folder (`sources/`, `notes/`, `outputs/`).

## Hard rules (also enforced by the PreToolUse hook)

- Research docs go **only** inside `Research/RS-NN-*/`. Never loose in `Research/`.
- Never write into `RS-00-template` / `RM-00-template`.
- One topic = one folder = one tag. Don't scatter a topic's files.

If a write gets blocked by the hook, you violated one of the above — create the proper folder and retry; don't work around it.
