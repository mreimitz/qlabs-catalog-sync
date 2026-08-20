// Drives the REAL `apiClient` through a stubbed `globalThis.fetch` and asserts on the actual
// `Request` object that would go over the wire -- method, path, headers, credentials -- per
// the task's own warning: asserting that code called a mock of itself proves nothing.
import { beforeEach, describe, expect, it } from "vitest";

import { installFetchMock, jsonResponse, requestFromMock, sessionInfoFixture } from "../test/apiFixtures";
import { getSessionState, setSignedIn, setSignedOut } from "../auth/sessionStore";
import { apiClient, isApiError, toApiError } from "./client";

describe("apiClient", () => {
  beforeEach(() => {
    setSignedOut();
  });

  it("sends credentials: same-origin on every request", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { code: "unauthenticated", message: "sign in" }));

    await apiClient.GET("/api/auth/session");

    const request = requestFromMock(fetchMock);
    expect(request.credentials).toBe("same-origin");
  });

  it("resolves relative paths against the page's own origin, never an absolute/configured URL", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { code: "unauthenticated", message: "sign in" }));

    await apiClient.GET("/api/auth/session");

    const request = requestFromMock(fetchMock);
    expect(request.url).toBe(`${window.location.origin}/api/auth/session`);
  });

  it("attaches X-CSRF-Token to a mutating request when a session is live", async () => {
    setSignedIn({ username: "admin", csrfToken: "the-csrf-token", expiresAt: "2026-01-01T00:00:00Z" });
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));

    await apiClient.DELETE("/api/auth/session");

    const request = requestFromMock(fetchMock);
    expect(request.headers.get("X-CSRF-Token")).toBe("the-csrf-token");
  });

  it("does NOT attach X-CSRF-Token to a safe (GET) request", async () => {
    setSignedIn({ username: "admin", csrfToken: "the-csrf-token", expiresAt: "2026-01-01T00:00:00Z" });
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, sessionInfoFixture()));

    await apiClient.GET("/api/auth/session");

    const request = requestFromMock(fetchMock);
    expect(request.headers.has("X-CSRF-Token")).toBe(false);
  });

  it("reads the CSRF token fresh at request time -- not a constant captured once", async () => {
    setSignedIn({ username: "admin", csrfToken: "first-token", expiresAt: "2026-01-01T00:00:00Z" });
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    await apiClient.DELETE("/api/auth/session");
    expect(requestFromMock(fetchMock, 0).headers.get("X-CSRF-Token")).toBe("first-token");

    // A second sign-in (e.g. after the first session ended) issues a DIFFERENT token. A
    // client that captured the token once at construction/module-load time would keep
    // sending "first-token" here; reading the session store at request time sends the new one.
    setSignedIn({ username: "admin", csrfToken: "second-token", expiresAt: "2026-01-01T08:00:00Z" });
    await apiClient.DELETE("/api/auth/session");
    expect(requestFromMock(fetchMock, 1).headers.get("X-CSRF-Token")).toBe("second-token");
  });

  it("sends no X-CSRF-Token on a mutating request while signed out (no token to send)", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { code: "unauthenticated", message: "sign in" }));

    await apiClient.DELETE("/api/auth/session");

    const request = requestFromMock(fetchMock);
    expect(request.headers.has("X-CSRF-Token")).toBe(false);
  });

  it("clears the session store on a 401 from ANY call, not just the auth routes", async () => {
    setSignedIn({ username: "admin", csrfToken: "tok", expiresAt: "2026-01-01T00:00:00Z" });
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(
      jsonResponse(401, { code: "unauthenticated", message: "sign in to use this API" }),
    );

    // An arbitrary, unrelated endpoint -- the point is that the reaction is generic, not
    // specific to the session endpoints.
    await apiClient.GET("/api/endpoints");

    expect(getSessionState().status).toBe("signed-out");
  });

  it("leaves the session store alone on a non-401 failure", async () => {
    setSignedIn({ username: "admin", csrfToken: "tok", expiresAt: "2026-01-01T00:00:00Z" });
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(422, { code: "request_validation_error", message: "bad" }));

    await apiClient.GET("/api/endpoints");

    expect(getSessionState().status).toBe("signed-in");
  });
});

describe("toApiError / isApiError", () => {
  it("recognizes a well-formed ErrorModel", () => {
    const error = { code: "endpoint_not_found", message: "no such endpoint", entity: "acme" };
    expect(isApiError(error)).toBe(true);
    expect(toApiError(error)).toEqual(error);
  });

  it("falls back to a safe shape for a value that isn't an ErrorModel", () => {
    expect(isApiError(undefined)).toBe(false);
    expect(isApiError("boom")).toBe(false);
    const fallback = toApiError(undefined);
    expect(fallback.code).toBe("unknown_error");
    expect(typeof fallback.message).toBe("string");
  });
});
