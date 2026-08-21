// Run history per pair, with status and duration (DoD), plus the run-now/pause/resume
// controls for whichever pair is selected. The "History" tab of `RunsScreen.tsx`.
//
// **Keyset pagination, not a numbered pager.** `GET /api/runs` returns
// `{items, limit, has_more, next_cursor}` -- an opaque cursor, never a page number
// (`history.py`'s own module docstring: OFFSET pagination "silently skips or repeats rows
// when the underlying table gains or loses rows between two page requests", which is exactly
// wrong for a table new runs are landing in continuously). This screen reflects that
// contract literally: fetched pages accumulate into one growing list, a "Load more" button
// is the only way to ask for the next page, it is disabled/hidden once `has_more` is false,
// and it is NEVER replaced by page-number buttons -- `@elabs-ai/components-ui`'s own
// `Pagination`/`PaginationNext`/`PaginationPrevious` are for the client-side/offset case and
// are deliberately not imported here.
//
// Changing a filter (pair or status) resets pagination back to a fresh first page -- a
// cursor encodes a position in ONE ordered, filtered result set (`_decode_cursor` in
// `history.py`), so a cursor obtained under one filter is meaningless (or, worse, silently
// wrong) under another.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Button,
  ResultCount,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  StatePanel,
} from "@elabs-ai/components-ui";
import { DataTable, type ColumnDef } from "@elabs-ai/components-data";

import { listPairs, listRuns, type RunRecordStatus, type RunSummaryOut, type SyncPairOut } from "./runsApi";
import { RunControlsPanel } from "./RunControlsPanel";
import { RunDetailSheet } from "./RunDetailSheet";
import { RunStatusBadge } from "./RunStatusBadge";
import { ENTITY_TYPE_LABEL } from "./labels";
import { formatDateTime, formatDuration } from "./format";
import { toApiError } from "../../api/client";

const ALL_PAIRS = "__all__";
const ALL_STATUSES = "__all__";

const STATUS_OPTIONS: readonly RunRecordStatus[] = ["running", "ok", "partial", "failed", "skipped"];
const STATUS_OPTION_LABEL: Record<RunRecordStatus, string> = {
  running: "Running",
  ok: "OK",
  partial: "Partial",
  failed: "Failed",
  skipped: "Skipped",
};

type LoadState = { status: "loading" } | { status: "error"; message: string } | { status: "loaded" };

export function RunHistoryTab() {
  const [pairsLoad, setPairsLoad] = useState<LoadState>({ status: "loading" });
  const [pairs, setPairs] = useState<SyncPairOut[]>([]);
  const [selectedPairName, setSelectedPairName] = useState<string>(ALL_PAIRS);
  const [selectedStatus, setSelectedStatus] = useState<string>(ALL_STATUSES);

  const [runs, setRuns] = useState<RunSummaryOut[]>([]);
  const [runsLoad, setRunsLoad] = useState<LoadState>({ status: "loading" });
  const [hasMore, setHasMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const [detailRunId, setDetailRunId] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  const mountedRef = useRef(true);
  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  useEffect(() => {
    void listPairs().then((result) => {
      if (!mountedRef.current) return;
      if (result.ok) {
        setPairs(result.data);
        setPairsLoad({ status: "loaded" });
      } else {
        setPairsLoad({ status: "error", message: toApiError(result.error).message });
      }
    });
  }, []);

  const fetchFirstPage = useCallback(async () => {
    setRunsLoad({ status: "loading" });
    setRuns([]);
    setNextCursor(null);
    setHasMore(false);
    const result = await listRuns({
      pair: selectedPairName === ALL_PAIRS ? undefined : selectedPairName,
      status: selectedStatus === ALL_STATUSES ? undefined : (selectedStatus as RunRecordStatus),
    });
    if (!mountedRef.current) return;
    if (!result.ok) {
      setRunsLoad({ status: "error", message: toApiError(result.error).message });
      return;
    }
    setRuns(result.data.items);
    setHasMore(result.data.has_more);
    setNextCursor(result.data.next_cursor);
    setRunsLoad({ status: "loaded" });
  }, [selectedPairName, selectedStatus]);

  useEffect(() => {
    void fetchFirstPage();
  }, [fetchFirstPage]);

  async function loadMore() {
    if (!nextCursor) return;
    setLoadingMore(true);
    const result = await listRuns({
      pair: selectedPairName === ALL_PAIRS ? undefined : selectedPairName,
      status: selectedStatus === ALL_STATUSES ? undefined : (selectedStatus as RunRecordStatus),
      cursor: nextCursor,
    });
    if (!mountedRef.current) return;
    setLoadingMore(false);
    if (!result.ok) {
      setRunsLoad({ status: "error", message: toApiError(result.error).message });
      return;
    }
    setRuns((prev) => [...prev, ...result.data.items]);
    setHasMore(result.data.has_more);
    setNextCursor(result.data.next_cursor);
  }

  function openDetail(run: RunSummaryOut) {
    setDetailRunId(run.id);
    setDetailOpen(true);
  }

  const selectedPair = pairs.find((pair) => pair.name === selectedPairName) ?? null;

  const columns: ColumnDef<RunSummaryOut>[] = [
    { accessorKey: "pair", header: "Pair" },
    {
      id: "entity_type",
      header: "Entity type",
      cell: ({ row }) => <span>{ENTITY_TYPE_LABEL[row.original.entity_type]}</span>,
    },
    {
      id: "status",
      header: "Status",
      cell: ({ row }) => (
        <RunStatusBadge status={row.original.status} inProgress={row.original.in_progress} />
      ),
    },
    {
      id: "started_at",
      header: "Started",
      cell: ({ row }) => <span>{formatDateTime(row.original.started_at)}</span>,
    },
    {
      id: "duration",
      header: "Duration",
      cell: ({ row }) => <span>{formatDuration(row.original.duration_seconds)}</span>,
    },
    {
      id: "dry_run",
      header: "Kind",
      cell: ({ row }) => <span>{row.original.dry_run ? "Dry run" : "Real cycle"}</span>,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-4">
        <div className="flex flex-col gap-1">
          <label htmlFor="runs-pair-filter" className="text-caption font-medium text-foreground">
            Pair
          </label>
          <Select value={selectedPairName} onValueChange={setSelectedPairName}>
            <SelectTrigger id="runs-pair-filter" aria-label="Filter by sync pair" className="w-56">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_PAIRS}>All pairs</SelectItem>
              {pairs.map((pair) => (
                <SelectItem key={pair.id} value={pair.name}>
                  {pair.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="runs-status-filter" className="text-caption font-medium text-foreground">
            Status
          </label>
          <Select value={selectedStatus} onValueChange={setSelectedStatus}>
            <SelectTrigger id="runs-status-filter" aria-label="Filter by run status" className="w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_STATUSES}>All statuses</SelectItem>
              {STATUS_OPTIONS.map((option) => (
                <SelectItem key={option} value={option}>
                  {STATUS_OPTION_LABEL[option]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {pairsLoad.status === "error" ? (
        <StatePanel kind="error" title="Could not load sync pairs" description={pairsLoad.message} />
      ) : selectedPair ? (
        <RunControlsPanel pair={selectedPair} onRunCompleted={() => void fetchFirstPage()} />
      ) : pairsLoad.status === "loaded" && pairs.length > 0 ? (
        <p className="text-caption text-muted-foreground">
          Select a specific pair above to see its run-now, pause and resume controls.
        </p>
      ) : null}

      {runsLoad.status === "error" ? (
        <StatePanel
          kind="error"
          title="Could not load run history"
          description={runsLoad.message}
          actions={<Button onClick={() => void fetchFirstPage()}>Retry</Button>}
        />
      ) : runsLoad.status === "loaded" && runs.length === 0 ? (
        <StatePanel
          kind="empty"
          title="No runs recorded yet"
          description="Run history fills in as scheduled cycles or run-now fire for a pair."
        />
      ) : (
        <div className="flex flex-col gap-3">
          <ResultCount count={runs.length} loading={runsLoad.status === "loading"}>
            runs
          </ResultCount>
          <DataTable
            columns={columns}
            data={runs}
            loading={runsLoad.status === "loading"}
            caption="Run history, newest first"
            onRowClick={(row) => openDetail(row.original)}
            rowActionLabel={(row) =>
              `View run detail: ${row.original.pair}, ${ENTITY_TYPE_LABEL[row.original.entity_type]}, started ${formatDateTime(row.original.started_at)}`
            }
          />
          {hasMore ? (
            <Button variant="outline" disabled={loadingMore} onClick={() => void loadMore()}>
              {loadingMore ? "Loading…" : "Load more"}
            </Button>
          ) : null}
        </div>
      )}

      <RunDetailSheet runId={detailRunId} open={detailOpen} onOpenChange={setDetailOpen} />
    </div>
  );
}
