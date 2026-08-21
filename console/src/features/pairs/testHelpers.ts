// Shared fixtures + a tiny request router for this feature's tests. Not a `*.test.*` file
// itself, so vitest never collects it as a suite. Every fixture is built from the REAL shapes
// in `../../api/generated/schema.ts` (a fixture that drifts from the API is a compile error,
// not a silent divergence), and every test drives the real `apiClient` through a stubbed
// `globalThis.fetch` (`../../test/apiFixtures.ts`'s `installFetchMock`), never a mock of
// `apiClient` or this feature's own modules. Mirrors `../endpoints/testHelpers.ts` exactly.
import { jsonResponse } from "../../test/apiFixtures";
import type { components } from "../../api/generated/schema";

export type EndpointOut = components["schemas"]["EndpointOut"];
export type EndpointHealthOut = components["schemas"]["EndpointHealthOut"];
export type SyncPairOut = components["schemas"]["SyncPairOut"];
export type ErrorModel = components["schemas"]["ErrorModel"];

export function endpointOutFixture(overrides: Partial<EndpointOut> = {}): EndpointOut {
  return {
    name: "databricks_prod",
    connector: "databricks",
    role: "source",
    settings: {},
    secret_ref: "env:DATABRICKS_TOKEN",
    enabled: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

export function healthOutFixture(overrides: Partial<EndpointHealthOut> = {}): EndpointHealthOut {
  return {
    endpoint: "databricks_prod",
    state: "healthy",
    reason: null,
    checked_at: "2026-01-01T00:00:00Z",
    details: {},
    ...overrides,
  };
}

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
    enabled: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
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

/** Routes a stubbed `globalThis.fetch` by `"METHOD pathname"`, so `Promise.all`-issued
 * requests (this screen's own initial load: pairs + endpoints) don't need a brittle
 * call-index ordering assumption -- see `../endpoints/testHelpers.ts`'s doc comment for why
 * call-order queueing is the wrong default here. Logs every request's `"METHOD pathname"` to
 * `calls` so a test can assert a route was -- or was NOT -- called (the healthcheck "never
 * fired automatically" mutation check needs exactly this). An unmatched request throws, so a
 * stray call fails the test loudly instead of hanging forever. */
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
    // A `Response` body can only be read once -- clone a static fixture so the SAME route can
    // be read more than once without "Body is unusable: Body has already been read".
    return typeof route === "function" ? route(request) : route.clone();
  });
  return { calls };
}

export { jsonResponse };
