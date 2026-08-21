// The single place this feature decides how a `RunRecordStatus` reads to an operator --
// every other screen in this feature imports this component rather than re-deriving a label
// or a color from the raw status string.
//
// `RunRecordStatus` (`../../api/generated/schema.ts`) is exactly five values: `running`,
// `ok`, `partial`, `failed`, `skipped`. `history.py`'s own module docstring is explicit about
// two things a console must not collapse:
//
//   1. `running` is not a fourth kind of failure -- it is a cycle in flight, made explicit on
//      the wire as `in_progress` (never inferred from the string) so a client cannot get this
//      wrong even by accident. This badge always checks `inProgress` FIRST, before `status`,
//      for exactly that reason: a still-`running` row's `status` column is meaningless while
//      `in_progress` is true.
//   2. A run reaped by `RunRecorder.reap_stale` after its process died is stored as
//      `status: "failed"` (the sweep has no gentler verdict to give it), but it is not the
//      same fact as a cycle that ran to completion and genuinely failed. `swept_stale` is
//      only on `RunDetailOut`/`RunIssuesOut`, not on the plain `RunSummaryOut` the history
//      list renders from -- so the list shows an ordinary "Failed" chip for a swept-stale
//      row, and the run-detail sheet (which loads the full `RunDetailOut`) upgrades that same
//      row to "Abandoned" once it knows. Abandoned gets its own label and its own
//      non-destructive tone, never folded into the same red "Failed" chip a genuine failure
//      gets -- see the mutation table in this task's report for the test that pins this.
//
// Built on `StatusBadge`'s `CustomStatus` escape hatch (`@elabs-ai/components-ui`) rather
// than a hand-rolled `Badge`, per that component's own doc comment: "reach for this only when
// the domain genuinely has a state the 7 [pending/running/complete/awaiting-approval/denied/
// failed/skipped] do not express... map it ONCE, near your domain" -- this module is that one
// mapping. `running`/`skipped`/`failed` reuse the canonical vocabulary verbatim (same label,
// same `STATUS_ROLE` tone); `ok`/`partial`/the swept-stale reading of `failed` are the states
// the canonical seven do not have a member for, so they go through `CustomStatus`.
import { CheckCircle2, CircleDashed, TriangleAlert } from "lucide-react";
import { StatusBadge } from "@elabs-ai/components-ui";

import type { RunRecordStatus } from "./runsApi";

export function RunStatusBadge({
  status,
  inProgress,
  sweptStale = false,
}: {
  status: RunRecordStatus;
  inProgress: boolean;
  /** Only ever meaningfully `true` for `status === "failed"` -- see the module doc comment.
   * Defaults to `false` so call sites that have not loaded a run's full detail (e.g. the
   * history list, which renders from `RunSummaryOut` and does not have this field) can omit
   * it rather than guess. */
  sweptStale?: boolean;
}) {
  // 1. In progress overrides everything else in `status` -- a cycle that has not finished
  // is not a verdict yet, whatever `status` happens to hold meanwhile.
  if (inProgress) {
    return <StatusBadge status="running" />;
  }

  // 2. A failed run reaped from a dead process reads as "abandoned", not "failed" -- a
  // distinct, non-destructive tone, because this is evidence about the PROCESS, not the
  // engine's own verdict on the cycle's work.
  if (status === "failed" && sweptStale) {
    return (
      <StatusBadge
        status={{
          label: "Abandoned (process stopped)",
          tone: "warning",
          icon: TriangleAlert,
        }}
      />
    );
  }

  if (status === "failed") {
    return <StatusBadge status="failed" />;
  }

  if (status === "skipped") {
    return <StatusBadge status="skipped" />;
  }

  if (status === "ok") {
    return <StatusBadge status={{ label: "OK", tone: "success", icon: CheckCircle2 }} />;
  }

  // status === "partial"
  return (
    <StatusBadge status={{ label: "Partial", tone: "warning", icon: CircleDashed }} />
  );
}

/** A small, label-only marker for "this run is still running, so its issue list has not been
 * recorded yet" (`RunIssuesOut.issues_recorded`) -- distinct from both the status badge above
 * and from "no issues found", per `history.py`'s own module docstring: "a console that
 * renders an in-progress run's issue list as 'no issues' would be reporting a false
 * negative." Exported alongside `RunStatusBadge` because every screen that renders one
 * plausibly needs the other. */
export function IssuesNotYetRecordedNote() {
  return (
    <p className="flex items-center gap-1.5 text-caption text-muted-foreground">
      <CircleDashed aria-hidden className="size-3.5" />
      This run is still in progress -- issues are recorded once it finishes.
    </p>
  );
}
