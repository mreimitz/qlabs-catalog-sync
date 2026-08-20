/* QLabs Catalog Sync Console — scaffolded by `brand-ui scaffold` from ./app-spec.md.
 *
 * Archetype: data-app · theme: light.
 * Seed: docs/playbooks/templates/data-app.tsx in the brand-ui repo (itself generated from
 * that archetype's Storybook story). This is YOUR code now — edit freely.
 *
 * `TODO(spec):` marks everything the app-spec did not answer. Sample data is
 * placeholder — replace it, don't ship it.
 */
import { useState } from "react";
import {
  Badge,
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
} from "@elabs-ai/components-ui";
import { AppIcon } from "@elabs-ai/components-icons";
import {
  ColumnPicker,
  DataTable,
  FilterBar,
  SearchInput,
  type ColumnDef,
} from "@elabs-ai/components-data";

type Status = "active" | "paused" | "error";

interface DataRow {
  id: string;
  name: string;
  status: Status;
  records: number;
  lastRun: string;
}

const statusVariant: Record<Status, "success" | "secondary" | "destructive"> = {
  active: "success",
  paused: "secondary",
  error: "destructive",
};

const columns: ColumnDef<DataRow>[] = [
  { accessorKey: "name", header: "Name" },
  {
    accessorKey: "status",
    header: "Status",
    cell: ({ row }) => (
      <Badge variant={statusVariant[row.original.status]}>{row.original.status}</Badge>
    ),
  },
  {
    accessorKey: "records",
    header: "Records",
    cell: ({ row }) => (
      <span className="tabular-nums">{row.original.records.toLocaleString()}</span>
    ),
  },
  { accessorKey: "lastRun", header: "Last run" },
];

const sampleData: DataRow[] = [
  { id: "1", name: "Customer import", status: "active", records: 14200, lastRun: "2 min ago" },
  { id: "2", name: "Nightly sync", status: "active", records: 88541, lastRun: "6 h ago" },
  { id: "3", name: "Legacy migration", status: "paused", records: 3010, lastRun: "3 d ago" },
  { id: "4", name: "Partner feed", status: "error", records: 0, lastRun: "1 d ago" },
];

const nav = [
  { id: "data", label: "Data" },
  { id: "tables", label: "Tables" },
  { id: "home", label: "Home" },
  { id: "settings", label: "Settings" },
];

function DataAppTemplate({ loading = false }: { loading?: boolean }) {
  const [active, setActive] = useState("data");
  const [search, setSearch] = useState("");
  return (
    <SidebarProvider>
      <Sidebar collapsible="offcanvas">
        <SidebarHeader className="px-3 py-2">
          <div className="flex items-center gap-2">
            <AppIcon height={20} aria-hidden />
            <span className="truncate font-semibold group-data-[collapsible=icon]:hidden">
              Data
            </span>
          </div>
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupContent>
              <SidebarMenu>
                {nav.map((n) => (
                  <SidebarMenuItem key={n.id}>
                    <SidebarMenuButton
                      isActive={active === n.id}
                      tooltip={n.label}
                      onClick={() => setActive(n.id)}
                    >
                      <span>{n.label}</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
      </Sidebar>
      <SidebarInset>
        <header className="flex h-14 items-center gap-2 border-b px-4">
          <SidebarTrigger />
          <h1 className="text-body font-medium capitalize">{active}</h1>
        </header>
        {/* NOT a second `<main>` (brand-ui issue 386): `SidebarInset` already renders the
            page's `<main>` landmark, so nesting one here produced three axe
            violations at once — `landmark-main-is-top-level`,
            `landmark-no-duplicate-main` and `landmark-unique`. The content
            region inside the inset is a plain `<div>`. */}
        <div className="p-6">
          <DataTable
            columns={columns}
            data={loading ? [] : sampleData}
            loading={loading}
            enablePagination
            // Controlled global filter — the app owns `search` and passes it down.
            // Never mutate the filter inside `toolbar` (e.g. table.setGlobalFilter),
            // which sets state during render and loops ("Too many re-renders").
            globalFilter={search}
            onGlobalFilterChange={setSearch}
            toolbar={(table) => (
              // The toolbar has no fetch state of its own (D5) — it just
              // reflects the table's `loading` by disabling its controls
              // (loading-states.md; brand-ui issue 269).
              <FilterBar actions={<ColumnPicker table={table} disabled={loading} />}>
                <SearchInput value={search} onValueChange={setSearch} disabled={loading} />
              </FilterBar>
            )}
          />
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}

// LOADING — the toolbar's controls (`ColumnPicker`, `SearchInput`) are
// disabled while `DataTable loading` renders its skeleton rows (brand-ui issue 269,
// loading-states.md). The toolbar has no fetch state of its own (D5); it
// just reflects the table's `loading` prop.

export default DataAppTemplate;
