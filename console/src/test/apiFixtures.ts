// Small shared helpers for driving the REAL `apiClient` through a stubbed `globalThis.fetch`
// -- never a mock of our own modules. Every helper here returns/builds exactly the shapes
// `console/src/api/generated/schema.ts` declares, so a fixture that drifts from the real API
// is a compile error, not a silent divergence.
import { vi } from "vitest";

import type { components } from "../api/generated/schema";

export type SessionInfo = components["schemas"]["SessionInfo"];
export type ErrorModel = components["schemas"]["ErrorModel"];

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** A `SessionInfo` fixture, matching `auth.py`'s `SessionInfo` model exactly. */
export function sessionInfoFixture(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    username: "admin",
    csrf_token: "test-csrf-token",
    expires_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

/** An `ErrorModel` fixture matching `errors.py`'s shared shape. */
export function errorModelFixture(overrides: Partial<ErrorModel> = {}): ErrorModel {
  return {
    code: "invalid_credentials",
    message: "invalid username or password",
    field: null,
    entity: null,
    correlation_id: null,
    ...overrides,
  };
}

/** Installs a `vi.fn()` on `globalThis.fetch` and returns it, so a test can both drive the
 * real `apiClient` and assert on the real `Request` object that would have gone over the
 * wire (`fetchMock.mock.calls[n][0]`). Callers queue responses with
 * `fetchMock.mockResolvedValueOnce(...)` / `mockImplementation(...)`. */
export function installFetchMock(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

export function requestFromMock(fetchMock: ReturnType<typeof vi.fn>, callIndex = 0): Request {
  const request = (fetchMock.mock.calls[callIndex] as unknown[] | undefined)?.[0];
  if (!(request instanceof Request)) {
    throw new Error(`fetch mock call ${callIndex} did not receive a Request`);
  }
  return request;
}

/** A URL-routed stub for `globalThis.fetch`, keyed `"<METHOD> <pathname>"`.
 *
 * Prefer this over queueing `mockResolvedValueOnce` whenever the tree under test mounts more
 * than one component that fetches. Call-order queueing is only correct while exactly one
 * component fetches on mount, and it fails in a way that is genuinely hard to read: React
 * fires effects bottom-up, so a newly-added child screen's request reaches the mock BEFORE
 * its parent's does, silently consuming the response the parent was queued to receive. The
 * parent then sees `undefined` and surfaces an unhandled rejection from somewhere unrelated.
 * That is exactly what happened the moment a real screen replaced the inert placeholder at
 * the default route, and this helper exists so it cannot happen again -- a route answers by
 * URL, so it does not matter who asks first.
 *
 * An unrouted request throws by name rather than returning `undefined`, so a missing fixture
 * fails loudly at the call site instead of as a mystery rejection elsewhere. */
export function installApiRouter(
  routes: Record<string, Response | ((request: Request) => Response | Promise<Response>)>,
): { fetchMock: ReturnType<typeof vi.fn>; calls: string[] } {
  const fetchMock = installFetchMock();
  const calls: string[] = [];
  fetchMock.mockImplementation(async (request: Request) => {
    const url = new URL(request.url);
    const key = `${request.method} ${url.pathname}`;
    calls.push(key);
    const route = routes[key];
    if (route === undefined) {
      throw new Error(
        `Unrouted request in test: ${key}. Add it to installApiRouter's routes map.`,
      );
    }
    // A Response body reads once -- clone so one route can answer repeated calls.
    return typeof route === "function" ? route(request) : route.clone();
  });
  return { fetchMock, calls };
}

/** The calls the Runs screen makes on mount. Any test that renders the shell at `/runs`
 * needs these answered, whether or not it cares about runs. `/api/runs` IS keyset-paginated
 * (unlike `/api/endpoints`, a bare array, and unlike `/api/pairs/{id}/source-tree`, which is
 * offset-based) -- three different shapes in one API, so each is checked against
 * `openapi.json` rather than assumed from its neighbours. */
export function runsScreenRoutes(): Record<string, Response> {
  return {
    "GET /api/pairs": jsonResponse(200, []),
    "GET /api/runs": jsonResponse(200, { items: [], limit: 50, has_more: false, next_cursor: null }),
  };
}

/** The two calls the Endpoints screen makes on mount. Any test that renders the shell at the
 * default route needs these answered, whether or not it cares about endpoints. */
export function endpointsScreenRoutes(): Record<string, Response> {
  return {
    // Both are bare arrays -- verified against openapi.json, not assumed. `/api/endpoints`
    // is NOT a paginated envelope (unlike `/api/runs`, which is keyset-paginated).
    "GET /api/endpoints": jsonResponse(200, []),
    "GET /api/connectors": jsonResponse(200, []),
  };
}
