/* Sync pairs (T13.4) -- list, create, edit, disable and delete sync pairs: source and target
 * endpoint, target Qlik space, entity types, cadence, manual-edit policy and activation
 * opt-in.
 *
 * Two things this screen deliberately keeps visibly separate, per the task's own DoD:
 *
 *   - **Disabling a pair is not deleting it.** "Enabled" is an inline `Switch` in its own
 *     table column (an immediate `PATCH`, no confirmation, exactly like
 *     `../endpoints/EndpointsScreen.tsx`'s own enable/disable column) -- pausing the
 *     scheduler, not removing the configuration. Delete is a separate icon action behind a
 *     `ConfirmDialog` that spells out the cascade (`pairs.py`: deleting a pair cascades its
 *     selection rules and overrides).
 *   - **Activation (D7) is a one-surface control.** The `Switch` that flips
 *     `activation_opt_in` lives ONLY inside `PairFormSheet.tsx`, next to the sentence stating
 *     its consequence (making the resulting Qlik data product discoverable tenant-wide) --
 *     never duplicated as a second, unexplained toggle in this table. The list instead shows
 *     activation as a read-only `Badge`; changing it means opening the form, where the
 *     consequence is always in view next to the control that flips it.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Badge,
  Button,
  ConfirmDialog,
  IconButton,
  SectionHeader,
  StatePanel,
  Switch,
  TooltipProvider,
  toast,
} from "@elabs-ai/components-ui";
import { DataTable, type ColumnDef } from "@elabs-ai/components-data";
import { Pencil, Plus, Trash2 } from "lucide-react";

import {
  deletePair,
  listEndpoints,
  listPairs,
  updatePair,
  type EndpointOut,
  type SyncPairOut,
} from "./pairsApi";
import { PairFormSheet, type PairFormMode } from "./PairFormSheet";
import { ENTITY_TYPE_LABEL, MANUAL_MODE_LABEL } from "./labels";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "loaded" };

function formatCadence(pair: SyncPairOut): string {
  const jitter = pair.jitter_seconds != null ? ` ± ${pair.jitter_seconds}s` : "";
  return `${pair.cadence_seconds}s${jitter}`;
}

function manualEditPolicySummary(pair: SyncPairOut): string {
  const overrideCount = Object.keys(pair.manual_edit_policy.per_entity ?? {}).length;
  const base = MANUAL_MODE_LABEL[pair.manual_edit_policy.default];
  return overrideCount > 0 ? `${base} (+${overrideCount} override${overrideCount === 1 ? "" : "s"})` : base;
}

export function PairsScreen() {
  const [load, setLoad] = useState<LoadState>({ status: "loading" });
  const [pairs, setPairs] = useState<SyncPairOut[]>([]);
  const [endpoints, setEndpoints] = useState<EndpointOut[]>([]);
  const [enabledPending, setEnabledPending] = useState<Record<string, boolean>>({});

  const [formOpen, setFormOpen] = useState(false);
  const [formMode, setFormMode] = useState<PairFormMode>({ kind: "create" });

  const [deleteTarget, setDeleteTarget] = useState<SyncPairOut | null>(null);
  const [deleting, setDeleting] = useState(false);

  // Guards state updates against firing after unmount -- see
  // `../endpoints/EndpointsScreen.tsx`'s identical comment for why this matters for a screen
  // that fetches automatically on mount.
  const mountedRef = useRef(true);
  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  const fetchAll = useCallback(async () => {
    setLoad({ status: "loading" });
    const [pairsResult, endpointsResult] = await Promise.all([listPairs(), listEndpoints()]);
    if (!mountedRef.current) return;
    if (!pairsResult.ok) {
      setLoad({ status: "error", message: pairsResult.error.message });
      return;
    }
    if (!endpointsResult.ok) {
      setLoad({ status: "error", message: endpointsResult.error.message });
      return;
    }
    setPairs(pairsResult.data);
    setEndpoints(endpointsResult.data);
    setLoad({ status: "loaded" });
  }, []);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  function openCreate() {
    setFormMode({ kind: "create" });
    setFormOpen(true);
  }

  function openEdit(pair: SyncPairOut) {
    setFormMode({ kind: "edit", pair });
    setFormOpen(true);
  }

  function handleSaved(pair: SyncPairOut) {
    setPairs((prev) => {
      const exists = prev.some((row) => row.id === pair.id);
      return exists ? prev.map((row) => (row.id === pair.id ? pair : row)) : [...prev, pair];
    });
  }

  async function handleToggleEnabled(pair: SyncPairOut, next: boolean) {
    setEnabledPending((prev) => ({ ...prev, [pair.id]: true }));
    const result = await updatePair(pair.id, { enabled: next });
    setEnabledPending((prev) => ({ ...prev, [pair.id]: false }));
    if (result.ok) {
      setPairs((prev) => prev.map((row) => (row.id === pair.id ? result.data : row)));
    } else {
      toast.error(`Could not update "${pair.name}": ${result.error.message}`);
    }
  }

  async function handleDeleteConfirm() {
    if (!deleteTarget) return;
    setDeleting(true);
    const result = await deletePair(deleteTarget.id);
    setDeleting(false);
    if (result.ok) {
      setPairs((prev) => prev.filter((row) => row.id !== deleteTarget.id));
      toast.success(`Sync pair "${deleteTarget.name}" deleted.`);
      setDeleteTarget(null);
    } else {
      toast.error(`Could not delete "${deleteTarget.name}": ${result.error.message}`);
    }
  }

  const columns: ColumnDef<SyncPairOut>[] = [
    { accessorKey: "name", header: "Name" },
    { accessorKey: "source", header: "Source" },
    { accessorKey: "target", header: "Target" },
    { accessorKey: "target_space", header: "Target space" },
    {
      id: "entity_types",
      header: "Entity types",
      cell: ({ row }) => (
        <div className="flex flex-wrap items-center gap-1">
          {row.original.entity_types.length === 0 ? (
            <span className="text-caption text-muted-foreground">None selected</span>
          ) : (
            row.original.entity_types.map((type) => (
              <Badge key={type} variant="secondary">
                {ENTITY_TYPE_LABEL[type]}
              </Badge>
            ))
          )}
        </div>
      ),
    },
    {
      id: "cadence",
      header: "Cadence",
      cell: ({ row }) => <span>{formatCadence(row.original)}</span>,
    },
    {
      id: "manual_edit_policy",
      header: "Manual-edit policy",
      cell: ({ row }) => <span>{manualEditPolicySummary(row.original)}</span>,
    },
    {
      id: "activation",
      header: "Activation",
      cell: ({ row }) =>
        row.original.activation_opt_in ? (
          <Badge variant="success">Discoverable tenant-wide</Badge>
        ) : (
          <Badge variant="outline">Not activated</Badge>
        ),
    },
    {
      id: "enabled",
      header: "Enabled",
      cell: ({ row }) => (
        <Switch
          checked={row.original.enabled}
          disabled={!!enabledPending[row.original.id]}
          onCheckedChange={(next) => void handleToggleEnabled(row.original, next)}
          aria-label={`${row.original.enabled ? "Disable" : "Enable"} sync pair "${row.original.name}"`}
        />
      ),
    },
    {
      id: "actions",
      header: "Actions",
      cell: ({ row }) => (
        <div className="flex items-center gap-1">
          <IconButton
            label={`Edit sync pair "${row.original.name}"`}
            icon={<Pencil />}
            variant="ghost"
            onClick={() => openEdit(row.original)}
          />
          <IconButton
            label={`Delete sync pair "${row.original.name}"`}
            icon={<Trash2 />}
            variant="ghost"
            onClick={() => setDeleteTarget(row.original)}
          />
        </div>
      ),
    },
  ];

  return (
    <TooltipProvider>
      <div className="flex flex-col gap-6">
        <SectionHeader
          title="Sync pairs"
          description="A source and a target endpoint, the Qlik space they sync into, and what to sync. Activation is off by default and, once opted into, makes the resulting Qlik data product discoverable tenant-wide."
          actions={
            <Button onClick={openCreate}>
              <Plus aria-hidden className="mr-1.5 size-4" />
              New sync pair
            </Button>
          }
        />

        {load.status === "error" ? (
          <StatePanel
            kind="error"
            title="Could not load sync pairs"
            description={load.message}
            actions={<Button onClick={() => void fetchAll()}>Retry</Button>}
          />
        ) : load.status === "loaded" && pairs.length === 0 ? (
          <StatePanel
            kind="empty"
            title="No sync pairs configured yet"
            description="Create a pair to start syncing metadata from a source endpoint into Qlik."
            actions={<Button onClick={openCreate}>New sync pair</Button>}
          />
        ) : (
          <DataTable columns={columns} data={pairs} loading={load.status === "loading"} caption="Configured sync pairs" />
        )}

        <PairFormSheet open={formOpen} onOpenChange={setFormOpen} mode={formMode} endpoints={endpoints} onSaved={handleSaved} />

        <ConfirmDialog
          open={deleteTarget !== null}
          onOpenChange={(next) => {
            if (!next) setDeleteTarget(null);
          }}
          title={`Delete sync pair "${deleteTarget?.name}"?`}
          description="This cannot be undone. Its selection rules and per-object overrides are deleted with it. This does not delete anything the engine already wrote to Qlik."
          confirmLabel="Delete sync pair"
          tone="destructive"
          loading={deleting}
          onConfirm={() => void handleDeleteConfirm()}
        />
      </div>
    </TooltipProvider>
  );
}
