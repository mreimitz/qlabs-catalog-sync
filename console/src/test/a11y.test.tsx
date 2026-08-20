// The accessibility gate (`pnpm a11y`). Runs axe-core directly against the rendered
// application shell and fails on any violation -- not vitest-axe (unmaintained at 0.1.0, a
// compatibility risk against Vitest 4; see the T13.1 report). This is deliberately its own
// script/file, separate from the general `pnpm test` suite, so CI (T13.8) can gate on it
// independently.
//
// T13.2 replaced the T13.1 scaffold's placeholder shell with the real one (sign-in, nav,
// breadcrumb, theme switcher, sign-out) -- this file now covers BOTH states an operator
// actually sees: signed-out (the sign-in screen) and signed-in (the navigation shell). A
// sign-in form and a navigation shell are exactly where keyboard and label defects appear,
// per the task brief; `SignInScreen.a11y.test.tsx` covers the sign-in screen's own states in
// more depth, this file's job is the composed, real `App`.
import { render, screen, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import axe from "axe-core";
import { beforeEach, describe, expect, it } from "vitest";

import { installFetchMock, jsonResponse, sessionInfoFixture } from "./apiFixtures";
import { setSignedOut } from "../auth/sessionStore";
import App from "../App";

function renderApp() {
  return render(
    <ThemeProvider defaultTheme="light">
      <App />
    </ThemeProvider>,
  );
}

describe("accessibility", () => {
  beforeEach(() => {
    setSignedOut();
    window.history.pushState({}, "", "/");
  });

  it("renders the sign-in screen with no axe violations", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { code: "unauthenticated", message: "sign in" }));
    const { container } = renderApp();

    await screen.findByRole("button", { name: "Sign in" });
    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("renders the signed-in navigation shell with no axe violations", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, sessionInfoFixture({ username: "admin" })));
    const { container } = renderApp();

    await waitFor(() => expect(screen.getAllByRole("link", { name: /Endpoints/ })).toHaveLength(1));
    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });
});
