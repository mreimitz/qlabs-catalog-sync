// Shared fixtures + a tiny request router for this feature's tests. Not a `*.test.*` file
// itself, so vitest never collects it as a suite. Every fixture is built from the REAL shapes
// in `../../api/generated/schema.ts` (a fixture that drifts from the API is a compile error,
// not a silent divergence -- the task brief's own instruction), and every test drives the real
// `apiClient` through a stubbed `globalThis.fetch` (`../../test/apiFixtures.ts`'s
// `installFetchMock`), never a mock of `apiClient` or this feature's own modules.
import { jsonResponse } from "../../test/apiFixtures";
import type { components } from "../../api/generated/schema";

export type ConnectorInfo = components["schemas"]["ConnectorInfo"];
export type EndpointOut = components["schemas"]["EndpointOut"];
export type EndpointHealthOut = components["schemas"]["EndpointHealthOut"];
export type EndpointManifestOut = components["schemas"]["EndpointManifestOut"];
export type SecretResolveOut = components["schemas"]["SecretResolveOut"];
export type CapabilityManifestOut = components["schemas"]["CapabilityManifestOut"];
export type ErrorModel = components["schemas"]["ErrorModel"];

export function manifestFixture(overrides: Partial<CapabilityManifestOut> = {}): CapabilityManifestOut {
  return {
    concurrency: "etag",
    entities: {
      data_product: {
        supported: true,
        identity_keys: ["qualified_name"],
        supports_events: false,
        allowed_update_paths: null,
        max_update_operations: null,
        fields: {
          name: { mode: "rw", writable_via: "name", partial_update: true, normalized_by_target: false },
          physical_ref: { mode: "ro", writable_via: null, partial_update: false, normalized_by_target: false },
        },
      },
    },
    ...overrides,
  };
}

export function connectorInfoFixture(overrides: Partial<ConnectorInfo> = {}): ConnectorInfo {
  return {
    name: "fake",
    available: true,
    manifest: manifestFixture(),
    manifest_unavailable_reason: null,
    // The connector's own settings schema, secrets stripped server-side (C2). Always present
    // on a real response -- config_secret_fields is a non-optional array, never undefined.
    config_schema: null,
    config_secret_fields: [],
    distribution: null,
    broken_stage: null,
    broken_reason: null,
    ...overrides,
  };
}

export function endpointOutFixture(overrides: Partial<EndpointOut> = {}): EndpointOut {
  return {
    name: "qlik_prod",
    connector: "fake",
    role: "target",
    settings: {},
    secret_ref: "env:QLIK_ACME",
    enabled: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

export function healthOutFixture(overrides: Partial<EndpointHealthOut> = {}): EndpointHealthOut {
  return {
    endpoint: "qlik_prod",
    state: "healthy",
    reason: null,
    checked_at: "2026-01-01T00:00:00Z",
    details: {},
    ...overrides,
  };
}

export function secretResolveFixture(overrides: Partial<SecretResolveOut> = {}): SecretResolveOut {
  return {
    resolvable: true,
    reason: "resolved from the environment backend",
    ...overrides,
  };
}

export function endpointManifestOutFixture(overrides: Partial<EndpointManifestOut> = {}): EndpointManifestOut {
  return {
    endpoint: "qlik_prod",
    manifest: manifestFixture(),
    unavailable_reason: null,
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

/** Routes a stubbed `globalThis.fetch` by `"METHOD pathname"` (e.g. `"GET /api/endpoints"`),
 * so `Promise.all`-issued requests (this screen's own initial load) don't need a brittle
 * call-index ordering assumption. Logs every request's `"METHOD pathname"` to `calls` so a
 * test can assert a route was -- or, just as importantly, was NOT -- called (the manifest and
 * healthcheck "never fired automatically" mutation checks need exactly this). An unmatched
 * request throws, so a stray call fails the test loudly instead of hanging forever. */
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
    // A `Response` body can only be read once -- clone a static fixture so the SAME route
    // (e.g. re-fetched on retry, or re-installed with an unchanged unrelated entry between two
    // `installApiRouter` calls in one test) can be read more than once without "Body is
    // unusable: Body has already been read".
    return typeof route === "function" ? route(request) : route.clone();
  });
  return { calls };
}

export { jsonResponse };
