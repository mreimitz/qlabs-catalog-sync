// Pure functions over a dry-run plan (`RunReportsOut`) -- no React, so the pieces that decide
// "which bucket does this record belong in" and "which records name an unresolved reference"
// are testable without rendering anything. Mirrors `../selection/draft.ts`'s own split between
// pure logic and the screen that renders it.
//
// C4's discipline extends here even though this screen has no server-side "evaluator" the way
// selection does: nothing below computes whether an object WOULD be created or updated, nor
// whether a reference resolved -- `SyncRunReportOut.records`, `.status` and `.counts` already
// say that, verbatim off the wire. The one thing this file DOES decide is a display grouping
// over an outcome the server already gave (create / update / no-op / other), never a
// recomputation of the outcome itself.
//
// The D2/D3 gap this screen cannot paper over
// ---------------------------------------------
// `RecordReportOut.target_skipped_fields` is "the channel decisions D2 and D3 use" per
// `run_control.py`'s own module docstring -- an unresolved dataset member or owner is reported
// there, by field name (`dataset_refs` / `owners`, from `qlabs-connector-qlik/write.py`'s
// `PATCH_PATH_FOR_FIELD`). But reading `sync/loop.py` end to end shows this field is NEVER
// populated on a dry run with the engine as it stands today: `_apply_update` (~line 1380) and
// `_create_or_skip` (~line 1574) both return a synthetic `RecordReport` the instant
// `self._dry_run` is true -- before `target.update()`/`target.create()` is ever called -- and
// `target_skipped_fields` defaults to `()` and is only ever set from the RESULT of one of those
// two calls (lines ~1453 and ~1623). D2/D3 resolution itself happens INSIDE
// `qlabs-connector-qlik/write.py`'s `_apply_owners`/`_resolve_datasets`, which a dry run never
// reaches. `unresolvedReferences` below is wired to the real field regardless of this, and will
// correctly surface an entry the day the engine grows a resolve-only preview path (or reads a
// `run-now` response, which DOES populate this honestly) -- it is not a fixture-shaped guess
// that happens to compile.
import type {
  EntityType,
  OrphanReportOut,
  RecordReportOut,
  SyncRunReportOut,
} from "./dryRunApi";

/** The neutral field name D2 (an unresolved dataset member) surfaces under --
 * `qlabs-connector-qlik/write.py`'s `PATCH_PATH_FOR_FIELD["dataset_refs"]` is Qlik's
 * `/datasetIds`, the one field a data product's member datasets travel under. */
export const D2_FIELD = "dataset_refs";

/** The neutral field name D3 (an owner email with no matching Qlik user) surfaces under --
 * `PATCH_PATH_FOR_FIELD["owners"]` is Qlik's `/keyContacts`. */
export const D3_FIELD = "owners";

export type WriteBucket = "create" | "update" | "no_op" | "other";

/** Which of the DoD's three primary buckets (plus "other", so a skipped/filtered/orphaned/
 * failed record is never simply missing from the screen) one outcome belongs in.
 *
 * `unchanged` and `no_op` are both "nothing to write", for different reasons
 * (`RecordOutcome`'s own docstrings in `sync/loop.py`: `unchanged` never entered the write path
 * at all -- every checksum matched; `no_op` did enter it and the target confirmed it already
 * matched) -- grouped together for the DoD's "no-ops" bucket, but a record's own `outcome`
 * field is rendered verbatim alongside it, never relabelled to hide which one it was. */
export function bucketFor(outcome: RecordReportOut["outcome"]): WriteBucket {
  switch (outcome) {
    case "created":
      return "create";
    case "written":
      return "update";
    case "unchanged":
    case "no_op":
      return "no_op";
    default:
      return "other";
  }
}

export interface GroupedRecords {
  creates: RecordReportOut[];
  updates: RecordReportOut[];
  noOps: RecordReportOut[];
  other: RecordReportOut[];
}

/** Split one entity type's records into the DoD's buckets, preserving each record's own order
 * within its bucket (the order the server returned them in). */
export function groupRecords(records: readonly RecordReportOut[]): GroupedRecords {
  const creates: RecordReportOut[] = [];
  const updates: RecordReportOut[] = [];
  const noOps: RecordReportOut[] = [];
  const other: RecordReportOut[] = [];
  for (const record of records) {
    switch (bucketFor(record.outcome)) {
      case "create":
        creates.push(record);
        break;
      case "update":
        updates.push(record);
        break;
      case "no_op":
        noOps.push(record);
        break;
      default:
        other.push(record);
        break;
    }
  }
  return { creates, updates, noOps, other };
}

export interface UnresolvedReference {
  entityType: EntityType;
  record: RecordReportOut;
  kind: "dataset_member" | "owner";
}

/** Every record naming an unresolved reference the operator can act on (RM-01 D2/D3), across
 * every entity type's report in this plan -- pulled here so the screen can render them at the
 * TOP, before any per-entity-type section, per this task's DoD ("called out rather than
 * buried"). One record can appear twice (once per kind) if it names both an unresolved dataset
 * member and an unresolvable owner. */
export function unresolvedReferences(runs: readonly SyncRunReportOut[]): UnresolvedReference[] {
  const found: UnresolvedReference[] = [];
  for (const run of runs) {
    for (const record of run.records) {
      if (record.target_skipped_fields.includes(D2_FIELD)) {
        found.push({ entityType: run.entity_type, record, kind: "dataset_member" });
      }
      if (record.target_skipped_fields.includes(D3_FIELD)) {
        found.push({ entityType: run.entity_type, record, kind: "owner" });
      }
    }
  }
  return found;
}

/** True when nothing in this plan would create or write anything -- the DoD's "no-op plan"
 * case, read the honest way: off `RunCountsOut.created`/`.written`, server-computed
 * (`sync/loop.py`'s own `SyncRunReport.count()`), never a client-side re-count of `records`.
 * Deliberately ignores `skipped`/`filtered`/`orphaned`/`failed` -- those are real, separate
 * facts (the DoD's own "counts are a vocabulary, not a score"), not part of whether this run
 * would CHANGE anything in Qlik. */
export function plansNoChanges(runs: readonly SyncRunReportOut[]): boolean {
  return runs.every((run) => run.counts.created === 0 && run.counts.written === 0);
}

/** True when at least one entity type's cycle could not even be planned -- `RunStatus.FAILED`
 * (`sync/loop.py`: "Nothing was committed"), which for a dry run means "the plan for this
 * entity type could not be produced", not "the plan is empty". Distinguishing the two is a DoD
 * item by name. */
export function hasFailedPlan(runs: readonly SyncRunReportOut[]): boolean {
  return runs.some((run) => run.status === "failed");
}

/** Every orphan across every entity type's report -- read straight off `SyncRunReportOut.orphans`
 * (never re-derived from `records`, even though an orphan also appears there as one
 * `RecordOutcome.ORPHANED` record; the two are the same fact reported on two channels by
 * `sync/loop.py`'s own `SyncRunReport.to_json()`, and this helper reads the dedicated one). */
export function totalOrphans(runs: readonly SyncRunReportOut[]): OrphanReportOut[] {
  return runs.flatMap((run) => run.orphans);
}
