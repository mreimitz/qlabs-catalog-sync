/* Endpoints (T13.3) -- the first screens an operator actually uses. List registered endpoints
 * with health and secret-reference status; register a new one against a discovered connector;
 * edit, healthcheck, enable/disable, delete; view a configured endpoint's capability manifest.
 *
 * Two things this screen deliberately does NOT do automatically on load, both because the
 * routes behind them run real I/O against a tenant (see `endpointsApi.ts`'s doc comments):
 *
 *   - It does not run a healthcheck for every row. `HealthBadge` shows "Not checked yet" until
 *     an operator clicks the row's "Run healthcheck" action, and a red result renders as a
 *     calm status badge -- never a toast, because the request that produced it succeeded; a red
 *     healthcheck is a normal fact about the endpoint, not an error in the request that asked
 *     (`routes/endpoints.py`'s module docstring). An ordinary request failure while RUNNING the
 *     healthcheck (a broken connector lookup, a network error to this API itself) is a
 *     different thing and DOES toast -- see `handleHealthcheck` below.
 *   - It does not open the capability-manifest sheet or fetch a manifest for every row. That
 *     is `EndpointManifestSheet.tsx`'s job, fired only when an operator opens it for one row.
 *
 * What it DOES do automatically on load: resolve every row's secret reference
 * (`GET .../secret-resolve`), because that is cheap, local/backend-only work, never tenant I/O
 * (`endpointsApi.ts`), and the task's own DoD requires an unresolvable reference to be visible
 * at a glance -- not behind a click.
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
import { FileText, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";

import {
  deleteEndpoint,
  listConnectors,
  listEndpoints,
  resolveEndpointSecret,
  runEndpointHealthcheck,
  updateEndpoint,
  type ConnectorInfo,
  type EndpointHealthOut,
  type EndpointOut,
  type SecretResolveOut,
} from "./endpointsApi";
import { EndpointFormSheet, type EndpointFormMode } from "./EndpointFormSheet";
import { EndpointManifestSheet } from "./EndpointManifestSheet";
import { HealthBadge, SecretResolveBadge } from "./StatusBadges";
import { toApiError } from "../../api/client";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "loaded" };

export function EndpointsScreen() {
  const [load, setLoad] = useState<LoadState>({ status: "loading" });
  const [endpoints, setEndpoints] = useState<EndpointOut[]>([]);
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [secretStatus, setSecretStatus] = useState<Record<string, SecretResolveOut | "loading">>({});
  const [health, setHealth] = useState<Record<string, EndpointHealthOut>>({});
  const [healthChecking, setHealthChecking] = useState<Record<string, boolean>>({});
  const [enabledPending, setEnabledPending] = useState<Record<string, boolean>>({});

  const [formOpen, setFormOpen] = useState(false);
  const [formMode, setFormMode] = useState<EndpointFormMode>({ kind: "create" });

  const [manifestOpen, setManifestOpen] = useState(false);
  const [manifestName, setManifestName] = useState<string | null>(null);

  const [deleteTarget, setDeleteTarget] = useState<EndpointOut | null>(null);
  const [deleting, setDeleting] = useState(false);

  // Guards every state update below against firing after this screen has unmounted -- most
  // importantly the per-row secret-resolve loop, which is still chaining `.then()` calls well
  // after the initial `Promise.all` settles. React does not cancel an in-flight promise chain on
  // unmount by itself, and this screen fetches automatically on mount (no click required), so
  // without this guard a navigation away mid-load would keep calling `setState` on an unmounted
  // component -- and, in a test that mounts/unmounts this screen repeatedly against a fresh
  // per-test `fetch` mock, keep issuing real requests against whatever `fetch` happens to be
  // installed by the time each trailing continuation runs, well after the test that started
  // them believes it is done.
  const mountedRef = useRef(true);
  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  const fetchAll = useCallback(async () => {
    setLoad({ status: "loading" });
    const [endpointsResult, connectorsResult] = await Promise.all([listEndpoints(), listConnectors()]);
    if (!mountedRef.current) return;
    if (!endpointsResult.ok) {
      setLoad({ status: "error", message: endpointsResult.error.message });
      return;
    }
    if (!connectorsResult.ok) {
      setLoad({ status: "error", message: connectorsResult.error.message });
      return;
    }
    setEndpoints(endpointsResult.data);
    setConnectors(connectorsResult.data);
    setLoad({ status: "loaded" });

    // Per-row secret-resolve, in parallel, for every row this load produced -- see the module
    // doc comment for why this (and only this) per-row call runs automatically.
    setSecretStatus(
      Object.fromEntries(endpointsResult.data.map((endpoint) => [endpoint.name, "loading" as const])),
    );
    for (const endpoint of endpointsResult.data) {
      void resolveEndpointSecret(endpoint.name).then((result) => {
        if (!mountedRef.current) return;
        if (result.ok) {
          setSecretStatus((prev) => ({ ...prev, [endpoint.name]: result.data }));
        }
      });
    }
  }, []);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  function openRegister() {
    setFormMode({ kind: "create" });
    setFormOpen(true);
  }

  function openEdit(endpoint: EndpointOut) {
    setFormMode({ kind: "edit", endpoint });
    setFormOpen(true);
  }

  function handleSaved(endpoint: EndpointOut) {
    setEndpoints((prev) => {
      const exists = prev.some((row) => row.name === endpoint.name);
      return exists ? prev.map((row) => (row.name === endpoint.name ? endpoint : row)) : [...prev, endpoint];
    });
    setSecretStatus((prev) => ({ ...prev, [endpoint.name]: "loading" }));
    void resolveEndpointSecret(endpoint.name).then((result) => {
      if (result.ok) {
        setSecretStatus((prev) => ({ ...prev, [endpoint.name]: result.data }));
      }
    });
  }

  function openManifest(name: string) {
    setManifestName(name);
    setManifestOpen(true);
  }

  async function handleHealthcheck(name: string) {
    setHealthChecking((prev) => ({ ...prev, [name]: true }));
    const result = await runEndpointHealthcheck(name);
    setHealthChecking((prev) => ({ ...prev, [name]: false }));
    if (result.ok) {
      // A red result here is a normal, successfully-rendered fact about the endpoint -- never
      // a toast (see the module doc comment). The row's own badge is the feedback.
      setHealth((prev) => ({ ...prev, [name]: result.data }));
    } else {
      // This IS an ordinary request failure (e.g. the endpoint names a connector that is
      // broken or no longer registered) -- distinct from a red-but-successful healthcheck, and
      // toasting it is correct (`routes/endpoints.py`'s module docstring draws this same line).
      toast.error(`Could not run healthcheck for "${name}": ${toApiError(result.error).message}`);
    }
  }

  async function handleToggleEnabled(endpoint: EndpointOut, next: boolean) {
    setEnabledPending((prev) => ({ ...prev, [endpoint.name]: true }));
    const result = await updateEndpoint(endpoint.name, { enabled: next });
    setEnabledPending((prev) => ({ ...prev, [endpoint.name]: false }));
    if (result.ok) {
      setEndpoints((prev) => prev.map((row) => (row.name === endpoint.name ? result.data : row)));
    } else {
      toast.error(`Could not update "${endpoint.name}": ${toApiError(result.error).message}`);
    }
  }

  async function handleDeleteConfirm() {
    if (!deleteTarget) return;
    setDeleting(true);
    const result = await deleteEndpoint(deleteTarget.name);
    setDeleting(false);
    if (result.ok) {
      setEndpoints((prev) => prev.filter((row) => row.name !== deleteTarget.name));
      toast.success(`Endpoint "${deleteTarget.name}" deleted.`);
      setDeleteTarget(null);
    } else {
      toast.error(`Could not delete "${deleteTarget.name}": ${toApiError(result.error).message}`);
    }
  }

  const connectorsByName = Object.fromEntries(connectors.map((entry) => [entry.name, entry]));

  const columns: ColumnDef<EndpointOut>[] = [
    { accessorKey: "name", header: "Name" },
    {
      accessorKey: "connector",
      header: "Connector",
      cell: ({ row }) => {
        const info = connectorsByName[row.original.connector];
        return (
          <div className="flex items-center gap-2">
            <span>{row.original.connector}</span>
            {info && !info.available ? <Badge variant="destructive">unavailable</Badge> : null}
          </div>
        );
      },
    },
    {
      accessorKey: "role",
      header: "Role",
      cell: ({ row }) => <Badge variant="secondary">{row.original.role}</Badge>,
    },
    {
      id: "enabled",
      header: "Enabled",
      cell: ({ row }) => (
        <Switch
          checked={row.original.enabled}
          disabled={!!enabledPending[row.original.name]}
          onCheckedChange={(next) => void handleToggleEnabled(row.original, next)}
          aria-label={`${row.original.enabled ? "Disable" : "Enable"} endpoint "${row.original.name}"`}
        />
      ),
    },
    {
      id: "secret",
      header: "Secret reference",
      cell: ({ row }) => <SecretResolveBadge status={secretStatus[row.original.name]} />,
    },
    {
      id: "health",
      header: "Health",
      cell: ({ row }) => <HealthBadge health={health[row.original.name]} />,
    },
    {
      id: "actions",
      header: "Actions",
      cell: ({ row }) => (
        <div className="flex items-center gap-1">
          <IconButton
            label={`Run healthcheck for "${row.original.name}"`}
            icon={<RefreshCw className={healthChecking[row.original.name] ? "animate-spin" : undefined} />}
            variant="ghost"
            disabled={!!healthChecking[row.original.name]}
            onClick={() => void handleHealthcheck(row.original.name)}
          />
          <IconButton
            label={`View capability manifest for "${row.original.name}"`}
            icon={<FileText />}
            variant="ghost"
            onClick={() => openManifest(row.original.name)}
          />
          <IconButton
            label={`Edit endpoint "${row.original.name}"`}
            icon={<Pencil />}
            variant="ghost"
            onClick={() => openEdit(row.original)}
          />
          <IconButton
            label={`Delete endpoint "${row.original.name}"`}
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
          title="Endpoints"
          description="Registered instances of the connectors this image discovered. An endpoint pairs a connector with settings and a secret reference -- never a stored credential."
          actions={
            <Button onClick={openRegister}>
              <Plus aria-hidden className="mr-1.5 size-4" />
              Register endpoint
            </Button>
          }
        />

        {load.status === "error" ? (
          <StatePanel
            kind="error"
            title="Could not load endpoints"
            description={load.message}
            actions={<Button onClick={() => void fetchAll()}>Retry</Button>}
          />
        ) : load.status === "loaded" && endpoints.length === 0 ? (
          <StatePanel
            kind="empty"
            title="No endpoints registered yet"
            description="Register an instance of a discovered connector to get started."
            actions={<Button onClick={openRegister}>Register endpoint</Button>}
          />
        ) : (
          <DataTable
            columns={columns}
            data={endpoints}
            loading={load.status === "loading"}
            caption="Registered endpoints"
          />
        )}

        <EndpointFormSheet
          open={formOpen}
          onOpenChange={setFormOpen}
          mode={formMode}
          connectors={connectors}
          onSaved={handleSaved}
        />

        <EndpointManifestSheet endpointName={manifestName} open={manifestOpen} onOpenChange={setManifestOpen} />

        <ConfirmDialog
          open={deleteTarget !== null}
          onOpenChange={(next) => {
            if (!next) setDeleteTarget(null);
          }}
          title={`Delete endpoint "${deleteTarget?.name}"?`}
          description="This cannot be undone. If a sync pair still uses this endpoint as its source or target, deletion is refused until that pair is deleted or repointed."
          confirmLabel="Delete endpoint"
          tone="destructive"
          loading={deleting}
          onConfirm={() => void handleDeleteConfirm()}
        />
      </div>
    </TooltipProvider>
  );
}
