// Shared fixtures + a tiny request router for this feature's tests. Every fixture is built
// from the REAL shapes in `../../api/generated/schema.ts` (a fixture that drifts from the API
// is a compile error, not a silent divergence), and every test drives the real `apiClient`
// through a stubbed `globalThis.fetch` (`../../test/apiFixtures.ts`'s `installFetchMock`),
// never a mock of `apiClient` or this feature's own modules. Mirrors
// `../pairs/testHelpers.ts` and `../endpoints/testHelpers.ts` exactly.
import { jsonResponse } from "../../test/apiFixtures";
import type {
  ConfigChangeOut,
  RunControlStatusOut,
  RunCountsOut,
  RunDetailOut,
  RunErrorOut,
  RunIssuesOut,
  RunItemOut,
  RunOrphanIssueOut,
  RunSummaryOut,
  SyncPairOut,
} from "./runsApi";
import type { components } from "../../api/generated/schema";

export type ErrorModel = components["schemas"]["ErrorModel"];

export function syncPairOutFixture(overrides: Partial<SyncPairOut> = {}): SyncPairOut {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    name: "prod_databricks_to_qlik",
    source: "databricks_prod",
    target: "qlik_prod",
    target_space: "analytics/finance",
    entity_types: ["data_product", "dataset"],
    cadence_seconds: 900,
    jitter_seconds: null,
    manual_edit_policy: { default: "source_wins" },
    activation_opt_in: false,
    enabled: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

export function runCountsOutFixture(overrides: Partial<RunCountsOut> = {}): RunCountsOut {
  return {
    read: 0,
    created: 0,
    written: 0,
    write: 0,
    unchanged: 0,
    no_op: 0,
    skipped: 0,
    orphaned: 0,
    filtered: 0,
    failed: 0,
    error: 0,
    ...overrides,
  };
}

export function runSummaryOutFixture(overrides: Partial<RunSummaryOut> = {}): RunSummaryOut {
  return {
    id: "22222222-2222-2222-2222-222222222222",
    pair: "prod_databricks_to_qlik",
    source_endpoint: "databricks_prod",
    target_endpoint: "qlik_prod",
    entity_type: "data_product",
    status: "ok",
    in_progress: false,
    dry_run: false,
    committed: true,
    create_enabled: false,
    watermark_before: null,
    watermark_after: "2026-01-01T00:05:00Z",
    watermark_advanced: true,
    has_more: false,
    pages: 1,
    started_at: "2026-01-01T00:00:00Z",
    finished_at: "2026-01-01T00:05:00Z",
    duration_seconds: 300,
    counts: runCountsOutFixture({ read: 4, created: 1, written: 1, write: 2, unchanged: 2 }),
    quarantined_endpoints: [],
    ...overrides,
  };
}

export function runDetailOutFixture(overrides: Partial<RunDetailOut> = {}): RunDetailOut {
  const { items, errors, swept_stale, ...summaryOverrides } = overrides;
  const summary = runSummaryOutFixture(summaryOverrides);
  return {
    ...summary,
    items: items ?? [],
    errors: errors ?? [],
    swept_stale: swept_stale ?? false,
  };
}

export function runItemOutFixture(overrides: Partial<RunItemOut> = {}): RunItemOut {
  return {
    id: "33333333-3333-3333-3333-333333333333",
    run_id: "22222222-2222-2222-2222-222222222222",
    native_key: "sales.orders",
    neutral_id: "44444444-4444-4444-4444-444444444444",
    display_name: "Orders",
    target_native_key: null,
    outcome: "skipped",
    reason: "unresolved_owner",
    detail: "owner email has no matching Qlik user",
    endpoint: "databricks_prod",
    held_watermark: true,
    unresolved_fields: ["owners"],
    ...overrides,
  };
}

export function runOrphanIssueOutFixture(
  overrides: Partial<RunOrphanIssueOut> = {},
): RunOrphanIssueOut {
  return {
    run_item_id: "55555555-5555-5555-5555-555555555555",
    neutral_id: "66666666-6666-6666-6666-666666666666",
    endpoint: "databricks_prod",
    native_key: "sales.legacy_orders",
    display_name: "Legacy Orders",
    detail: "no longer present in the source catalog",
    orphan_log_found: true,
    first_missing_at: "2026-01-01T00:00:00Z",
    last_missing_at: "2026-01-02T00:00:00Z",
    last_seen_at: "2025-12-31T00:00:00Z",
    resolved_at: null,
    still_open: true,
    ...overrides,
  };
}

export function runErrorOutFixture(overrides: Partial<RunErrorOut> = {}): RunErrorOut {
  return {
    id: "77777777-7777-7777-7777-777777777777",
    run_id: "22222222-2222-2222-2222-222222222222",
    kind: "connector_error",
    message: "connection refused",
    endpoint: "databricks_prod",
    native_key: null,
    operation: "read",
    retryable: true,
    fatal: false,
    is_stale_sweep: false,
    ...overrides,
  };
}

export function runIssuesOutFixture(overrides: Partial<RunIssuesOut> = {}): RunIssuesOut {
  return {
    run_id: "22222222-2222-2222-2222-222222222222",
    status: "ok",
    in_progress: false,
    issues_recorded: true,
    swept_stale: false,
    has_issues: false,
    unresolved_dataset_members: [],
    unresolvable_owners: [],
    orphans: [],
    other_outstanding: [],
    errors: [],
    ...overrides,
  };
}

export function runControlStatusOutFixture(
  overrides: Partial<RunControlStatusOut> = {},
): RunControlStatusOut {
  return {
    pair_id: "11111111-1111-1111-1111-111111111111",
    pair_name: "prod_databricks_to_qlik",
    enabled: true,
    paused: false,
    running: false,
    ...overrides,
  };
}

export function configChangeOutFixture(overrides: Partial<ConfigChangeOut> = {}): ConfigChangeOut {
  return {
    id: "88888888-8888-8888-8888-888888888888",
    entity_kind: "sync_pair",
    entity_id: "prod_databricks_to_qlik",
    action: "update",
    field: "enabled",
    old_value: false,
    new_value: true,
    actor: "admin",
    changed_at: "2026-01-01T00:00:00Z",
    generation: 1,
    ...overrides,
  };
}

export function errorModelFixture(overrides: Partial<ErrorModel> = {}): ErrorModel {
  return {
    code: "config_service_error",
    message: "something went wrong",
    field: null,
    entity: null,
    correlation_id: null,
    ...overrides,
  };
}

export type RouteResponder = (request: Request) => Response | Promise<Response>;
export type Routes = Record<string, RouteResponder | Response>;

/** Routes a stubbed `globalThis.fetch` by `"METHOD pathname"` -- never call-order queueing,
 * see `../pairs/testHelpers.ts`'s own doc comment for why that is the wrong default here. An
 * unmatched request throws, so a stray call fails the test loudly. */
export function installApiRouter(
  fetchMock: ReturnType<typeof import("vitest").vi.fn>,
  routes: Routes,
): { calls: string[] } {
  const calls: string[] = [];
  fetchMock.mockImplementation(async (request: Request) => {
    const url = new URL(request.url);
    const key = `${request.method} ${url.pathname}`;
    calls.push(key);
    const route = routes[key];
    if (route === undefined) {
      throw new Error(`Unhandled request in test: ${key}`);
    }
    return typeof route === "function" ? route(request) : route.clone();
  });
  return { calls };
}

export { jsonResponse };
