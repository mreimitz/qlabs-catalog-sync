// The DoD's "the theme switcher persists a choice across reloads" item, driven through the
// REAL shell's ThemeSwitcher and the REAL ThemeProvider (`@elabs-ai/components-tokens`) --
// not a mock of either. Persistence is the library's own job (`ThemeProvider`'s
// `storageKey`, default `"brand-ui-theme"`, confirmed via `brand-ui docs ThemeProvider`); this
// test exists so a future change that breaks the wiring between them (e.g. swapping in a
// *controlled* `preference` prop without actually persisting it, or dropping `ThemeSwitcher`
// from the shell) fails a named test instead of shipping silently.
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { beforeEach, describe, expect, it } from "vitest";

import { endpointsScreenRoutes, installApiRouter, jsonResponse, sessionInfoFixture } from "../test/apiFixtures";
import { setSignedOut } from "../auth/sessionStore";
import App from "../App";

const THEME_STORAGE_KEY = "brand-ui-theme";

function renderApp() {
  return render(
    <ThemeProvider defaultTheme="light">
      <App />
    </ThemeProvider>,
  );
}

async function signInAndWaitForShell() {
  // Reset the module-level session store first. It is deliberately module-level (one signed-in
  // operator per page), so it survives `cleanup()` -- and the second call below is modelling a
  // page RELOAD, which in a real browser starts with that store empty. Without this reset the
  // second mount skips the boot gate entirely and the two halves of this test stop being
  // comparable.
  setSignedOut();
  installApiRouter({
    "GET /api/auth/session": jsonResponse(200, sessionInfoFixture({ username: "admin" })),
    ...endpointsScreenRoutes(),
  });
  renderApp();
  await screen.findByRole("button", { name: /Sign out/ });
}

describe("theme switcher persistence", () => {
  beforeEach(() => {
    setSignedOut();
    window.history.pushState({}, "", "/");
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
  });

  it("persists an explicit Dark choice to localStorage and re-applies it on the next mount", async () => {
    await signInAndWaitForShell();
    const user = userEvent.setup();

    // With exactly 2 registry themes (light/dark) the shipped switcher (verified via its own
    // source: "auto" picks dropdown only for >2 themes) renders as a single toggle button
    // that CYCLES light -> dark -> system -> light on each activation, labelling itself with
    // the current theme and what one more click does. Starting from the explicit "light"
    // default, one click lands on "dark".
    const trigger = screen.getByRole("button", { name: /^Theme: Light\. Activate to switch to Dark\.$/ });
    await user.click(trigger);

    await waitFor(() => expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");

    // Simulate a reload: unmount everything and mount a completely fresh provider tree, which
    // is the same thing a hard page refresh does -- component state is gone, only
    // localStorage survives.
    cleanup();
    await signInAndWaitForShell();

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});
