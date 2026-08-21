// Pair-level run controls (T13.7's own DoD: "run-now, pause and resume... shown alongside
// the pair, not buried only in run history"). One instance per selected pair, rendered above
// the run-history table in `RunHistoryTab.tsx` -- not a form, not a row action buried in a
// table cell.
//
// "Run-now reflects live status" (the task's own DoD, and mutation check #7) is the one
// property this component exists to guarantee: the running/paused state shown here is never
// inferred from a button's own local "I just clicked it" state. `GET .../run-status` is the
// only source of truth this panel trusts, and it is re-read -- not assumed -- after every
// mutating action:
//
//   - On mount and whenever `pair.id` changes (a fresh read of the real state).
//   - After `run-now` settles, success OR failure (a 409 `sync_cycle_already_running` means
//     someone/something else has a cycle in flight for this pair RIGHT NOW -- the local
//     "not running" guess would be wrong; only a fresh GET tells the truth).
//   - `pause`/`resume` don't need a follow-up GET: `RunControlStatusOut` is already the
//     response body of both (`run_control.py`'s own routes), so this panel applies THAT
//     response directly rather than discarding it and re-fetching -- one fewer round trip,
//     same freshness guarantee.
//
// Run-now is real, synchronous I/O that can legitimately take up to the server's own timeout
// (`run_control.py`'s module docstring: "a request that can legitimately take up to the
// timeout before answering") -- this panel disables the button and shows a busy state for the
// whole wait, never lets a second click queue a second cycle client-side (the server's own
// `_running` set already refuses that with 409, but a disabled button is the honest UI
// reflection of that guarantee, not a substitute for it).
//
// A paused pair, an already-running pair, and a genuine request failure are three different
// facts, not one generic error: pausing a pair is an ordinary configuration fact
// (`enabled: false`), not a failure of anything, so it renders as a calm `Badge`, never a
// toast. An actual failed request -- run-now refused, pause/resume rejected, the status GET
// itself failing -- IS an ordinary request failure and DOES toast, mirroring
// `EndpointsScreen.tsx`'s `handleHealthcheck` precedent for the same distinction.
import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Button, IconButton, Skeleton, toast } from "@elabs-ai/components-ui";
import { Pause, Play, RefreshCw } from "lucide-react";

import { getRunStatus, pausePair, resumePair, runNow, type RunControlStatusOut, type SyncPairOut } from "./runsApi";
import { toApiError } from "../../api/client";

export function RunControlsPanel({
  pair,
  onRunCompleted,
}: {
  pair: SyncPairOut;
  /** Fired after a `run-now` call settles (success or failure) so the caller can refresh the
   * run-history list this pair's rows live in -- this panel does not know about run history
   * at all, it only knows about this one pair's control state. */
  onRunCompleted?: () => void;
}) {
  const [status, setStatus] = useState<RunControlStatusOut | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [runPending, setRunPending] = useState(false);
  const [pausePending, setPausePending] = useState(false);

  // Guards state updates against firing after this panel has unmounted (e.g. the operator
  // switched the pair selector while a run-now was still in flight) -- same discipline as
  // `../pairs/PairsScreen.tsx`'s own `mountedRef`.
  const mountedRef = useRef(true);
  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  const refreshStatus = useCallback(async () => {
    setStatusLoading(true);
    const result = await getRunStatus(pair.id);
    if (!mountedRef.current) return;
    setStatusLoading(false);
    if (result.ok) {
      setStatus(result.data);
    } else {
      toast.error(`Could not read run status for "${pair.name}": ${toApiError(result.error).message}`);
    }
  }, [pair.id, pair.name]);

  useEffect(() => {
    setStatus(null);
    void refreshStatus();
  }, [refreshStatus]);

  async function handleRunNow() {
    setRunPending(true);
    const result = await runNow(pair.id);
    // Always re-read live status after run-now settles, whatever the outcome -- see the
    // module doc comment. This is what makes "run-now reflects live status" true even on the
    // 409/504 paths, not just the happy path.
    await refreshStatus();
    if (!mountedRef.current) return;
    setRunPending(false);
    if (result.ok) {
      const summary = result.data.runs
        .map((report) => `${report.entity_type}: ${report.status}`)
        .join(", ");
      toast.success(`Run started for "${pair.name}" finished -- ${summary || "no entity types ran"}.`);
    } else {
      toast.error(`Run-now for "${pair.name}" did not complete: ${toApiError(result.error).message}`);
    }
    onRunCompleted?.();
  }

  async function handlePauseResume() {
    if (!status) return;
    setPausePending(true);
    const action = status.paused ? resumePair : pausePair;
    const result = await action(pair.id);
    if (!mountedRef.current) return;
    setPausePending(false);
    if (result.ok) {
      setStatus(result.data);
    } else {
      const verb = status.paused ? "resume" : "pause";
      toast.error(`Could not ${verb} "${pair.name}": ${toApiError(result.error).message}`);
    }
  }

  return (
    <section
      aria-label={`Run controls for sync pair "${pair.name}"`}
      className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-surface-muted/40 p-3"
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-caption font-medium text-foreground">{pair.name}</span>
        <div role="status" aria-live="polite" className="flex flex-wrap items-center gap-2">
          {statusLoading && !status ? (
            <Skeleton className="h-5 w-40" />
          ) : status ? (
            <>
              <Badge variant={status.paused ? "secondary" : "success"}>
                {status.paused ? "Paused" : "Active"}
              </Badge>
              {status.running ? <Badge variant="info">Run in progress</Badge> : null}
            </>
          ) : (
            <span className="text-caption text-muted-foreground">Status unavailable.</span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2">
        <IconButton
          label={`Refresh run status for "${pair.name}"`}
          icon={<RefreshCw className={statusLoading ? "animate-spin" : undefined} />}
          variant="ghost"
          disabled={statusLoading}
          onClick={() => void refreshStatus()}
        />
        <Button
          variant="outline"
          disabled={!status || pausePending || statusLoading}
          onClick={() => void handlePauseResume()}
        >
          {status?.paused ? (
            <Play aria-hidden className="mr-1.5 size-4" />
          ) : (
            <Pause aria-hidden className="mr-1.5 size-4" />
          )}
          {status?.paused ? "Resume" : "Pause"}
        </Button>
        <Button
          disabled={!status || status.paused || status.running || runPending}
          onClick={() => void handleRunNow()}
        >
          {runPending ? "Running…" : "Run now"}
        </Button>
      </div>
    </section>
  );
}
