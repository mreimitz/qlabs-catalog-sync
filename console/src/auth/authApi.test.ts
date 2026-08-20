import { beforeEach, describe, expect, it } from "vitest";

import { errorModelFixture, installFetchMock, jsonResponse, sessionInfoFixture } from "../test/apiFixtures";
import { getSessionState, setSignedOut } from "./sessionStore";
import { loadSession, signIn, signOut } from "./authApi";

describe("loadSession (the boot sequence)", () => {
  beforeEach(() => {
    setSignedOut();
  });

  it("signs in from an existing cookie when GET /api/auth/session returns 200", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, sessionInfoFixture({ username: "admin" })));

    await loadSession();

    const state = getSessionState();
    expect(state.status).toBe("signed-in");
    if (state.status === "signed-in") {
      expect(state.username).toBe("admin");
      expect(state.csrfToken).toBe("test-csrf-token");
    }
  });

  it("shows signed-out when GET /api/auth/session returns 401 (no cookie)", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(401, errorModelFixture({ code: "unauthenticated" })));

    await loadSession();

    expect(getSessionState().status).toBe("signed-out");
  });
});

describe("signIn", () => {
  beforeEach(() => {
    setSignedOut();
  });

  it("posts the credentials and signs in on success", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, sessionInfoFixture({ username: "admin" })));

    const result = await signIn("admin", "hunter22-hunter22");

    expect(result.ok).toBe(true);
    expect(getSessionState().status).toBe("signed-in");
    const [request] = fetchMock.mock.calls[0] as [Request];
    expect(request.method).toBe("POST");
    const parsed = JSON.parse(await request.clone().text()) as { username: string; password: string };
    expect(parsed).toEqual({ username: "admin", password: "hunter22-hunter22" });
  });

  it("returns the ONE typed error for a wrong username or password, without inventing a finer message", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        401,
        errorModelFixture({ code: "invalid_credentials", message: "invalid username or password" }),
      ),
    );

    const result = await signIn("admin", "wrong-password-entirely");

    expect(result.ok).toBe(false);
    expect(result.error?.code).toBe("invalid_credentials");
    expect(result.error?.message).toBe("invalid username or password");
    expect(getSessionState().status).toBe("signed-out");
  });
});

describe("signOut", () => {
  it("clears the local session even if the server call itself fails", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockRejectedValueOnce(new Error("network down"));

    await signOut();

    expect(getSessionState().status).toBe("signed-out");
  });
});
