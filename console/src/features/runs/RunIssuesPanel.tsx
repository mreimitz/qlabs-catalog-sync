// Renders `RunIssuesOut` (`GET /api/runs/{run_id}/issues`) -- "what this run could not
// resolve", per that route's own summary: unresolved dataset members (D2), unresolvable
// owners (D3), orphans (D4), everything else still outstanding, and errors.
//
// Two DoD items this module exists to satisfy:
//
//   - **"Issues link back to the object that caused them."** Every `RunItemOut` this panel
//     renders shows its `native_key` (always present) and `display_name` (when the source
//     could report one) as the row's own identity, never just a reason string floating with
//     nothing to point at.
//   - **"The UI never implies an orphan was deleted."** `RunOrphanBadge` below is the one
//     place in this feature that renders an orphan, and it is deliberately never the
//     `destructive` badge variant, never the word "deleted" or "removed" -- see its own doc
//     comment.
//
// `issues_recorded` is false only while a run is still `RUNNING` (`history.py`'s module
// docstring: "a console that renders an in-progress run's issue list as 'no issues' would be
// reporting a false negative") -- this panel renders `IssuesNotYetRecordedNote` instead of an
// empty-issues message in that case. Nothing in this component ever calls `toast` -- a run
// that reports issues is not a request failure (`get_run_issues` succeeded; it is reporting
// real, ordinary facts about the run), so this stays inline, calm rendering throughout. The
// caller (`RunDetailSheet.tsx`) is the only place that toasts, and only for a genuine failed
// fetch of this data.
import {
  Badge,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@elabs-ai/components-ui";

import { IssuesNotYetRecordedNote } from "./RunStatusBadge";
import { OUTCOME_LABEL, OUTCOME_VARIANT } from "./labels";
import type { RunErrorOut, RunIssuesOut, RunItemOut, RunOrphanIssueOut } from "./runsApi";

function ObjectIdentity({ item }: { item: RunItemOut }) {
  return (
    <div className="flex flex-col">
      <span className="font-medium text-foreground">{item.display_name ?? item.native_key}</span>
      {item.display_name ? (
        <span className="text-caption text-muted-foreground">{item.native_key}</span>
      ) : null}
      {item.endpoint ? (
        <span className="text-caption text-muted-foreground">endpoint: {item.endpoint}</span>
      ) : null}
    </div>
  );
}

function RunItemsTable({ items, caption }: { items: RunItemOut[]; caption: string }) {
  if (items.length === 0) {
    return <p className="text-caption text-muted-foreground">None.</p>;
  }
  return (
    <Table>
      <TableCaption className="sr-only">{caption}</TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead>Object</TableHead>
          <TableHead>Outcome</TableHead>
          <TableHead>Unresolved fields</TableHead>
          <TableHead>Detail</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((item) => (
          <TableRow key={item.id}>
            <TableCell>
              <ObjectIdentity item={item} />
            </TableCell>
            <TableCell>
              <Badge variant={OUTCOME_VARIANT[item.outcome]}>{OUTCOME_LABEL[item.outcome]}</Badge>
            </TableCell>
            <TableCell>
              {item.unresolved_fields.length > 0 ? item.unresolved_fields.join(", ") : "—"}
            </TableCell>
            <TableCell className="max-w-xs">
              <span className="text-caption">{item.detail ?? item.reason ?? "—"}</span>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/** An orphan's own badge -- deliberately never `destructive`, never the word "deleted" or
 * "removed", anywhere. D4: the engine never deletes anything in Qlik; an orphan is a source
 * object that vanished, reported so an operator can decide what (if anything) to do about it
 * in Qlik themselves. `still_open` (still missing as of the most recent cycle that checked)
 * vs. resolved (`resolved_at` set -- the source object reappeared) is a real, separate fact
 * worth its own reading, so it gets a second, distinct badge rather than being folded into
 * one "orphaned" chip that could be misread as a verdict either way. */
function RunOrphanBadge({ orphan }: { orphan: RunOrphanIssueOut }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Badge variant="outline">Orphaned -- reported, not deleted</Badge>
      {orphan.still_open ? (
        <Badge variant="warning">Still missing from source</Badge>
      ) : (
        <Badge variant="secondary">Seen again since</Badge>
      )}
    </div>
  );
}

function OrphansSection({ orphans }: { orphans: RunOrphanIssueOut[] }) {
  if (orphans.length === 0) {
    return <p className="text-caption text-muted-foreground">None.</p>;
  }
  return (
    <ul className="flex flex-col gap-3">
      {orphans.map((orphan) => (
        <li key={orphan.run_item_id} className="rounded-md border border-border p-2">
          <div className="flex flex-col gap-1">
            <span className="font-medium text-foreground">
              {orphan.display_name ?? orphan.native_key ?? orphan.run_item_id}
            </span>
            {orphan.native_key && orphan.display_name ? (
              <span className="text-caption text-muted-foreground">{orphan.native_key}</span>
            ) : null}
            {orphan.endpoint ? (
              <span className="text-caption text-muted-foreground">endpoint: {orphan.endpoint}</span>
            ) : null}
            <RunOrphanBadge orphan={orphan} />
            {!orphan.orphan_log_found ? (
              <span className="text-caption text-muted-foreground">
                No matching orphan-lifecycle record was found for this item (expected only for a
                dry run).
              </span>
            ) : null}
          </div>
        </li>
      ))}
    </ul>
  );
}

function ErrorsSection({ errors }: { errors: RunErrorOut[] }) {
  if (errors.length === 0) {
    return <p className="text-caption text-muted-foreground">None.</p>;
  }
  return (
    <ul className="flex flex-col gap-2">
      {errors.map((error) => (
        <li key={error.id} className="rounded-md border border-border p-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={error.is_stale_sweep ? "warning" : "destructive"}>
              {error.is_stale_sweep ? "Abandoned (process stopped)" : error.kind}
            </Badge>
            {error.native_key ? (
              <span className="text-caption text-muted-foreground">{error.native_key}</span>
            ) : null}
            {error.endpoint ? (
              <span className="text-caption text-muted-foreground">endpoint: {error.endpoint}</span>
            ) : null}
          </div>
          <p className="text-caption">{error.message}</p>
        </li>
      ))}
    </ul>
  );
}

export function RunIssuesPanel({ issues }: { issues: RunIssuesOut }) {
  if (!issues.issues_recorded) {
    return <IssuesNotYetRecordedNote />;
  }

  if (!issues.has_issues) {
    return <p className="text-body text-muted-foreground">No issues recorded for this run.</p>;
  }

  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-2">
        <h4 className="text-body font-medium">Unresolved dataset members (D2)</h4>
        <p className="text-caption text-muted-foreground">
          Dataset members named by the source that could not be matched to a member in the
          target space.
        </p>
        <RunItemsTable
          items={issues.unresolved_dataset_members}
          caption="Unresolved dataset members"
        />
      </section>

      <section className="flex flex-col gap-2">
        <h4 className="text-body font-medium">Unresolvable owners (D3)</h4>
        <p className="text-caption text-muted-foreground">
          Owner emails with no matching Qlik user -- best-effort metadata, never an identity
          system.
        </p>
        <RunItemsTable items={issues.unresolvable_owners} caption="Unresolvable owners" />
      </section>

      <section className="flex flex-col gap-2">
        <h4 className="text-body font-medium">Orphans (D4)</h4>
        <p className="text-caption text-muted-foreground">
          Source objects that vanished. The engine never deletes anything in Qlik -- these are
          reported for an operator to review, not removed automatically.
        </p>
        <OrphansSection orphans={issues.orphans} />
      </section>

      <section className="flex flex-col gap-2">
        <h4 className="text-body font-medium">Other outstanding</h4>
        <p className="text-caption text-muted-foreground">
          Every other reportable item this run did not fully resolve -- a failed record, or one
          holding the watermark for a reason that is neither D2 nor D3.
        </p>
        <RunItemsTable items={issues.other_outstanding} caption="Other outstanding items" />
      </section>

      <section className="flex flex-col gap-2">
        <h4 className="text-body font-medium">Errors</h4>
        <ErrorsSection errors={issues.errors} />
      </section>
    </div>
  );
}
