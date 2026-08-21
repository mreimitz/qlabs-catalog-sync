/* Dry run (T13.6) -- the planned write set for a pair, reviewed BEFORE anything is applied to
 * the operator's Qlik tenant.
 *
 * The one rule this screen is built around
 * ----------------------------------------
 * `run_control.py`'s own module docstring: "the plan is `SyncRunReport`, not a second shape...
 * the console never renders something the CLI's own `dry-run` did not already print." So this
 * screen computes no verdict of its own -- no outcome, no count, no "this would fail" is
 * decided here. Every one of those arrives on `RunReportsOut` and is rendered as given. The
 * only client-side derivation in this feature is a display GROUPING over an outcome the server
 * already gave (`planGrouping.ts`'s `groupRecords`), never a recomputation of the outcome
 * itself.
 *
 * A dry run is a deliberate action, never an effect
 * --------------------------------------------------
 * `POST /pairs/{id}/dry-run` does real read I/O against the source connector -- potentially
 * many pages of it -- run synchronously, bounded by the server's own ~120s timeout. Firing that
 * on mount, or on every pair selection, would mean picking a pair in the dropdown alone costs a
 * real, possibly slow round trip against a live tenant. So this screen never calls the route
 * except from `handleRunDryRun`, wired to an explicit "Run dry run" button click -- selecting a
 * different pair only clears whatever plan is on screen, it never re-fetches one.
 *
 * Three distinct "nothing happened" states, never confused
 * ----------------------------------------------------------
 *  - **The request itself failed** (network, 422 `endpoint_setup_failed`, 504 timeout, ...) --
 *    `runState.status === "error"`, a `StatePanel kind="error"` replaces the whole results area.
 *  - **One entity type's cycle could not be planned** -- `SyncRunReportOut.status === "failed"`
 *    inside an otherwise-successful response; `EntityTypeSection` renders that one section as a
 *    failure while any OTHER entity type's plan (which may have succeeded) still renders
 *    normally next to it.
 *  - **The plan is genuinely empty / changes nothing** -- a real, positive answer
 *    (`plansNoChanges` in `planGrouping.ts`), rendered as a confident banner, never as a blank
 *    region indistinguishable from "still loading" or "the request failed".
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Badge,
  Button,
  Checkbox,
  FieldRow,
  Label,
  SectionHeader,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  StatePanel,
  Text,
  TooltipProvider,
} from "@elabs-ai/components-ui";
import { CheckCircle2, Info } from "lucide-react";

import { listPairs, runDryRun, type RunReportsOut, type SyncPairOut } from "./dryRunApi";
import { EntityTypeSection } from "./EntityTypeSection";
import { UnresolvedReferencesPanel } from "./UnresolvedReferencesPanel";
import { hasFailedPlan, plansNoChanges } from "./planGrouping";
import { ENTITY_TYPE_ORDER } from "./labels";

type PairsLoad =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "loaded" };

type RunState =
  | { status: "idle" }
  | { status: "running"; startedAt: number }
  | { status: "error"; message: string }
  | { status: "loaded"; data: RunReportsOut };

/** The server's own bound (`run_control.py`'s `DEFAULT_DRY_RUN_TIMEOUT_SECONDS`), restated here
 * only for the operator-facing caption -- never used to time anything out client-side; the
 * request either resolves or the server itself answers with a 504. */
const SERVER_TIMEOUT_SECONDS = 120;

export function DryRunScreen() {
  const [pairsLoad, setPairsLoad] = useState<PairsLoad>({ status: "loading" });
  const [pairs, setPairs] = useState<SyncPairOut[]>([]);
  const [pairId, setPairId] = useState<string | null>(null);
  const [createMissing, setCreateMissing] = useState(false);
  const [runState, setRunState] = useState<RunState>({ status: "idle" });
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  const mountedRef = useRef(true);
  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  const fetchPairs = useCallback(async () => {
    setPairsLoad({ status: "loading" });
    const result = await listPairs();
    if (!mountedRef.current) return;
    if (!result.ok) {
      setPairsLoad({ status: "error", message: result.error.message });
      return;
    }
    setPairs(result.data);
    setPairsLoad({ status: "loaded" });
  }, []);

  useEffect(() => {
    void fetchPairs();
  }, [fetchPairs]);

  const pair = useMemo(() => pairs.find((row) => row.id === pairId) ?? null, [pairs, pairId]);

  // Switching pairs clears whatever plan is on screen -- a stale plan for a pair that is no
  // longer selected is exactly the kind of thing that could be mistaken for a fresh answer
  // about the newly-selected one.
  function selectPair(id: string) {
    setPairId(id);
    setRunState({ status: "idle" });
  }

  // Ticks the elapsed-time caption shown while a dry run is in flight -- an honest "N seconds
  // elapsed", never a fabricated percentage (there is nothing this screen can know about how
  // far through a real, multi-page read the server is).
  useEffect(() => {
    if (runState.status !== "running") return;
    const id = window.setInterval(() => {
      setElapsedSeconds(Math.floor((Date.now() - runState.startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(id);
  }, [runState]);

  async function handleRunDryRun() {
    if (pair === null) return;
    setElapsedSeconds(0);
    setRunState({ status: "running", startedAt: Date.now() });
    const result = await runDryRun(pair.id, { create_missing: createMissing });
    if (!mountedRef.current) return;
    if (result.ok) {
      setRunState({ status: "loaded", data: result.data });
    } else {
      setRunState({ status: "error", message: result.error.message });
    }
  }

  const running = runState.status === "running";
  const sortedRuns = useMemo(() => {
    if (runState.status !== "loaded") return [];
    return [...runState.data.runs].sort(
      (a, b) => ENTITY_TYPE_ORDER.indexOf(a.entity_type) - ENTITY_TYPE_ORDER.indexOf(b.entity_type),
    );
  }, [runState]);

  return (
    <TooltipProvider>
      <div className="flex flex-col gap-6">
        <SectionHeader
          title="Dry run"
          description="See exactly what a run would change before anything is applied. This is real read I/O against the pair's source and can take a while -- it never fires automatically."
        />

        {pairsLoad.status === "error" ? (
          <StatePanel
            kind="error"
            title="Could not load sync pairs"
            description={pairsLoad.message}
            actions={<Button onClick={() => void fetchPairs()}>Retry</Button>}
          />
        ) : null}

        <Select value={pairId ?? undefined} onValueChange={selectPair}>
          <FieldRow
            label="Sync pair"
            description="Plan this pair's next cycle. Nothing is written until a real run happens."
          >
            <SelectTrigger disabled={pairsLoad.status !== "loaded" || pairs.length === 0 || running}>
              <SelectValue
                placeholder={pairs.length === 0 ? "No sync pairs configured yet" : "Select a sync pair"}
              />
            </SelectTrigger>
          </FieldRow>
          <SelectContent>
            {pairs.map((row) => (
              <SelectItem key={row.id} value={row.id}>
                {row.name} ({row.source} → {row.target})
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {pairsLoad.status === "loaded" && pairs.length === 0 ? (
          <StatePanel
            kind="empty"
            title="No sync pairs configured yet"
            description="A dry run plans one pair's next cycle. Create a pair on the Sync pairs screen first."
          />
        ) : null}

        {pair !== null ? (
          <div className="flex flex-col gap-3 rounded-md border border-border p-4">
            <div className="flex items-center gap-2">
              <Checkbox
                id="dry-run-create-missing"
                checked={createMissing}
                onCheckedChange={(checked) => setCreateMissing(checked === true)}
                disabled={running}
              />
              <Label htmlFor="dry-run-create-missing">Preview creates for unbound objects</Label>
            </div>
            <Text variant="caption" tone="muted">
              Off by default: a source object with no confirmed Qlik binding previews as skipped,
              not created -- matching what a real run does without this opt-in. Turning it on
              previews what the create would look like; it still writes nothing.
            </Text>

            <div className="flex items-center gap-3">
              <Button disabled={running} onClick={() => void handleRunDryRun()}>
                {running ? `Running… ${elapsedSeconds}s` : "Run dry run"}
              </Button>
              <div role="status" aria-live="polite" className="text-caption text-muted-foreground">
                {running
                  ? `Reading "${pair.name}"'s source live. This can take up to ${SERVER_TIMEOUT_SECONDS}s.`
                  : null}
              </div>
            </div>
          </div>
        ) : null}

        {runState.status === "error" ? (
          <StatePanel
            kind="error"
            title="Could not produce a dry-run plan"
            description={runState.message}
            actions={<Button onClick={() => void handleRunDryRun()}>Retry</Button>}
          />
        ) : null}

        {runState.status === "loaded" ? (
          <div className="flex flex-col gap-6">
            <div className="flex flex-wrap items-start gap-2 rounded-md border border-border bg-surface-muted/40 p-3">
              <Info aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
              <Text variant="caption">
                Plan generated {new Date(runState.data.generated_at).toLocaleString()} for{" "}
                <span className="font-medium">{runState.data.pair_name}</span>. This is a plan
                only -- nothing above has been written to Qlik.
              </Text>
            </div>

            {hasFailedPlan(sortedRuns) ? (
              <Badge variant="destructive" className="w-fit">
                {sortedRuns.filter((run) => run.status === "failed").length} of {sortedRuns.length}{" "}
                entity type(s) could not be planned -- see below
              </Badge>
            ) : plansNoChanges(sortedRuns) ? (
              <div
                role="status"
                className="flex items-center gap-2 rounded-md border border-success/40 bg-success/5 p-3"
              >
                <CheckCircle2 aria-hidden className="size-5 text-success" />
                <Text className="font-medium">
                  This run would change nothing. Every evaluated object already matches Qlik, or
                  has no confirmed target binding to create.
                </Text>
              </div>
            ) : (
              <Text variant="body">
                This run would create{" "}
                <span className="font-medium tabular-nums">
                  {sortedRuns.reduce((sum, run) => sum + run.counts.created, 0)}
                </span>{" "}
                and write{" "}
                <span className="font-medium tabular-nums">
                  {sortedRuns.reduce((sum, run) => sum + run.counts.written, 0)}
                </span>{" "}
                object(s) across {sortedRuns.length} entity type(s).
              </Text>
            )}

            <UnresolvedReferencesPanel runs={sortedRuns} />

            {sortedRuns.length === 0 ? (
              <StatePanel
                kind="empty"
                title="Nothing to plan"
                description="This pair has no entity types configured to sync."
              />
            ) : (
              sortedRuns.map((report) => (
                <EntityTypeSection key={report.entity_type} report={report} />
              ))
            )}
          </div>
        ) : null}
      </div>
    </TooltipProvider>
  );
}
