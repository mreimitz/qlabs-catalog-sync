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
