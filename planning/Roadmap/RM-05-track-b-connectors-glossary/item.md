---
type: "Roadmap Item"
title: "Track B source connectors and the Qlik glossary"
description: "Extend the shipped upstream sync with the Collibra and Snowflake read connectors and the Qlik glossary write path, on top of the SDK and engine that v0.1 proved."
tags: ["roadmap", "RM-05"]
timestamp: "2026-08-20T11:05:00Z"
status: "planned"
---

# Track B source connectors and the Qlik glossary

## Goal

Extend the shipped upstream sync with the Collibra and Snowflake read connectors and the Qlik glossary write path, on top of the SDK and engine that v0.1 proved.

## Why it matters

RM-01 ships one proven source-to-Qlik flow. This item is where the connector SDK earns its keep: two more read connectors reusing the same contract, and the glossary path that only a real glossary source such as Collibra can justify.

## Scope

These fifteen tasks were Track B inside RM-01. They were split out so RM-01 completes at the point
the software actually ships — a tagged v0.1 — rather than waiting on work that starts afterwards.
Nothing about the tasks changed; their dependencies still point back into the RM-01 board, and the
ready queue resolves across both.

The executable board is [tools/agent-plan/tasks-rm-05.json](/tools/agent-plan/tasks-rm-05.json).
Every task on it is `blocked`, which is deliberate: they stay that way until v0.1 is tagged, and
whoever starts this item flips them to `pending` as its first step.

The v1 scope guardrails still hold — upstream only, Qlik is the sole write target, the new source
connectors are read-only, no two-way sync, no access-control sync.

## Milestones

- [ ] Unblock the Track B board once v0.1 is tagged.
- [ ] Qlik glossary write path: terms, categories, relations, links, change-status.
- [ ] Collibra read connector: auth, manifest, list_changed, read, mapping, conformance.
- [ ] Snowflake read connector: auth, manifest, list_changed, read, mapping, conformance.
- [ ] Collibra-to-Qlik glossary pilot and Snowflake-to-Qlik pilot.

## Depends on

- [RM-01 Upstream metadata sync MVP](/Roadmap/RM-01-one-way-sync-mvp/item.md) — freezes the
  connector contract, the engine, and the Qlik writer this item builds on.

## Linked research

- [RS-02](/Research/RS-02-qlik-catalog-api/topic.md)
- [RS-03](/Research/RS-03-neutral-metadata-model/topic.md)
- [RS-05](/Research/RS-05-snowflake-catalog-api/topic.md)
- [RS-06](/Research/RS-06-collibra-catalog-api/topic.md)
- [RS-08](/Research/RS-08-connector-plugin-sdk/topic.md)
