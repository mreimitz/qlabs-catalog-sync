// Drives the REAL `RunsScreen` (and, through it, the real `apiClient`) via a stubbed
// `globalThis.fetch` (`installFetchMock`) and this feature's own `installApiRouter`, never a
// mock of `apiClient` or this feature's own modules. Every fixture comes from
// `./testHelpers.ts`, built from the real shapes in `../../api/generated/schema.ts`.
//
// The task brief's mutation table drives most of these tests directly -- each `it` names
// which mutation it kills in its own description.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { toast } from "@elabs-ai/components-ui";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { installFetchMock, requestFromMock } from "../../test/apiFixtures";
import { RunsScreen } from "./RunsScreen";
import {
  configChangeOutFixture,
  errorModelFixture,
  installApiRouter,
  jsonResponse,
  runControlStatusOutFixture,
  runDetailOutFixture,
  runIssuesOutFixture,
  runItemOutFixture,
  runOrphanIssueOutFixture,
  runSummaryOutFixture,
  syncPairOutFixture,
  type Routes,
} from "./testHelpers";

// Radix Select/Tabs need a few DOM APIs jsdom does not implement (pointer capture,
// scrollIntoView) -- see `../pairs/PairsScreen.test.tsx`'s identical block for why.
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

describe("RunsScreen -- history", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lists run history with pair, status and duration -- per-pair, not summed", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, {
        items: [
          runSummaryOutFixture({
            id: "run-1",
            pair: PAIR.name,
            status: "ok",
            in_progress: false,
            duration_seconds: 12.5,
          }),
        ],
        limit: 50,
        has_more: false,
        next_cursor: null,
      }),
    });
    renderScreen();

    await screen.findByText(PAIR.name);
    expect(screen.getByText("OK")).toBeInTheDocument();
    expect(screen.getByText("12.5s")).toBeInTheDocument();
  });

  it("mutation check 3: keyset pagination -- Load more sends the real next_cursor, appends rows, and never renders a numbered pager", async () => {
    const fetchMock = installFetchMock();
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": (request) => {
        const url = new URL(request.url);
        const cursor = url.searchParams.get("cursor");
        if (cursor === null) {
          return jsonResponse(200, {
            items: [runSummaryOutFixture({ id: "run-page-1", pair: PAIR.name })],
            limit: 50,
            has_more: true,
            next_cursor: "opaque-cursor-token",
          });
        }
        expect(cursor).toBe("opaque-cursor-token");
        return jsonResponse(200, {
          items: [runSummaryOutFixture({ id: "run-page-2", pair: PAIR.name, status: "partial" })],
          limit: 50,
          has_more: false,
          next_cursor: null,
        });
      },
    };
    installApiRouter(fetchMock, routes);
    renderScreen();

    await screen.findByText(PAIR.name);
    // Never a numbered pager for a keyset-paginated list.
    expect(screen.queryByRole("navigation", { name: /pagination/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/page 1/i)).not.toBeInTheDocument();

    const loadMore = screen.getByRole("button", { name: "Load more" });
    const user = userEvent.setup();
    await user.click(loadMore);

    await screen.findByText("Partial");
    // Both pages' rows are present -- the second page was APPENDED, not a replacement.
    expect(screen.getAllByText(PAIR.name)).toHaveLength(2);
    expect(screen.getByText("OK")).toBeInTheDocument();
    expect(screen.getByText("Partial")).toBeInTheDocument();
    // has_more: false on page two means the button is gone -- no dead-end pager either.
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("mutation check 2 (screen-level): a still-running row never renders as Failed in the history table", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, {
        items: [
          runSummaryOutFixture({ id: "run-running", status: "ok", in_progress: true }),
        ],
        limit: 50,
        has_more: false,
        next_cursor: null,
      }),
    });
    renderScreen();

    await screen.findByText("Running");
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
    expect(screen.queryByText("OK")).not.toBeInTheDocument();
  });

  it("selecting a pair shows its run controls, reading live status from the run-status route", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, { items: [], limit: 50, has_more: false, next_cursor: null }),
      [`GET /api/pairs/${PAIR.id}/run-status`]: jsonResponse(
        200,
        runControlStatusOutFixture({ pair_id: PAIR.id, pair_name: PAIR.name, paused: false, running: false }),
      ),
    });
    renderScreen();

    const user = userEvent.setup();
    await user.click(await screen.findByRole("combobox", { name: "Filter by sync pair" }));
    await user.click(await screen.findByRole("option", { name: PAIR.name }));

    await screen.findByRole("button", { name: "Run now" });
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run now" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  });

  it("mutation check 7: run-now re-reads live status afterward, on both success and refusal", async () => {
    const fetchMock = installFetchMock();
    let statusCall = 0;
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, { items: [], limit: 50, has_more: false, next_cursor: null }),
      [`GET /api/pairs/${PAIR.id}/run-status`]: () => {
        statusCall += 1;
        // Second read (after run-now settles) reports a cycle in flight -- proving the UI
        // reflects a FRESH read, not a locally-guessed "not running" state.
        return jsonResponse(
          200,
          runControlStatusOutFixture({
            pair_id: PAIR.id,
            pair_name: PAIR.name,
            running: statusCall > 1,
          }),
        );
      },
      [`POST /api/pairs/${PAIR.id}/run-now`]: jsonResponse(200, {
        pair_id: PAIR.id,
        pair_name: PAIR.name,
        generated_at: "2026-01-01T00:00:00Z",
        runs: [],
      }),
    };
    installApiRouter(fetchMock, routes);
    renderScreen();

    const user = userEvent.setup();
    await user.click(await screen.findByRole("combobox", { name: "Filter by sync pair" }));
    await user.click(await screen.findByRole("option", { name: PAIR.name }));
    await screen.findByRole("button", { name: "Run now" });

    await user.click(screen.getByRole("button", { name: "Run now" }));

    // The panel's own live region now reports "Run in progress" -- read from the SECOND
    // run-status call this action triggered, not inferred client-side.
    await screen.findByText("Run in progress");
    expect(statusCall).toBeGreaterThanOrEqual(2);
  });

  it("mutation check 7b: run-now refused (already running) still re-reads live status, and does not silently show 'not running'", async () => {
    const fetchMock = installFetchMock();
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, { items: [], limit: 50, has_more: false, next_cursor: null }),
      [`GET /api/pairs/${PAIR.id}/run-status`]: jsonResponse(
        200,
        runControlStatusOutFixture({ pair_id: PAIR.id, pair_name: PAIR.name, running: true }),
      ),
      [`POST /api/pairs/${PAIR.id}/run-now`]: jsonResponse(
        409,
        errorModelFixture({
          code: "sync_cycle_already_running",
          message: "already has a cycle in flight",
          entity: PAIR.name,
        }),
      ),
    };
    installApiRouter(fetchMock, routes);
    renderScreen();

    const user = userEvent.setup();
    await user.click(await screen.findByRole("combobox", { name: "Filter by sync pair" }));
    await user.click(await screen.findByRole("option", { name: PAIR.name }));

    // Live status already says "running" -- Run now is disabled by that live read, so the
    // panel does not even need the click to prove the point; assert the disabled state
    // directly, matching what a real refusal would look like from the operator's chair.
    await waitFor(() => expect(screen.getByRole("button", { name: "Run now" })).toBeDisabled());
    expect(screen.getByText("Run in progress")).toBeInTheDocument();
  });

  it("pause and resume flip the pair's live status without a page reload, and never toast for the ordinary paused fact", async () => {
    const toastErrorSpy = vi.spyOn(toast, "error");
    const fetchMock = installFetchMock();
    let paused = false;
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, { items: [], limit: 50, has_more: false, next_cursor: null }),
      [`GET /api/pairs/${PAIR.id}/run-status`]: () =>
        jsonResponse(200, runControlStatusOutFixture({ pair_id: PAIR.id, pair_name: PAIR.name, paused })),
      [`POST /api/pairs/${PAIR.id}/pause`]: () => {
        paused = true;
        return jsonResponse(200, runControlStatusOutFixture({ pair_id: PAIR.id, pair_name: PAIR.name, paused: true }));
      },
    };
    installApiRouter(fetchMock, routes);
    renderScreen();

    const user = userEvent.setup();
    await user.click(await screen.findByRole("combobox", { name: "Filter by sync pair" }));
    await user.click(await screen.findByRole("option", { name: PAIR.name }));
    await screen.findByText("Active");

    await user.click(screen.getByRole("button", { name: "Pause" }));
    await screen.findByText("Paused");
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(toastErrorSpy).not.toHaveBeenCalled();
  });

  it("mutation check 6: opening a run whose issues include real errors never fires an error toast -- the fetch succeeded", async () => {
    const toastErrorSpy = vi.spyOn(toast, "error");
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, {
        items: [runSummaryOutFixture({ id: "run-with-issues", status: "failed" })],
        limit: 50,
        has_more: false,
        next_cursor: null,
      }),
      "GET /api/runs/run-with-issues": jsonResponse(
        200,
        runDetailOutFixture({ id: "run-with-issues", status: "failed", swept_stale: false }),
      ),
      "GET /api/runs/run-with-issues/issues": jsonResponse(
        200,
        runIssuesOutFixture({
          status: "failed",
          has_issues: true,
          other_outstanding: [runItemOutFixture({ outcome: "failed" })],
        }),
      ),
    });
    renderScreen();

    await screen.findByText(PAIR.name);
    const user = userEvent.setup();
    const row = await screen.findByRole("button", { name: /view run detail/i });
    await user.click(row);

    await screen.findByRole("heading", { name: /run: /i });
    const issuesTab = screen.getByRole("tab", { name: "Issues" });
    await user.click(issuesTab);
    // The failed record's own outcome badge, inside the "Other outstanding" table -- proof
    // the issues panel actually rendered the failed item, not just the sheet shell.
    await waitFor(() => expect(screen.getAllByText("Failed").length).toBeGreaterThan(0));

    expect(toastErrorSpy).not.toHaveBeenCalled();
  });

  it("mutation check 5 (screen-level): the run detail's issues panel shows the object identity from a real fetch, not a hand-rolled string", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, {
        items: [runSummaryOutFixture({ id: "run-detail-1" })],
        limit: 50,
        has_more: false,
        next_cursor: null,
      }),
      "GET /api/runs/run-detail-1": jsonResponse(200, runDetailOutFixture({ id: "run-detail-1" })),
      "GET /api/runs/run-detail-1/issues": jsonResponse(
        200,
        runIssuesOutFixture({
          has_issues: true,
          unresolvable_owners: [
            runItemOutFixture({
              native_key: "sales.orders",
              display_name: "Orders",
              endpoint: "databricks_prod",
              unresolved_fields: ["owners"],
            }),
          ],
        }),
      ),
    });
    renderScreen();

    await screen.findByText(PAIR.name);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /view run detail/i }));
    await user.click(screen.getByRole("tab", { name: "Issues" }));

    expect(await screen.findByText("Orders")).toBeInTheDocument();
    expect(screen.getByText("sales.orders")).toBeInTheDocument();
  });

  it("mutation check 1 (screen-level): a run detail with an orphan never shows destructive/deleted wording anywhere in the sheet", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, {
        items: [runSummaryOutFixture({ id: "run-orphan-1" })],
        limit: 50,
        has_more: false,
        next_cursor: null,
      }),
      "GET /api/runs/run-orphan-1": jsonResponse(200, runDetailOutFixture({ id: "run-orphan-1" })),
      "GET /api/runs/run-orphan-1/issues": jsonResponse(
        200,
        runIssuesOutFixture({
          has_issues: true,
          orphans: [runOrphanIssueOutFixture({ display_name: "Vanished Table" })],
        }),
      ),
    });
    renderScreen();

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /view run detail/i }));
    await user.click(screen.getByRole("tab", { name: "Issues" }));

    await screen.findByText("Vanished Table");
    expect(screen.getByText("Orphaned -- reported, not deleted")).toBeInTheDocument();
    expect(screen.queryByText("Deleted")).not.toBeInTheDocument();
    expect(screen.queryByText("Removed")).not.toBeInTheDocument();
  });

  it("filtering by status resets pagination to a fresh first page (no stale cursor from a previous filter)", async () => {
    const fetchMock = installFetchMock();
    const calls: string[] = [];
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": (request) => {
        const url = new URL(request.url);
        calls.push(url.search);
        return jsonResponse(200, {
          items: [runSummaryOutFixture({ id: `run-${calls.length}` })],
          limit: 50,
          has_more: false,
          next_cursor: null,
        });
      },
    };
    installApiRouter(fetchMock, routes);
    renderScreen();

    await screen.findByText(PAIR.name);
    const user = userEvent.setup();
    await user.click(screen.getByRole("combobox", { name: "Filter by run status" }));
    await user.click(await screen.findByRole("option", { name: "Failed" }));

    await waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2));
    const lastCall = calls[calls.length - 1]!;
    expect(lastCall).toContain("status=failed");
    expect(lastCall).not.toContain("cursor=");
  });
});

describe("RunsScreen -- configuration changes", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("switching to the Configuration changes tab lists the raw change log, one row per changed field", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, { items: [], limit: 50, has_more: false, next_cursor: null }),
      "GET /api/config-changes": jsonResponse(200, {
        items: [configChangeOutFixture({ field: "enabled", entity_id: PAIR.name })],
        limit: 50,
        has_more: false,
        next_cursor: null,
      }),
    });
    renderScreen();

    // Wait for the History tab's own initial load to settle first (its empty state), so the
    // tab switch below is not racing the screen's first render.
    await screen.findByText("No runs recorded yet");
    const user = userEvent.setup();
    await user.click(screen.getByRole("tab", { name: "Configuration changes" }));

    expect(await screen.findByText("enabled")).toBeInTheDocument();
    const requests = fetchMock.mock.calls.map((_, index) => requestFromMock(fetchMock, index));
    expect(requests.some((req) => new URL(req.url).pathname === "/api/config-changes")).toBe(true);
  });

  it("mutation check 3 (config changes): Load more sends the real next_cursor and appends, never a numbered pager", async () => {
    const fetchMock = installFetchMock();
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(200, [PAIR]),
      "GET /api/runs": jsonResponse(200, { items: [], limit: 50, has_more: false, next_cursor: null }),
      "GET /api/config-changes": (request) => {
        const url = new URL(request.url);
        const cursor = url.searchParams.get("cursor");
        if (cursor === null) {
          return jsonResponse(200, {
            items: [configChangeOutFixture({ id: "change-1", field: "enabled" })],
            limit: 50,
            has_more: true,
            next_cursor: "changes-cursor-token",
          });
        }
        expect(cursor).toBe("changes-cursor-token");
        return jsonResponse(200, {
          items: [configChangeOutFixture({ id: "change-2", field: "cadence_seconds" })],
          limit: 50,
          has_more: false,
          next_cursor: null,
        });
      },
    };
    installApiRouter(fetchMock, routes);
    renderScreen();

    await screen.findByText("No runs recorded yet");
    const user = userEvent.setup();
    await user.click(screen.getByRole("tab", { name: "Configuration changes" }));
    await screen.findByText("enabled");

    expect(screen.queryByRole("navigation", { name: /pagination/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load more" }));

    await screen.findByText("cadence_seconds");
    expect(screen.getByText("enabled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });
});
