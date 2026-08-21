// The configuration change log (`GET /api/config-changes`) -- answers "when did this schema
// start syncing" (the RM-06 decision's own Consequences section, quoted in `history.py`'s
// module docstring). Reachable from the Runs screen per `app-spec.md`: "reachable from here
// or from the endpoint/pair screens it documents" -- this feature owns the routes, so it is
// the "here".
//
// Same keyset-pagination contract and the same "no numbered pager" rule as
// `RunHistoryTab.tsx` -- see that module's doc comment for the reasoning, which applies here
// unchanged (`ConfigChangeListPage` is `{items, limit, has_more, next_cursor}`, structurally
// identical to `RunListPage`).
//
// One raw `config_changes` row per changed field (`history.py`'s own module docstring:
// "Recording every changed field as its own row... A single blob-diff row would still say
// 'something about this pair changed on this date' but never let a caller ask about one field
// in isolation"). This screen renders that shape as-is -- one table row per field-change --
// rather than synthesizing a grouped "operation" view the API deliberately does not offer.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Badge,
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

import { listConfigChanges, type ChangeEntityKind, type ConfigChangeOut } from "./runsApi";
import { formatDateTime } from "./format";
import { toApiError } from "../../api/client";

const ALL_KINDS = "__all__";

const ENTITY_KIND_OPTIONS: readonly ChangeEntityKind[] = [
  "endpoint",
  "sync_pair",
  "selection_rule",
  "selection_override",
];
const ENTITY_KIND_LABEL: Record<ChangeEntityKind, string> = {
  endpoint: "Endpoint",
  sync_pair: "Sync pair",
  selection_rule: "Selection rule",
  selection_override: "Selection override",
};

const ACTION_VARIANT: Record<ConfigChangeOut["action"], "success" | "secondary" | "destructive"> = {
  create: "success",
  update: "secondary",
  delete: "destructive",
};

type LoadState = { status: "loading" } | { status: "error"; message: string } | { status: "loaded" };

export function ConfigChangesTab() {
  const [entityKind, setEntityKind] = useState<string>(ALL_KINDS);
  const [changes, setChanges] = useState<ConfigChangeOut[]>([]);
  const [load, setLoad] = useState<LoadState>({ status: "loading" });
  const [hasMore, setHasMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const mountedRef = useRef(true);
  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  const fetchFirstPage = useCallback(async () => {
    setLoad({ status: "loading" });
    setChanges([]);
    setNextCursor(null);
    setHasMore(false);
    const result = await listConfigChanges({
      entity_kind: entityKind === ALL_KINDS ? undefined : (entityKind as ChangeEntityKind),
    });
    if (!mountedRef.current) return;
    if (!result.ok) {
      setLoad({ status: "error", message: toApiError(result.error).message });
      return;
    }
    setChanges(result.data.items);
    setHasMore(result.data.has_more);
    setNextCursor(result.data.next_cursor);
    setLoad({ status: "loaded" });
  }, [entityKind]);

  useEffect(() => {
    void fetchFirstPage();
  }, [fetchFirstPage]);

  async function loadMore() {
    if (!nextCursor) return;
    setLoadingMore(true);
    const result = await listConfigChanges({
      entity_kind: entityKind === ALL_KINDS ? undefined : (entityKind as ChangeEntityKind),
      cursor: nextCursor,
    });
    if (!mountedRef.current) return;
    setLoadingMore(false);
    if (!result.ok) {
      setLoad({ status: "error", message: toApiError(result.error).message });
      return;
    }
    setChanges((prev) => [...prev, ...result.data.items]);
    setHasMore(result.data.has_more);
    setNextCursor(result.data.next_cursor);
  }

  const columns: ColumnDef<ConfigChangeOut>[] = [
    {
      id: "entity_kind",
      header: "Entity",
      cell: ({ row }) => <span>{ENTITY_KIND_LABEL[row.original.entity_kind]}</span>,
    },
    { accessorKey: "entity_id", header: "Entity id" },
    {
      id: "action",
      header: "Action",
      cell: ({ row }) => (
        <Badge variant={ACTION_VARIANT[row.original.action]}>{row.original.action}</Badge>
      ),
    },
    {
      id: "field",
      header: "Field",
      cell: ({ row }) => <span>{row.original.field ?? "(whole entity)"}</span>,
    },
    {
      id: "changed_at",
      header: "Changed at",
      cell: ({ row }) => <span>{formatDateTime(row.original.changed_at)}</span>,
    },
    { accessorKey: "actor", header: "Actor" },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <label htmlFor="config-changes-kind-filter" className="text-caption font-medium text-foreground">
          Entity kind
        </label>
        <Select value={entityKind} onValueChange={setEntityKind}>
          <SelectTrigger id="config-changes-kind-filter" aria-label="Filter by entity kind" className="w-56">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL_KINDS}>All entity kinds</SelectItem>
            {ENTITY_KIND_OPTIONS.map((option) => (
              <SelectItem key={option} value={option}>
                {ENTITY_KIND_LABEL[option]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {load.status === "error" ? (
        <StatePanel
          kind="error"
          title="Could not load the configuration change log"
          description={load.message}
          actions={<Button onClick={() => void fetchFirstPage()}>Retry</Button>}
        />
      ) : load.status === "loaded" && changes.length === 0 ? (
        <StatePanel
          kind="empty"
          title="No configuration changes recorded yet"
          description="Every create, edit and delete of an endpoint, sync pair, selection rule or override is logged here, one row per changed field."
        />
      ) : (
        <div className="flex flex-col gap-3">
          <ResultCount count={changes.length} loading={load.status === "loading"}>
            changes
          </ResultCount>
          <DataTable
            columns={columns}
            data={changes}
            loading={load.status === "loading"}
            caption="Configuration change log, newest first"
          />
          {hasMore ? (
            <Button variant="outline" disabled={loadingMore} onClick={() => void loadMore()}>
              {loadingMore ? "Loading…" : "Load more"}
            </Button>
          ) : null}
        </div>
      )}
    </div>
  );
}
