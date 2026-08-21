// Display labels for this screen, kept in one place so the counts strip, the record groups
// and the unresolved-references panel never describe the same enum value two different ways.
// Mirrors `../runs/labels.ts`'s own rationale -- duplicated rather than imported, per
// `dryRunApi.ts`'s own doc comment on why this feature owns its one-file surface.
import type { EntityType, RecordOutcome, RunStatus } from "./dryRunApi";

export const ENTITY_TYPE_LABEL: Record<EntityType, string> = {
  data_product: "Data products",
  dataset: "Datasets",
  glossary_term: "Glossary terms",
  category: "Categories",
};

/** The order entity-type sections render in -- data products first (D1: the top-level synced
 * entity), matching `../pairs/labels.ts`'s own listing order. */
export const ENTITY_TYPE_ORDER: readonly EntityType[] = [
  "data_product",
  "dataset",
  "glossary_term",
  "category",
];

/** One label + one badge variant per `RecordOutcome` -- copied field-for-field from
 * `../runs/labels.ts`'s own `OUTCOME_LABEL`/`OUTCOME_VARIANT` so a record reads the same way
 * whether it is seen here (a plan) or on the Runs screen (what actually happened).
 * `skipped`/`filtered`/`no_op`/`unchanged`/`orphaned` are ordinary, calm outcomes -- never the
 * destructive variant (DoD: "do not render skipped or filtered as failures"). Only `failed`
 * gets the destructive variant. */
export const OUTCOME_LABEL: Record<RecordOutcome, string> = {
  created: "Created",
  written: "Written",
  unchanged: "Unchanged",
  no_op: "No-op",
  skipped: "Skipped",
  orphaned: "Orphaned",
  filtered: "Filtered",
  failed: "Failed",
};

export const OUTCOME_VARIANT: Record<
  RecordOutcome,
  "success" | "secondary" | "outline" | "destructive"
> = {
  created: "success",
  written: "success",
  unchanged: "secondary",
  no_op: "secondary",
  skipped: "outline",
  orphaned: "outline",
  filtered: "outline",
  failed: "destructive",
};

/** `RunCountsOut`'s own fields, in the fixed order every counts strip on this screen renders
 * them -- a vocabulary, not a score (DoD: never summed into one "changed" number). Mirrors
 * `../runs/labels.ts`'s `COUNT_FIELD_ORDER`, minus `write` and `error` -- run-control's own
 * `RunCountsOut` (`qlabs_catalog_sync__api__routes__run_control__RunCountsOut`) has neither
 * field; it is the per-cycle shape `sync/loop.py`'s `SyncRunReport.to_json()` produces
 * directly, not the run-history row `../runs/runsApi.ts` reads. */
export const COUNT_FIELD_ORDER = [
  "read",
  "created",
  "written",
  "unchanged",
  "no_op",
  "skipped",
  "orphaned",
  "filtered",
  "failed",
] as const;

export const COUNT_FIELD_LABEL: Record<(typeof COUNT_FIELD_ORDER)[number], string> = {
  read: "Read",
  created: "Created",
  written: "Written",
  unchanged: "Unchanged",
  no_op: "No-op",
  skipped: "Skipped",
  orphaned: "Orphaned",
  filtered: "Filtered",
  failed: "Failed",
};

/** One label + one badge variant per `RunStatus` -- worded for a PLAN rather than a completed
 * cycle (`sync/loop.py`'s own `RunStatus` docstrings talk about commits and watermarks, which
 * do not apply to a dry run: `committed` is always `false` here). `failed` is deliberately the
 * only destructive one -- "could not produce a plan" is a real failure; `skipped` here just
 * means this pair is not configured to sync this entity type at all, an ordinary fact. */
export const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  ok: "Plan complete",
  partial: "Plan complete — work would remain outstanding",
  failed: "Could not produce a plan",
  skipped: "Not configured for this pair",
};

export const RUN_STATUS_VARIANT: Record<
  RunStatus,
  "success" | "warning" | "destructive" | "secondary"
> = {
  ok: "success",
  partial: "warning",
  failed: "destructive",
  skipped: "secondary",
};
