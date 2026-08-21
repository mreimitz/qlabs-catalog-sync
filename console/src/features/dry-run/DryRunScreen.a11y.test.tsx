// Named *.a11y.test.tsx beside the screen it covers (console/CLAUDE.md's load-bearing naming
// convention -- `pnpm a11y` runs `vitest run a11y`, a path-substring filter, so an
// accessibility test named anything else is silently not gated).
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes as RouterRoutes } from "react-router-dom";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import axe from "axe-core";
import { beforeAll, describe, expect, it } from "vitest";

import { Shell } from "../../app/Shell";
import { installFetchMock } from "../../test/apiFixtures";
import { DryRunScreen } from "./DryRunScreen";
import {
  PAIR_ID,
  countsFixture,
  droppedFieldFixture,
  installApiRouter,
  jsonResponse,
  orphanReportFixture,
  recordReportFixture,
  runReportsOutFixture,
  syncPairOutFixture,
  syncRunReportFixture,
  withheldFieldFixture,
  type Routes,
} from "./testHelpers";

beforeAll(() => {
  if (!("hasPointerCapture" in Element.prototype)) {
    Object.defineProperty(Element.prototype, "hasPointerCapture", { value: () => false, configurable: true });
  }
  if (!("releasePointerCapture" in Element.prototype)) {
    Object.defineProperty(Element.prototype, "releasePointerCapture", { value: () => {}, configurable: true });
  }
  if (!("setPointerCapture" in Element.prototype)) {
    Object.defineProperty(Element.prototype, "setPointerCapture", { value: () => {}, configurable: true });
  }
  if (!("scrollIntoView" in Element.prototype)) {
    Object.defineProperty(Element.prototype, "scrollIntoView", { value: () => {}, configurable: true });
  }
});

/** A full plan: creates, updates (with dropped/withheld/target_skipped_fields), no-ops, other
 * outcomes (skipped/filtered/orphaned/failed) and a D2+D3 hit -- so the axe pass covers every
 * structural shape this screen renders, not just the happy path. */
function fullPlanRoutes(overrides: Routes = {}): Routes {
  return {
    "GET /api/pairs": jsonResponse(200, [syncPairOutFixture()]),
    [`POST /api/pairs/${PAIR_ID}/dry-run`]: jsonResponse(
      200,
      runReportsOutFixture({
        runs: [
          syncRunReportFixture({
            entity_type: "data_product",
            counts: countsFixture({ read: 5, created: 1, written: 1, unchanged: 1, skipped: 1, orphaned: 1, failed: 1 }),
            records: [
              recordReportFixture({ native_key: "dp-new", display_name: "analytics.new_schema", outcome: "created" }),
              recordReportFixture({
                native_key: "dp-updated",
                display_name: "analytics.sales",
                outcome: "written",
                changed_fields: ["description", "owners"],
                written_fields: ["description"],
                dropped: [droppedFieldFixture()],
                withheld: [withheldFieldFixture()],
                target_skipped_fields: ["owners"],
                detail: "1 of 2 owner(s) did not match a Qlik user (decision D3)",
              }),
              recordReportFixture({ native_key: "dp-same", display_name: "finance.reporting", outcome: "unchanged" }),
              recordReportFixture({ native_key: "dp-skip", display_name: "analytics.staging", outcome: "skipped", reason: "not_selected" }),
              recordReportFixture({
                native_key: "dp-gone",
                display_name: "analytics.legacy",
                outcome: "orphaned",
                detail: "gone at the source; recorded as an orphan and never deleted at the target",
              }),
              recordReportFixture({ native_key: "dp-fail", display_name: "analytics.broken", outcome: "failed" }),
            ],
            orphans: [orphanReportFixture({ native_key: "dp-gone" })],
          }),
          syncRunReportFixture({
            entity_type: "dataset",
            status: "failed",
            counts: countsFixture(),
            records: [],
            errors: [
              {
                kind: "AuthError",
                message: "the source endpoint rejected the credential",
                endpoint: "databricks_prod",
                native_key: null,
                operation: "list_changed",
                retryable: false,
                fatal: true,
              },
            ],
          }),
        ],
      }),
    ),
    ...overrides,
  };
}

async function runDryRun(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("combobox", { name: /sync pair/i }));
  await user.click(await screen.findByRole("option", { name: /prod_databricks_to_qlik/ }));
  await user.click(await screen.findByRole("button", { name: /run dry run/i }));
  await waitFor(() => {
    expect(screen.getByText(/this is a plan only/i)).toBeInTheDocument();
  });
}

describe("DryRunScreen accessibility", () => {
  it("has no axe violations with a full plan on screen (creates, updates, no-ops, other outcomes, D2/D3, a failed entity type)", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, fullPlanRoutes());

    const { container } = render(
      <ThemeProvider defaultTheme="light">
        <DryRunScreen />
      </ThemeProvider>,
    );
    await runDryRun(user);

    // Sanity: every shape really is on screen before axe runs over it.
    expect(screen.getByText("analytics.new_schema")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Unresolved references" })).toBeInTheDocument();
    expect(screen.getAllByText(/could not produce a plan/i).length).toBeGreaterThan(0);

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("has no axe violations in the idle state (no pair selected yet)", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, fullPlanRoutes());

    const { container } = render(
      <ThemeProvider defaultTheme="light">
        <DryRunScreen />
      </ThemeProvider>,
    );
    await screen.findByRole("combobox", { name: /sync pair/i });

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("renders inside Shell at /dry-run with no axe violations", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      ...fullPlanRoutes(),
      "GET /api/auth/session": jsonResponse(200, {
        username: "admin",
        csrf_token: "test-csrf-token",
        expires_at: "2026-01-01T00:00:00Z",
      }),
    });

    const { container } = render(
      <ThemeProvider defaultTheme="light">
        <MemoryRouter initialEntries={["/dry-run"]}>
          <RouterRoutes>
            <Route element={<Shell />}>
              <Route path="/dry-run" element={<DryRunScreen />} />
            </Route>
          </RouterRoutes>
        </MemoryRouter>
      </ThemeProvider>,
    );

    await runDryRun(user);

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });
});
