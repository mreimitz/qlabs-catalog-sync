---
type: "Standard Profile"
title: "Research Scaffold OKF Profile"
description: "The strict metadata, lifecycle, evidence, and validation rules for this scaffold."
tags: ["okf", "standard", "validation"]
timestamp: "2026-08-20T10:45:00Z"
status: "active"
---

# Research Scaffold OKF Profile

This project targets [Open Knowledge Format v0.1 Draft](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md).
The machine-readable profile is stored in [`.claude/okf-profile.json`](.claude/okf-profile.json).
The inspected specification revision is `ee67a5ca27044ebe7c38385f5b6cffc2305a9c1a`; the upstream
repository head observed on 2026-07-24 was `d44368c15e38e7c92481c5992e4f9b5b421a801d`.

## Conformance layers

The validator reports two layers independently:

1. **Official OKF v0.1** checks concept frontmatter, non-empty `type`, and reserved file structure.
2. **Research Scaffold Profile** adds mandatory metadata, controlled types and statuses, managed
   directory structures, complete indexes, source companions, citations, links, and stable tags.

## Required concept fields

Every ordinary concept requires `type`, `title`, `description`, `tags`, `timestamp`, and `status`.
Frontmatter uses the profile's JSON-compatible YAML subset so validation remains deterministic and
offline.

Markdown under `tools/` is forbidden. That directory contains non-OKF scaffold implementation;
ignoring Markdown there would violate the root bundle's official whole-tree conformance boundary.

## Reserved files

- `index.md` provides progressive disclosure and contains no frontmatter, except the bundle-root
  index, which declares `okf_version: "0.1"`.
- `log.md` records changes under newest-first ISO 8601 date headings and contains no frontmatter.

## Delivery lifecycle

Roadmap items and their documentation are validated as a pair. A `Roadmap Item` with status
`done` must live under `Roadmap/completed/`, and every item there must be `done`. Each completed
item must be recorded as a `### RM-NN` increment inside a `Documentation` concept under
`Docu/DC-NN-slug/`, and that increment must link back to the item it documents. `Documentation`
concepts carry a `## Delivered increments` section, and may only leave `draft` once it holds at
least one increment.

## Evidence

Non-Markdown research artifacts require a same-stem `Source Reference` concept. Research notes,
research outputs, and decisions always include a `# Citations` section.
