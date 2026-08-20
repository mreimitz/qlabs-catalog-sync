---
type: "Decision"
title: "Decision: v1 scope — upstream-only, no access-control sync"
description: "Scopes v1 to upstream-only metadata sync with Qlik as the sole writer, defers two-way sync and access-control sync, and treats owners as best-effort metadata."
tags: ["decision", "RM-01", "scope", "v1"]
timestamp: "2026-08-06T12:30:00Z"
status: "accepted"
---

# Decision: v1 scope — upstream-only, no access-control sync

## Context

The research base (RS-01 through RS-09) shows that the four catalogs share a syncable core of
descriptive metadata but differ sharply in their data-product concepts, identity keys, write
semantics, and — most of all — their access-control models. Two-way sync requires a bidirectional
conflict engine, and access-control sync requires an identity-resolution layer and lossy model
mapping. Both are large and risky relative to the core value of moving data-product metadata into
Qlik.

## Decision

For v1 (RM-01):

1. **Upstream-only.** Sync flows from source catalogs (Databricks, then Collibra and Snowflake)
   into Qlik. Sources are read-only; Qlik is the single write target.
2. **No two-way sync.** Bidirectional reconciliation and the full conflict engine are deferred
   (RM-02). If a Qlik-side value is edited manually, the default policy is source-wins overwrite,
   configurable to preserve local edits.
3. **No access-control sync.** Access/authorization is entirely out of v1. The connector SDK leaves
   read-only Principal/AccessBinding entities unimplemented (declared unsupported). Access observe-
   and-report is tracked as RM-04, backed by RS-09.
4. **Owners as best-effort metadata.** Owner/contact fields are copied as plain metadata correlated
   on email, with no correctness guarantees, and must not be turned into an identity-resolution
   system.

## Consequences

- Only one write adapter (Qlik) is built in v1; source connectors implement read paths only.
- The conflict engine (RS-04) shrinks to the single manual-edit policy above.
- Qlik's lack of change events stops mattering for the sync direction; the engine polls sources.
- Glossary/term sync into Qlik (which has a first-class glossary) is clean, making Collibra to Qlik
  a high-value early flow.
- Access work, and the identity-resolution layer it depends on, is preserved as designed in RS-09
  for a later, read-only-first increment.

# Citations

* [Access-Control Sync — Options, Risks & Recommendation](/Research/RS-09-access-control-sync/outputs/access-control-sync-options.md) — basis for deferring access and starting read-only.
* [Neutral Metadata Model Specification (v1)](/Research/RS-03-neutral-metadata-model/outputs/neutral-metadata-model-spec.md) — the model this scope builds on.
* [Standalone Python Sync Service — Architecture & Tech Stack](/Research/RS-07-architecture-techstack-references/outputs/architecture-and-techstack.md) — architecture the upstream-only simplification applies to.
