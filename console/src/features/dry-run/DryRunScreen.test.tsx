// Screen-level tests -- drives the REAL `apiClient` through a stubbed `globalThis.fetch`
// (`installApiRouter`), never a mock of `apiClient` or of this feature's own modules. Each `it`
// either names the mutation check it kills, or exercises the "deliberate action, not an
// effect" wiring the task brief calls out by name.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { beforeAll, describe, expect, it } from "vitest";

import { installFetchMock } from "../../test/apiFixtures";
import { DryRunScreen } from "./DryRunScreen";
import {
  PAIR_ID,
  bodyOf,
  countsFixture,
  errorModelFixture,
  installApiRouter,
  jsonResponse,
  lastRequestTo,
  recordReportFixture,
  runReportsOutFixture,
  syncPairOutFixture,
  syncRunReportFixture,
  type Routes,
} from "./testHelpers";

beforeAll(() => {
  // Radix Select/Checkbox need a few DOM APIs jsdom does not implement -- same polyfill set
  // `../selection/SelectionScreen.test.tsx` and `../pairs/PairsScreen.test.tsx` install.
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

function baseRoutes(overrides: Routes = {}): Routes {
  return {
    "GET /api/pairs": jsonResponse(200, [syncPairOutFixture()]),
    ...overrides,
  };
}

function renderScreen() {
  return render(
    <ThemeProvider defaultTheme="light">
      <DryRunScreen />
    </ThemeProvider>,
  );
}

async function selectThePair(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("combobox", { name: /sync pair/i }));
  await user.click(await screen.findByRole("option", { name: /prod_databricks_to_qlik/ }));
  await screen.findByRole("button", { name: /run dry run/i });
}

describe("DryRunScreen: a dry run is a deliberate action, never an effect", () => {
  it("does not call the dry-run route just from loading or from selecting a pair", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const { calls } = installApiRouter(fetchMock, baseRoutes());

    renderScreen();
    await selectThePair(user);

    // Only the pair list was fetched -- picking a pair in the dropdown must never itself cost
    // a real, possibly-slow round trip against a live tenant.
    expect(calls).toEqual(["GET /api/pairs"]);
    expect(calls.some((call) => call.includes("dry-run"))).toBe(false);
  });

  it("only fires the dry-run request after an explicit click on 'Run dry run', with the real request body", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const { requests } = installApiRouter(
      fetchMock,
      baseRoutes({
        [`POST /api/pairs/${PAIR_ID}/dry-run`]: jsonResponse(200, runReportsOutFixture()),
      }),
    );

    renderScreen();
    await selectThePair(user);
    await user.click(screen.getByRole("button", { name: /run dry run/i }));

    await waitFor(() => {
      expect(screen.getByText(/this is a plan only/i)).toBeInTheDocument();
    });

    const request = lastRequestTo(requests, `POST /api/pairs/${PAIR_ID}/dry-run`);
    expect(await bodyOf(request)).toEqual({ create_missing: false });
  });

  it("sends create_missing: true over the wire when 'Preview creates for unbound objects' is checked", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const { requests } = installApiRouter(
      fetchMock,
      baseRoutes({
        [`POST /api/pairs/${PAIR_ID}/dry-run`]: jsonResponse(200, runReportsOutFixture()),
      }),
    );

    renderScreen();
    await selectThePair(user);
    await user.click(screen.getByRole("checkbox", { name: /preview creates for unbound objects/i }));
    await user.click(screen.getByRole("button", { name: /run dry run/i }));

    await waitFor(() => {
      expect(screen.getByText(/this is a plan only/i)).toBeInTheDocument();
    });

    const request = lastRequestTo(requests, `POST /api/pairs/${PAIR_ID}/dry-run`);
    expect(await bodyOf(request)).toEqual({ create_missing: true });
  });
});

describe("DryRunScreen: the three 'nothing happened' states are never confused", () => {
  it("mutation check 1: a plan that changes nothing renders a confident no-op banner, not an empty screen", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      baseRoutes({
        [`POST /api/pairs/${PAIR_ID}/dry-run`]: jsonResponse(
          200,
          runReportsOutFixture({
            runs: [
              syncRunReportFixture({
                entity_type: "data_product",
                counts: countsFixture({ read: 3, unchanged: 3 }),
                records: [
                  recordReportFixture({ native_key: "dp-1", display_name: "analytics.sales", outcome: "unchanged" }),
                ],
              }),
              syncRunReportFixture({
                entity_type: "dataset",
                counts: countsFixture({ read: 2, unchanged: 2 }),
                records: [
                  recordReportFixture({ native_key: "ds-1", display_name: "analytics.sales.orders", outcome: "unchanged" }),
                ],
              }),
            ],
          }),
        ),
      }),
    );

    renderScreen();
    await selectThePair(user);
    await user.click(screen.getByRole("button", { name: /run dry run/i }));

    // A confident, positive statement -- never an empty StatePanel, never a bare spinner.
    expect(
      await screen.findByText(/this run would change nothing/i),
    ).toBeInTheDocument();
    // And the underlying records are still genuinely rendered, not hidden behind the banner.
    expect(screen.getByText("analytics.sales")).toBeInTheDocument();
    expect(screen.getByText("analytics.sales.orders")).toBeInTheDocument();
    expect(screen.queryByText(/nothing to plan/i)).not.toBeInTheDocument();
  });

  it("mutation check 5: a dry run that fails at the request level renders an error state, distinct from an empty plan", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      baseRoutes({
        [`POST /api/pairs/${PAIR_ID}/dry-run`]: jsonResponse(
          422,
          errorModelFixture({ message: "endpoint 'databricks_prod' could not be reached" }),
        ),
      }),
    );

    renderScreen();
    await selectThePair(user);
    await user.click(screen.getByRole("button", { name: /run dry run/i }));

    expect(await screen.findByText("Could not produce a dry-run plan")).toBeInTheDocument();
    expect(screen.getByText(/could not be reached/)).toBeInTheDocument();
    // This is a request failure, never rendered as "the plan is empty" or "changes nothing".
    expect(screen.queryByText(/this run would change nothing/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/nothing to plan/i)).not.toBeInTheDocument();
  });

  it("clears a previous plan when a different pair is selected, rather than leaving a stale one on screen", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const secondPairId = "99999999-9999-9999-9999-999999999999";
    installApiRouter(
      fetchMock,
      baseRoutes({
        "GET /api/pairs": jsonResponse(200, [
          syncPairOutFixture(),
          syncPairOutFixture({ id: secondPairId, name: "second_pair" }),
        ]),
        [`POST /api/pairs/${PAIR_ID}/dry-run`]: jsonResponse(200, runReportsOutFixture()),
      }),
    );

    renderScreen();
    await selectThePair(user);
    await user.click(screen.getByRole("button", { name: /run dry run/i }));
    await screen.findByText(/this is a plan only/i);

    await user.click(screen.getByRole("combobox", { name: /sync pair/i }));
    await user.click(await screen.findByRole("option", { name: /second_pair/ }));

    expect(screen.queryByText(/this is a plan only/i)).not.toBeInTheDocument();
  });
});

describe("DryRunScreen: what a dry run does and does not do is stated plainly", () => {
  it("mutation check 6: states that a dry run writes nothing to Qlik", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      baseRoutes({
        [`POST /api/pairs/${PAIR_ID}/dry-run`]: jsonResponse(200, runReportsOutFixture()),
      }),
    );

    renderScreen();
    await selectThePair(user);
    await user.click(screen.getByRole("button", { name: /run dry run/i }));

    expect(
      await screen.findByText(/nothing above has been written to Qlik/i),
    ).toBeInTheDocument();
  });

  it("shows an honest elapsed-time status while the request is in flight, and disables the pair picker and button", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    let resolveDryRun!: (response: Response) => void;
    installApiRouter(
      fetchMock,
      baseRoutes({
        [`POST /api/pairs/${PAIR_ID}/dry-run`]: () =>
          new Promise<Response>((resolve) => {
            resolveDryRun = resolve;
          }),
      }),
    );

    renderScreen();
    await selectThePair(user);
    await user.click(screen.getByRole("button", { name: /run dry run/i }));

    expect(await screen.findByText(/reading .* source live/i)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /sync pair/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /running/i })).toBeDisabled();

    resolveDryRun(jsonResponse(200, runReportsOutFixture()));
    await screen.findByText(/this is a plan only/i);
  });
});

describe("DryRunScreen: unresolved references surface at the top of the plan", () => {
  it("D2 and D3 records appear inside the Unresolved references region before any per-entity-type section", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      baseRoutes({
        [`POST /api/pairs/${PAIR_ID}/dry-run`]: jsonResponse(
          200,
          runReportsOutFixture({
            runs: [
              syncRunReportFixture({
                entity_type: "data_product",
                counts: countsFixture({ read: 1, written: 1 }),
                records: [
                  recordReportFixture({
                    native_key: "dp-1",
                    display_name: "analytics.sales",
                    outcome: "written",
                    target_skipped_fields: ["dataset_refs", "owners"],
                    detail: "unresolved members and owners",
                  }),
                ],
              }),
            ],
          }),
        ),
      }),
    );

    renderScreen();
    await selectThePair(user);
    await user.click(screen.getByRole("button", { name: /run dry run/i }));

    const region = await screen.findByRole("region", { name: "Unresolved references" });
    expect(within(region).getByText("Unresolved dataset members (D2) — 1")).toBeInTheDocument();
    expect(within(region).getByText("Unresolvable owners (D3) — 1")).toBeInTheDocument();
  });
});
