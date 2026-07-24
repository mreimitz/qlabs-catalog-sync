---
type: "Project"
title: "Research Project Scaffold"
description: "A reusable, strictly validated OKF scaffold for research and roadmap knowledge."
tags: ["project", "research", "okf"]
timestamp: "2026-07-24T00:00:00Z"
status: "active"
---

# Research Project Scaffold

This repository is a reusable knowledge-project scaffold for Claude Code and Claude Cowork.
The repository root is an Open Knowledge Format bundle, and every Markdown document below it is
either an OKF concept or a reserved `index.md` or `log.md` file.

## Knowledge domains

- [Research](/Research/) contains tagged investigations and their sources, notes, and outputs.
- [Roadmap](/Roadmap/) contains the master plan and tagged initiatives.
- [Claude controls](/.claude/) contains the commands, skills, hooks, templates, and profile that
  keep the knowledge tree valid.

## Daily workflows

- Use `/new-research` to scope and create an `RS-NN` research topic.
- Use `/new-roadmap` to create an `RM-NN` roadmap item.
- Use `/research-status` to inspect current work.
- Use `/validate-okf` to validate official OKF and the strict local profile.
- Use `/sync-okf` to regenerate managed indexes and the master roadmap view.

## Stable tags

Research folders use `RS-NN-short-slug`; roadmap folders use `RM-NN-short-slug`. Numbers are
zero-padded, allocated atomically, and never reused.
