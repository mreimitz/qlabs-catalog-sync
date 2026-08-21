// The run-detail panel (DoD: "run detail with counts and issues, the orphan and
// unresolved-reference report"). A right-side `Sheet`, per `app-spec.md`'s shell archetype B
// ("a right-side detail panel (Sheet/Drawer) for inspecting one object... without leaving the
// list") -- opened from a row in `RunHistoryTab.tsx`'s table, never a route of its own (this
// feature owns exactly one nav route, `/runs`, per `../../app/routes.ts`).
//
// Fetches `GET /api/runs/{run_id}` and `GET /api/runs/{run_id}/issues` only when actually
// open, exactly like `../endpoints/EndpointManifestSheet.tsx`'s own doc comment describes for
// its manifest fetch -- never once per row on the list's own load.
//
// A run whose issues include real errors, or whose status is `failed`/`partial`, is still a
// SUCCESSFUL read of this run's own record -- `get_run`/`get_run_issues` answering with real
// facts about a bad cycle is not a failure of the request that asked. This component never
// toasts for that. It DOES toast if the fetch itself fails (network error, 404 because the
// run id is stale, ...), the same distinction `RunControlsPanel.tsx` and
// `../endpoints/EndpointsScreen.tsx` already draw.
import { useEffect, useState } from "react";
import {
  Descriptions,
  DescriptionsItem,
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  Skeleton,
  StatePanel,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@elabs-ai/components-ui";

import { getRun, getRunIssues, type RunCountsOut, type RunDetailOut, type RunIssuesOut } from "./runsApi";
import { RunIssuesPanel } from "./RunIssuesPanel";
import { RunStatusBadge } from "./RunStatusBadge";
import { COUNT_FIELD_LABEL, COUNT_FIELD_ORDER, ENTITY_TYPE_LABEL } from "./labels";
import { formatDateTime, formatDuration } from "./format";
import { toApiError } from "../../api/client";

type FetchState =
  | { status: "loading" }
  | { status: "loaded"; run: RunDetailOut; issues: RunIssuesOut }
  | { status: "error"; message: string };

/** Every `RunCountsOut` field, rendered as its own labeled tile -- a vocabulary, not a score
 * (DoD: never summed into one "changed" number). Plain `Descriptions` tiles rather than a
 * chart-package `MetricCard`: `numeric` gets each value `tabular-nums` so the column of
 * digits still aligns, with no new package dependency this feature does not own
 * `package.json` to add. */
function RunCountsGrid({ counts }: { counts: RunCountsOut }) {
  return (
    <Descriptions columns={3} layout="vertical" aria-label="Run counts, by outcome">
      {COUNT_FIELD_ORDER.map((field) => (
        <DescriptionsItem key={field} label={COUNT_FIELD_LABEL[field]} numeric>
          {counts[field]}
        </DescriptionsItem>
      ))}
    </Descriptions>
  );
}

export function RunDetailSheet({
  runId,
  open,
  onOpenChange,
}: {
  runId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [state, setState] = useState<FetchState>({ status: "loading" });

  useEffect(() => {
    if (!open || !runId) return;
    let cancelled = false;
    setState({ status: "loading" });
    void Promise.all([getRun(runId), getRunIssues(runId)]).then(([runResult, issuesResult]) => {
      if (cancelled) return;
      if (!runResult.ok) {
        setState({ status: "error", message: toApiError(runResult.error).message });
        return;
      }
      if (!issuesResult.ok) {
        setState({ status: "error", message: toApiError(issuesResult.error).message });
        return;
      }
      setState({ status: "loaded", run: runResult.data, issues: issuesResult.data });
    });
    return () => {
      cancelled = true;
    };
  }, [open, runId]);

  const run = state.status === "loaded" ? state.run : null;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-2xl">
        <SheetHeader>
          <SheetTitle>
            {run ? `Run: ${run.pair} — ${ENTITY_TYPE_LABEL[run.entity_type]}` : "Run detail"}
          </SheetTitle>
          <SheetDescription>
            {run
              ? `Started ${formatDateTime(run.started_at)}, ${run.dry_run ? "dry run" : "real cycle"}.`
              : "Counts, items and issues for one run."}
          </SheetDescription>
        </SheetHeader>

        <div className="flex flex-col gap-6 px-4 pb-4">
          {state.status === "loading" ? (
            <div className="flex flex-col gap-2" aria-live="polite" aria-busy="true">
              <Skeleton className="h-6 w-1/2" />
              <Skeleton className="h-32 w-full" />
              <Skeleton className="h-32 w-full" />
            </div>
          ) : state.status === "error" ? (
            <StatePanel kind="error" title="Could not load this run" description={state.message} />
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <RunStatusBadge
                  status={state.run.status}
                  inProgress={state.run.in_progress}
                  sweptStale={state.run.swept_stale}
                />
                {state.run.dry_run ? null : (
                  <span className="text-caption text-muted-foreground">
                    {state.run.committed ? "Committed" : "Not committed"}
                  </span>
                )}
              </div>

              <Descriptions columns={2}>
                <DescriptionsItem label="Source endpoint">{state.run.source_endpoint}</DescriptionsItem>
                <DescriptionsItem label="Target endpoint">{state.run.target_endpoint}</DescriptionsItem>
                <DescriptionsItem label="Started">{formatDateTime(state.run.started_at)}</DescriptionsItem>
                <DescriptionsItem label="Finished">{formatDateTime(state.run.finished_at)}</DescriptionsItem>
                <DescriptionsItem label="Duration" numeric>
                  {formatDuration(state.run.duration_seconds)}
                </DescriptionsItem>
                <DescriptionsItem label="Pages" numeric>
                  {state.run.pages}
                </DescriptionsItem>
                <DescriptionsItem label="Watermark before">
                  {state.run.watermark_before ?? "—"}
                </DescriptionsItem>
                <DescriptionsItem label="Watermark after">
                  {state.run.watermark_after ?? "—"}
                </DescriptionsItem>
                <DescriptionsItem label="Watermark advanced">
                  {state.run.watermark_advanced ? "Yes" : "No"}
                </DescriptionsItem>
                <DescriptionsItem label="Create missing enabled">
                  {state.run.create_enabled ? "Yes" : "No"}
                </DescriptionsItem>
              </Descriptions>

              {state.run.quarantined_endpoints.length > 0 ? (
                <StatePanel
                  kind="error"
                  title="Quarantined endpoints"
                  description={`This run's cycle quarantined: ${state.run.quarantined_endpoints.join(", ")}.`}
                />
              ) : null}

              <Tabs defaultValue="counts">
                <TabsList>
                  <TabsTrigger value="counts">Counts</TabsTrigger>
                  <TabsTrigger value="issues">Issues</TabsTrigger>
                </TabsList>
                <TabsContent value="counts">
                  <RunCountsGrid counts={state.run.counts} />
                </TabsContent>
                <TabsContent value="issues">
                  <RunIssuesPanel issues={state.issues} />
                </TabsContent>
              </Tabs>
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
