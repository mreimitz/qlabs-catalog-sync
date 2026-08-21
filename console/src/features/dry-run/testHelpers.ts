// Shared fixtures + this feature's request router. Not a `*.test.*` file itself, so vitest
// never collects it as a suite. Mirrors `../selection/testHelpers.ts` exactly, and for the same
// reasons:
//
//  * every fixture is built from the REAL shapes in `../../api/generated/schema.ts`, so a
//    fixture that drifts from the API is a compile error rather than a silent divergence;
//  * every test drives the REAL `apiClient` through a stubbed `globalThis.fetch`, never a mock
//    of `apiClient` or of this feature's own modules, so what a test asserts is what would go
//    over the wire;
//  * the router answers by `"METHOD pathname"`, and records the full request, so a test can
//    assert on the exact body that would go over the wire (`entity_types`, `create_missing`).
import { jsonResponse } from "../../test/apiFixtures";
import type { components } from "../../api/generated/schema";

export type SyncPairOut = components["schemas"]["SyncPairOut"];
export type EntityType = components["schemas"]["EntityType"];
export type RecordOutcome = components["schemas"]["RecordOutcome"];
export type RecordReportOut = components["schemas"]["RecordReportOut"];
export type DroppedFieldOut = components["schemas"]["DroppedFieldOut"];
export type WithheldFieldOut = components["schemas"]["WithheldFieldOut"];
export type OrphanReportOut = components["schemas"]["OrphanReportOut"];
export type ErrorReportOut = components["schemas"]["ErrorReportOut"];
export type WatermarkOut = components["schemas"]["WatermarkOut"];
export type RunCountsOut =
  components["schemas"]["qlabs_catalog_sync__api__routes__run_control__RunCountsOut"];
export type SyncRunReportOut = components["schemas"]["SyncRunReportOut"];
export type RunReportsOut = components["schemas"]["RunReportsOut"];
export type ErrorModel = components["schemas"]["ErrorModel"];

export const PAIR_ID = "22222222-2222-2222-2222-222222222222";

export function syncPairOutFixture(overrides: Partial<SyncPairOut> = {}): SyncPairOut {
  return {
    id: PAIR_ID,
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

export function droppedFieldFixture(overrides: Partial<DroppedFieldOut> = {}): DroppedFieldOut {
  return {
    field: "documentation",
    reason: "not_applicable",
    capability_mode: "na",
    native_path: null,
    ...overrides,
  };
}

export function withheldFieldFixture(overrides: Partial<WithheldFieldOut> = {}): WithheldFieldOut {
  return {
    field: "status",
    reason: "activation_not_opted_in",
    ...overrides,
  };
}

export function watermarkFixture(overrides: Partial<WatermarkOut> = {}): WatermarkOut {
  return {
    before: null,
    after: "2026-08-20T12:00:00Z",
    advanced: true,
    held_by: [],
    has_more: false,
    pages: 1,
    ...overrides,
  };
}

export function countsFixture(overrides: Partial<RunCountsOut> = {}): RunCountsOut {
  return {
    read: 0,
    created: 0,
    written: 0,
    unchanged: 0,
    no_op: 0,
    skipped: 0,
    orphaned: 0,
    filtered: 0,
    failed: 0,
    ...overrides,
  };
}

export function recordReportFixture(overrides: Partial<RecordReportOut> = {}): RecordReportOut {
  return {
    native_key: "table-id-1",
    entity_type: "data_product",
    outcome: "unchanged",
    neutral_id: "33333333-3333-3333-3333-333333333333",
    display_name: "analytics.sales",
    target_native_key: null,
    reason: null,
    detail: null,
    was_read: true,
    changed_fields: [],
    written_fields: [],
    dropped: [],
    withheld: [],
    target_skipped_fields: [],
    holds_watermark: false,
    ...overrides,
  };
}

export function orphanReportFixture(overrides: Partial<OrphanReportOut> = {}): OrphanReportOut {
  return {
    neutral_id: "44444444-4444-4444-4444-444444444444",
    entity_type: "data_product",
    endpoint: "databricks_prod",
    native_key: "schema-id-gone",
    observed_at: "2026-08-20T12:00:00Z",
    ...overrides,
  };
}

export function errorReportFixture(overrides: Partial<ErrorReportOut> = {}): ErrorReportOut {
  return {
    kind: "TransientError",
    message: "the source endpoint timed out mid-page",
    endpoint: "databricks_prod",
    native_key: null,
    operation: "list_changed",
    retryable: true,
    fatal: true,
    ...overrides,
  };
}

export function syncRunReportFixture(
  overrides: Partial<SyncRunReportOut> = {},
): SyncRunReportOut {
  return {
    pair: "prod_databricks_to_qlik",
    source_endpoint: "databricks_prod",
    target_endpoint: "qlik_prod",
    entity_type: "data_product",
    status: "ok",
    dry_run: true,
    committed: false,
    create_enabled: false,
    started_at: "2026-08-20T12:00:00Z",
    finished_at: "2026-08-20T12:00:05Z",
    duration_seconds: 5,
    watermark: watermarkFixture(),
    counts: countsFixture(),
    records: [],
    orphans: [],
    errors: [],
    quarantined_endpoints: [],
    ...overrides,
  };
}

export function runReportsOutFixture(overrides: Partial<RunReportsOut> = {}): RunReportsOut {
  return {
    pair_id: PAIR_ID,
    pair_name: "prod_databricks_to_qlik",
    generated_at: "2026-08-20T12:00:10Z",
    runs: [syncRunReportFixture()],
    ...overrides,
  };
}

export function errorModelFixture(overrides: Partial<ErrorModel> = {}): ErrorModel {
  return {
    code: "endpoint_setup_failed",
    message: "endpoint 'databricks_prod' could not be reached",
    field: null,
    entity: "databricks_prod",
    correlation_id: null,
    ...overrides,
  };
}

export type RouteResponder = (request: Request) => Response | Promise<Response>;
export type Routes = Record<string, RouteResponder | Response>;

/** Routes a stubbed `globalThis.fetch` by `"METHOD pathname"`. Records the full request (not
 * just its URL), so a test can assert on the JSON body a dry-run POST actually carried
 * (`entity_types`, `create_missing`) -- the request that would go over the wire, per this
 * task's own instructions, not an assumption about it.
 *
 * An unrouted request throws by name rather than returning `undefined`, so a missing fixture
 * fails loudly at the call site instead of as a mystery rejection elsewhere. */
export function installApiRouter(
  fetchMock: ReturnType<typeof import("vitest").vi.fn>,
  routes: Routes,
): { calls: string[]; requests: Request[] } {
  const calls: string[] = [];
  const requests: Request[] = [];
  fetchMock.mockImplementation(async (request: Request) => {
    const url = new URL(request.url);
    const key = `${request.method} ${url.pathname}`;
    calls.push(key);
    requests.push(request);
    const route = routes[key];
    if (route === undefined) {
      throw new Error(`Unrouted request in test: ${key}. Add it to installApiRouter.`);
    }
    // A `Response` body reads once -- clone a static fixture so one route can answer repeats.
    return typeof route === "function" ? route(request) : route.clone();
  });
  return { calls, requests };
}

/** The JSON body a recorded request would actually have sent. */
export async function bodyOf(request: Request): Promise<unknown> {
  return JSON.parse(await request.clone().text()) as unknown;
}

/** The last recorded request matching `"METHOD pathname"`. */
export function lastRequestTo(requests: readonly Request[], key: string): Request {
  for (let index = requests.length - 1; index >= 0; index -= 1) {
    const request = requests[index];
    if (request === undefined) continue;
    const url = new URL(request.url);
    if (`${request.method} ${url.pathname}` === key) return request;
  }
  throw new Error(`No request matching ${key}. Saw: ${requests.map((r) => r.url).join(", ")}`);
}

export { jsonResponse };
