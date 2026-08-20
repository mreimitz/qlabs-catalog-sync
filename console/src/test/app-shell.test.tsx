// A genuine smoke test for the REAL application shell (T13.2), not the T13.1 scaffold's
// placeholder data table it used to cover -- see the T13.2 report for why this file, rather
// than a screen-specific test, is what's under test here: it drives the real `App` through a
// stubbed `fetch` at the network boundary (the boot sequence's `GET /api/auth/session`), the
// same discipline every other new test in this task follows.
import { render, screen, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { beforeEach, describe, expect, it } from "vitest";

import {
  endpointsScreenRoutes,
  installApiRouter,
  installFetchMock,
  jsonResponse,
  sessionInfoFixture,
} from "./apiFixtures";
import { setSignedOut } from "../auth/sessionStore";
import App from "../App";

function renderApp() {
  return render(
    <ThemeProvider defaultTheme="light">
      <App />
    </ThemeProvider>,
  );
}

describe("application shell", () => {
  beforeEach(() => {
    setSignedOut();
    window.history.pushState({}, "", "/");
  });

  it("renders the sign-in screen first when there is no session", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { code: "unauthenticated", message: "sign in" }));
    renderApp();

    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("renders the real nav, breadcrumb and sign-out control once signed in", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, sessionInfoFixture({ username: "admin" })));
    renderApp();

    // The five real WP13 destinations -- not the T13.1 scaffold's "Data"/"Tables"/"Home"/
    // "Settings" placeholders.
    for (const label of ["Endpoints", "Sync pairs", "Selection", "Dry run", "Runs"]) {
      expect(await screen.findByRole("link", { name: new RegExp(label) })).toBeInTheDocument();
    }

    expect(screen.getByRole("button", { name: /Sign out/ })).toBeInTheDocument();
    expect(screen.getByText("admin")).toBeInTheDocument();

    // Exactly one <main> landmark (brand-ui issue 386, carried over from the T13.1 scaffold's
    // own note): SidebarInset renders it, the content region beneath it is a plain <div>.
    await waitFor(() => expect(screen.getAllByRole("main")).toHaveLength(1));
  });

  it("redirects the bare '/' path to the default (Endpoints) screen", async () => {
    // Routed by URL rather than queued in call order: the default route now mounts a REAL
    // screen that fetches on mount, and React fires effects bottom-up, so the child's request
    // reaches the stub before AuthGate's own does. See `installApiRouter`'s doc comment.
    installApiRouter({
      "GET /api/auth/session": jsonResponse(200, sessionInfoFixture()),
      ...endpointsScreenRoutes(),
    });
    renderApp();

    await screen.findByRole("button", { name: /Sign out/ });
    expect(window.location.pathname).toBe("/endpoints");
    // The real Endpoints screen (T13.3), not the placeholder this once asserted. Its section
    // heading is what proves the route resolved to the screen rather than to a 404 or a shell
    // with an empty content region.
    expect(await screen.findByRole("heading", { name: "Endpoints" })).toBeInTheDocument();
  });
});
