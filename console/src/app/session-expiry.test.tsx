// The DoD's session-expiry item, driven end to end through the real `App`: "an expired
// session returns the operator to sign-in without losing the route they were on, and returns
// them there after signing back in. A 401 from any API call -- not just the session check --
// must trigger this." Both halves are tested here because they are one mechanism
// (`AuthGate` never navigates -- see its doc comment) and a regression in either direction
// (losing the route on expiry, or not restoring it on sign-back-in) is the same class of bug.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { beforeEach, describe, expect, it } from "vitest";

import {
  installApiRouter,
  jsonResponse,
  runsScreenRoutes,
  sessionInfoFixture,
} from "../test/apiFixtures";
import { setSignedOut } from "../auth/sessionStore";
import { apiClient } from "../api/client";
import App from "../App";

function renderApp() {
  return render(
    <ThemeProvider defaultTheme="light">
      <App />
    </ThemeProvider>,
  );
}

describe("session expiry", () => {
  beforeEach(() => {
    setSignedOut();
  });

  it("a 401 from any API call (not just the session check) returns to sign-in WITHOUT changing the route", async () => {
    window.history.pushState({}, "", "/runs");
    // Routed by URL, not queued in call order: `/runs` mounts a real screen that fetches on
    // mount, and React fires effects bottom-up, so the child's requests reach the stub before
    // AuthGate's own session check does. See `installApiRouter`'s doc comment.
    installApiRouter({
      "GET /api/auth/session": jsonResponse(200, sessionInfoFixture({ username: "admin" })),
      ...runsScreenRoutes(),
      // Only this test calls it -- the deliberate mid-session expiry below.
      "GET /api/endpoints": jsonResponse(401, { code: "unauthenticated", message: "sign in" }),
    });
    renderApp();

    // Confirm we are genuinely signed in, on the Runs screen, before the expiry.
    await screen.findByRole("button", { name: /Sign out/ });
    expect(screen.getByRole("heading", { name: "Runs" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/runs");

    // An arbitrary call from somewhere else in the app -- a screen's own data fetch, not the
    // session endpoints -- comes back 401. This is what a mid-session expiry, revocation or a
    // service restart looks like from the browser's side (auth.py's module doc).
    await apiClient.GET("/api/endpoints");

    await screen.findByRole("button", { name: "Sign in" });
    // The URL never moved -- AuthGate swapped what's rendered, it did not navigate.
    expect(window.location.pathname).toBe("/runs");
  });

  it("signing back in returns the operator to the route they were on, not the home route", async () => {
    window.history.pushState({}, "", "/runs");
    installApiRouter({
      // Boot: no session. Sign-in: a POST, a different route key -- so this stays correct no
      // matter what order the screen's own fetches interleave in.
      "GET /api/auth/session": jsonResponse(401, { code: "unauthenticated", message: "sign in" }),
      "POST /api/auth/session": jsonResponse(200, sessionInfoFixture({ username: "admin" })),
      ...runsScreenRoutes(),
    });
    renderApp();

    const user = userEvent.setup();
    await user.type(await screen.findByRole("textbox", { name: "Username" }), "admin");
    await user.type(screen.getByLabelText("Password"), "the-real-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await screen.findByRole("button", { name: /Sign out/ });
    // Still Runs -- NOT the default/home (Endpoints) route.
    expect(window.location.pathname).toBe("/runs");
    expect(screen.getByRole("heading", { name: "Runs" })).toBeInTheDocument();
    // ...and definitely not the default (Endpoints) screen. Asserted by its heading rather
    // than by the placeholder copy this once looked for -- that placeholder stopped existing
    // the moment T13.3 landed, which made the assertion vacuously true.
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Endpoints" })).not.toBeInTheDocument(),
    );
  });
});
