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

import { installFetchMock, jsonResponse, sessionInfoFixture } from "../test/apiFixtures";
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
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, sessionInfoFixture({ username: "admin" })));
    renderApp();

    // Confirm we are genuinely signed in, on the Runs screen, before the expiry.
    await screen.findByRole("button", { name: /Sign out/ });
    expect(screen.getByText(/This screen has not been built yet\. T13\.7 builds it\./)).toBeInTheDocument();
    expect(window.location.pathname).toBe("/runs");

    // An arbitrary call from somewhere else in the app -- a screen's own data fetch, not the
    // session endpoints -- comes back 401. This is what a mid-session expiry, revocation or a
    // service restart looks like from the browser's side (auth.py's module doc).
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { code: "unauthenticated", message: "sign in" }));
    await apiClient.GET("/api/endpoints");

    await screen.findByRole("button", { name: "Sign in" });
    // The URL never moved -- AuthGate swapped what's rendered, it did not navigate.
    expect(window.location.pathname).toBe("/runs");
  });

  it("signing back in returns the operator to the route they were on, not the home route", async () => {
    window.history.pushState({}, "", "/runs");
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { code: "unauthenticated", message: "sign in" }));
    renderApp();

    const user = userEvent.setup();
    await user.type(await screen.findByRole("textbox", { name: "Username" }), "admin");
    await user.type(screen.getByLabelText("Password"), "the-real-password");
    fetchMock.mockResolvedValueOnce(jsonResponse(200, sessionInfoFixture({ username: "admin" })));
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await screen.findByRole("button", { name: /Sign out/ });
    // Still Runs -- NOT the default/home (Endpoints) route.
    expect(window.location.pathname).toBe("/runs");
    expect(screen.getByText(/This screen has not been built yet\. T13\.7 builds it\./)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText(/This screen has not been built yet\. T13\.3 builds it\./)).not.toBeInTheDocument(),
    );
  });
});
