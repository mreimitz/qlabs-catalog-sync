# Architectural Decision Records (ADRs)

This directory contains Architectural Decision Records (ADRs) documenting important technical decisions made during development.

## What is an ADR?

An ADR is a written record of a significant architectural decision and the reasoning behind it. It captures:

- The context and problem that required the decision
- The options that were considered
- The decision made and why
- The consequences of that decision

ADRs help the team understand why things are built the way they are, and make it easier for new contributors to grasp the design rationale.

## Numbering and filename convention

ADRs are numbered sequentially in this directory:

- File names: `NNNN-short-title.md` (four-digit zero-padded number, kebab-case title)
- Examples: `0001-async-throughout.md`, `0002-sdk-only-dependencies.md`
- Numbers are never reused, even after a decision is superseded or deprecated

Reference an ADR by its number in code comments and documentation; for example: "See ADR-0001 for why we went async throughout."

## When to write an ADR

Write an ADR when you make a significant technical decision affecting:

- Package architecture or boundary rules
- Async/concurrency patterns
- Error handling or logging strategy
- Testing approach or test tooling
- Configuration mechanisms
- Dependency choices that lock in a direction

Do not write an ADR for:

- Routine implementation choices (e.g. variable naming, loop structure)
- Changes to code that do not affect architecture or design patterns
- Bug fixes or routine refactoring

## ADRs that are already decided

The MVP scope and the eight binding mapping decisions for the first release are documented in the governed `planning/` OKF bundle, not here. Read them at:

- **v1 scope guardrails:** `planning/Roadmap/completed/RM-01-one-way-sync-mvp/decision.md`
- **MVP mapping decisions (D1–D8):** `planning/Roadmap/completed/RM-01-one-way-sync-mvp/decision-databricks-to-qlik-mvp.md`

These decisions are part of the roadmap governance and may only be changed through the roadmap process, not through this ADR directory.

## How to write a new ADR

1. Look at the next available number (check the highest-numbered file in this directory and increment).
2. Copy `0000-template.md` to `NNNN-short-title.md` with your chosen title and number.
3. Fill in all sections: Context, Options, Decision, Consequences, and Alternatives considered.
4. Be concise but thorough. Assume the reader has some context but is not deeply familiar with that part of the system.
5. Link to related ADRs using their filename (e.g. "See ADR-0001 for related reasoning").
6. Submit the ADR as part of your pull request. If the decision is contentious, discuss it in the PR before merging.

## Superseding or deprecating an ADR

If a later decision reverses or significantly changes an earlier ADR:

1. Add a note at the top of the old ADR saying it is superseded (e.g. "**SUPERSEDED by ADR-0005.**")
2. In the new ADR, reference the old one: "This supersedes ADR-0002 because..."
3. Do not delete old ADRs; they remain as a historical record.
