// Named *.a11y.test.tsx beside the screen it covers (console/CLAUDE.md's load-bearing naming
// convention -- `pnpm a11y` runs `vitest run a11y`, a path-substring filter). Covers the
// states the task brief calls out as exactly where label/name/role defects appear: a form
// with switches, selects and checkboxes (the create/edit sheet, including the activation
// switch's linked consequence text and a field-level validation error), a data table (the
// pair list, including its activation/enabled badges and inline `Switch`), and a picker's
// on-demand health status.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import axe from "axe-core";
import { beforeAll, describe, expect, it } from "vitest";

import { installFetchMock } from "../../test/apiFixtures";
import { PairsScreen } from "./PairsScreen";
import {
  endpointOutFixture,
  errorModelFixture,
  healthOutFixture,
  installApiRouter,
  jsonResponse,
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
      <PairsScreen />
    </ThemeProvider>,
  );
}

const SOURCE_EP = endpointOutFixture({ name: "databricks_prod", connector: "databricks", role: "source", enabled: true });
const TARGET_EP = endpointOutFixture({ name: "qlik_prod", connector: "qlik", role: "target", enabled: true });
const DISABLED_SOURCE_EP = endpointOutFixture({ name: "disabled_source_ep", connector: "databricks", role: "source", enabled: false });

describe("PairsScreen accessibility", () => {
  it("the populated list -- with entity types, activation and enabled controls -- has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [
        syncPairOutFixture({ name: "pair_a", activation_opt_in: true, enabled: true }),
        syncPairOutFixture({ name: "pair_b", activation_opt_in: false, enabled: false }),
      ]),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    });

    const { container } = renderScreen();
    await screen.findByText("pair_a");

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("the empty state has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, []),
      "GET /api/endpoints": jsonResponse(200, []),
    });
    const { container } = renderScreen();
    await screen.findByText("No sync pairs configured yet");
    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("the create form -- including the activation switch's linked consequence text -- has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, []),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP, DISABLED_SOURCE_EP]),
    });
    const { container } = renderScreen();
    await screen.findByText("No sync pairs configured yet");
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "New sync pair" })[0]!);
    await screen.findByRole("heading", { name: "New sync pair" });

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("the create form with an entity type selected (rendering a per-entity override row) has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, []),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    });
    const { container } = renderScreen();
    await screen.findByText("No sync pairs configured yet");
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "New sync pair" })[0]!);
    await screen.findByRole("heading", { name: "New sync pair" });
    await user.click(screen.getByRole("checkbox", { name: "Dataset" }));

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("a field-level validation error on the create form has no axe violations", async () => {
    const fetchMock = installFetchMock();
    const routes: Record<string, ReturnType<typeof jsonResponse> | ((r: Request) => Response)> = {
      "GET /api/pairs": jsonResponse(200, []),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    };
    installApiRouter(fetchMock, routes);
    const { container } = renderScreen();
    await screen.findByText("No sync pairs configured yet");
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "New sync pair" })[0]!);
    await screen.findByRole("heading", { name: "New sync pair" });

    await user.type(screen.getByLabelText("Name"), "dup_pair");
    await user.click(screen.getByRole("combobox", { name: "Source endpoint" }));
    await user.click(await screen.findByRole("option", { name: /databricks_prod/ }));
    await user.click(screen.getByRole("combobox", { name: "Target endpoint" }));
    await user.click(await screen.findByRole("option", { name: /qlik_prod/ }));
    await user.type(screen.getByLabelText("Target Qlik space"), "analytics/finance");

    routes["POST /api/pairs"] = jsonResponse(
      409,
      errorModelFixture({
        code: "sync_pair_already_exists",
        message: "a sync pair named 'dup_pair' already exists",
        entity: "dup_pair",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Create sync pair" }));
    await screen.findByRole("alert");

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("a red healthcheck badge from the picker's Check health action has no axe violations", async () => {
    const fetchMock = installFetchMock();
    const routes: Record<string, ReturnType<typeof jsonResponse> | ((r: Request) => Response)> = {
      "GET /api/pairs": jsonResponse(200, []),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    };
    installApiRouter(fetchMock, routes);
    const { container } = renderScreen();
    await screen.findByText("No sync pairs configured yet");
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "New sync pair" })[0]!);
    await screen.findByRole("heading", { name: "New sync pair" });
    await user.click(screen.getByRole("combobox", { name: "Source endpoint" }));
    await user.click(await screen.findByRole("option", { name: /databricks_prod/ }));

    routes["POST /api/endpoints/databricks_prod/healthcheck"] = jsonResponse(
      200,
      healthOutFixture({ endpoint: "databricks_prod", state: "unhealthy", reason: "connection refused" }),
    );
    await user.click(screen.getByRole("button", { name: 'Run healthcheck for "databricks_prod"' }));
    await waitFor(() => expect(screen.getByText("unhealthy")).toBeInTheDocument());

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("the delete confirmation dialog has no axe violations", async () => {
    const fetchMock = installFetchMock();
    const pair = syncPairOutFixture();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [pair]),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    });
    const { container } = renderScreen();
    await screen.findByText(pair.name);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: `Delete sync pair "${pair.name}"` }));
    await screen.findByRole("alertdialog");

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });
});
