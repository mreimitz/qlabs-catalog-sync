// Named `*.a11y.test.tsx` beside the screen it covers (`console/CLAUDE.md`'s load-bearing
// naming convention -- `pnpm a11y` runs `vitest run a11y`, a path-substring filter). Covers
// the states the task brief calls out as exactly where label/name/role/live-region defects
// appear: a history table with status badges, a run-detail sheet with tabs and a live-status
// control panel whose badges update while an operator watches, and a keyset "Load more"
// control.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import axe from "axe-core";
import { beforeAll, describe, expect, it } from "vitest";

import { installFetchMock } from "../../test/apiFixtures";
import { RunsScreen } from "./RunsScreen";
import {
  configChangeOutFixture,
  installApiRouter,
  jsonResponse,
  runControlStatusOutFixture,
  runDetailOutFixture,
  runIssuesOutFixture,
  runItemOutFixture,
  runOrphanIssueOutFixture,
  runSummaryOutFixture,
  syncPairOutFixture,
} from "./testHelpers";

beforeAll(() => {
  if (!("hasPointerCapture" in Element.prototype)) {
    Object.defineProperty(Element.prototype, "hasPointerCapture", { value: () => false, configurable: true });
  }
  if (!("releasePointerCapture" in Element.prototype)) {
    Object.defineProperty(Element.prototype, "releasePointerCapture", { value: () => {}, configurable: true });
  }
  if (!("scrollIntoView" in Element.prototype)) {
    Object.defineProperty(Element.prototype, "scrollIntoView", { value: () => {}, configurable: true });
  }
  if (typeof globalThis.ResizeObserver === "undefined") {
    class FakeResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver;
  }
});

function renderScreen() {
  return render(
    <ThemeProvider defaultTheme="light">
      <RunsScreen />
    </ThemeProvider>,
  );
}

const PAIR = syncPairOutFixture();

describe("RunsScreen accessibility", () => {
  it("the populated history table -- with status badges and duration -- has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, {
        items: [
          runSummaryOutFixture({ id: "run-1", status: "ok" }),
          runSummaryOutFixture({ id: "run-2", status: "failed", in_progress: false }),
          runSummaryOutFixture({ id: "run-3", status: "ok", in_progress: true }),
        ],
        limit: 50,
        has_more: true,
        next_cursor: "cursor-token",
      }),
    });
    const { container } = renderScreen();
    await screen.findByText("OK");

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("the empty history state has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, []),
      "GET /api/runs": jsonResponse(200, { items: [], limit: 50, has_more: false, next_cursor: null }),
    });
    const { container } = renderScreen();
    await screen.findByText("No runs recorded yet");

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("a selected pair's run-controls panel -- live status badges, Run now, Pause -- has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, { items: [], limit: 50, has_more: false, next_cursor: null }),
      [`GET /api/pairs/${PAIR.id}/run-status`]: jsonResponse(
        200,
        runControlStatusOutFixture({ pair_id: PAIR.id, pair_name: PAIR.name }),
      ),
    });
    const { container } = renderScreen();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("combobox", { name: "Filter by sync pair" }));
    await user.click(await screen.findByRole("option", { name: PAIR.name }));
    await screen.findByRole("button", { name: "Run now" });

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("a running pair's controls panel (running badge, disabled Run now) has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, { items: [], limit: 50, has_more: false, next_cursor: null }),
      [`GET /api/pairs/${PAIR.id}/run-status`]: jsonResponse(
        200,
        runControlStatusOutFixture({ pair_id: PAIR.id, pair_name: PAIR.name, running: true }),
      ),
    });
    const { container } = renderScreen();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("combobox", { name: "Filter by sync pair" }));
    await user.click(await screen.findByRole("option", { name: PAIR.name }));
    await waitFor(() => expect(screen.getByText("Run in progress")).toBeInTheDocument());

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("the run-detail sheet -- status badge, counts tab, issues tab with an orphan and an unresolved owner -- has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, {
        items: [runSummaryOutFixture({ id: "run-detail-a11y" })],
        limit: 50,
        has_more: false,
        next_cursor: null,
      }),
      "GET /api/runs/run-detail-a11y": jsonResponse(
        200,
        runDetailOutFixture({ id: "run-detail-a11y", swept_stale: false }),
      ),
      "GET /api/runs/run-detail-a11y/issues": jsonResponse(
        200,
        runIssuesOutFixture({
          has_issues: true,
          unresolvable_owners: [runItemOutFixture({ native_key: "sales.orders", display_name: "Orders" })],
          orphans: [runOrphanIssueOutFixture({ display_name: "Vanished Table" })],
        }),
      ),
    });
    const { container } = renderScreen();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /view run detail/i }));
    await screen.findByRole("heading", { name: /run: /i });

    let results = await axe.run(container);
    expect(results.violations).toEqual([]);

    await user.click(screen.getByRole("tab", { name: "Issues" }));
    await screen.findByText("Vanished Table");
    results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("an abandoned (swept-stale) run's detail sheet has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, {
        items: [runSummaryOutFixture({ id: "run-abandoned", status: "failed" })],
        limit: 50,
        has_more: false,
        next_cursor: null,
      }),
      "GET /api/runs/run-abandoned": jsonResponse(
        200,
        runDetailOutFixture({ id: "run-abandoned", status: "failed", swept_stale: true }),
      ),
      "GET /api/runs/run-abandoned/issues": jsonResponse(200, runIssuesOutFixture({ status: "failed" })),
    });
    const { container } = renderScreen();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /view run detail/i }));
    await screen.findByText(/abandoned/i);

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("the Configuration changes tab has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, { items: [], limit: 50, has_more: false, next_cursor: null }),
      "GET /api/config-changes": jsonResponse(200, {
        items: [configChangeOutFixture()],
        limit: 50,
        has_more: false,
        next_cursor: null,
      }),
    });
    const { container } = renderScreen();
    await screen.findByText("No runs recorded yet");
    const user = userEvent.setup();
    await user.click(screen.getByRole("tab", { name: "Configuration changes" }));
    await screen.findByText("enabled");

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });
});
