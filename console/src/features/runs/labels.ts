// Display labels shared across this feature's screens, kept in one place so the history
// table, the run-detail sheet and the issues panel never drift into describing the same enum
// value two different ways. Mirrors `../pairs/labels.ts`'s own rationale.
import type { EntityType, RecordOutcome } from "./runsApi";

/** Every `EntityType` the SDK defines, in the same order `../pairs/labels.ts` lists them --
 * duplicated rather than imported, see `runsApi.ts`'s own doc comment for why this feature
 * does not reach into `../pairs/`. */
export const ENTITY_TYPE_LABEL: Record<EntityType, string> = {
  data_product: "Data product",
  dataset: "Dataset",
  glossary_term: "Glossary term",
  category: "Category",
};

/** One label + one badge variant per `RecordOutcome` -- the vocabulary a single candidate
 * object's write resolved to. `skipped`/`filtered`/`no_op`/`unchanged` are ordinary, calm
 * outcomes (never the destructive variant -- DoD: "do not render skipped or filtered as
 * failures"). `orphaned` is reported, never destructive either: an orphan is a source object
 * that vanished, not something this engine deleted from Qlik (D4) -- see
 * `RunOrphanBadge` in `RunIssuesPanel.tsx` for the fuller treatment orphans get in the issues
 * list specifically. Only `failed` -- a genuine per-record failure -- gets the destructive
 * variant. */
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

/** One label per `RunCountsOut` field, in the fixed order every counts grid in this feature
 * renders them -- a vocabulary, not a score (DoD: never summed into one "changed" number).
 * `write` is `created + written`, already computed server-side (`RunRecordStatus`'s own
 * module docstring on `RunRow`) -- displaying it is not this UI inventing a second
 * aggregation, it is showing a field the API already names and defines. */
export const COUNT_FIELD_ORDER = [
  "read",
  "created",
  "written",
  "write",
  "unchanged",
  "no_op",
  "skipped",
  "orphaned",
  "filtered",
  "failed",
  "error",
] as const;

export const COUNT_FIELD_LABEL: Record<(typeof COUNT_FIELD_ORDER)[number], string> = {
  read: "Read",
  created: "Created",
  written: "Written",
  write: "Write (created + written)",
  unchanged: "Unchanged",
  no_op: "No-op",
  skipped: "Skipped",
  orphaned: "Orphaned",
  filtered: "Filtered",
  failed: "Failed",
  error: "Error",
};
