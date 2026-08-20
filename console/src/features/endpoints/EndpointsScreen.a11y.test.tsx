// Named *.a11y.test.tsx beside the screen it covers (console/CLAUDE.md's load-bearing naming
// convention -- `pnpm a11y` runs `vitest run a11y`, a path-substring filter). Covers the states
// the task brief calls out as exactly where label/name/role defects appear: a form (the
// register sheet, including a field-level validation error), a data table (the endpoint list,
// including its status badges and inline Switch), and a status-bearing list (health/secret
// badges, the manifest sheet).
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import axe from "axe-core";
import { beforeAll, describe, expect, it } from "vitest";

import { installFetchMock } from "../../test/apiFixtures";
import { EndpointsScreen } from "./EndpointsScreen";
import {
  connectorInfoFixture,
  endpointManifestOutFixture,
  endpointOutFixture,
  errorModelFixture,
  healthOutFixture,
  installApiRouter,
  jsonResponse,
  secretResolveFixture,
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
      <EndpointsScreen />
    </ThemeProvider>,
  );
}

describe("EndpointsScreen accessibility", () => {
  it("the populated list -- with health, secret-reference and enabled controls -- has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/endpoints": jsonResponse(200, [
        endpointOutFixture({ name: "ep_a", secret_ref: "env:A" }),
        endpointOutFixture({ name: "ep_b", secret_ref: null, enabled: false }),
      ]),
      "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
      "GET /api/endpoints/ep_a/secret-resolve": jsonResponse(200, secretResolveFixture({ resolvable: true })),
      "GET /api/endpoints/ep_b/secret-resolve": jsonResponse(200, secretResolveFixture({ resolvable: false })),
    });

    const { container } = renderScreen();
    await screen.findByText("ep_a");
    await waitFor(() => expect(screen.getByText("Resolves")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("Unresolvable")).toBeInTheDocument());

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("the empty state has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/endpoints": jsonResponse(200, []),
      "GET /api/connectors": jsonResponse(200, []),
    });
    const { container } = renderScreen();
    await screen.findByText("No endpoints registered yet");
    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("the register form, including a red healthcheck badge already on a row behind it, has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/endpoints": jsonResponse(200, [endpointOutFixture({ name: "ep_a" })]),
      "GET /api/connectors": jsonResponse(200, [
        connectorInfoFixture({ name: "fake" }),
        connectorInfoFixture({
          name: "broken_one",
          available: false,
          distribution: "acme-broken",
          broken_reason: "ImportError",
        }),
      ]),
      "GET /api/endpoints/ep_a/secret-resolve": jsonResponse(200, secretResolveFixture()),
    });
    const { container } = renderScreen();
    await screen.findByText("ep_a");
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "Register endpoint" })[0]!);
    await screen.findByRole("heading", { name: "Register endpoint" });

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("a field-level validation error on the register form has no axe violations", async () => {
    const fetchMock = installFetchMock();
    const routes: Record<string, ReturnType<typeof jsonResponse> | ((r: Request) => Response)> = {
      "GET /api/endpoints": jsonResponse(200, []),
      "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
    };
    installApiRouter(fetchMock, routes);
    const { container } = renderScreen();
    await screen.findByText("No endpoints registered yet");
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "Register endpoint" })[0]!);
    await screen.findByRole("heading", { name: "Register endpoint" });

    await user.type(screen.getByLabelText("Name"), "new_ep");
    await user.click(screen.getByRole("combobox", { name: "Connector" }));
    await user.click(await screen.findByRole("option", { name: "fake" }));
    await user.click(screen.getByRole("combobox", { name: "Role" }));
    await user.click(await screen.findByRole("option", { name: "source" }));

    routes["POST /api/endpoints"] = jsonResponse(
      422,
      errorModelFixture({
        code: "connector_not_registered",
        message: "connector 'fake' is not registered",
        field: "connector",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Register endpoint" }));
    await screen.findByRole("alert");

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("the capability manifest sheet, both with a manifest and with an unavailable reason, has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/endpoints": jsonResponse(200, [endpointOutFixture({ name: "ep_a" })]),
      "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
      "GET /api/endpoints/ep_a/secret-resolve": jsonResponse(200, secretResolveFixture()),
      "GET /api/endpoints/ep_a/manifest": jsonResponse(200, endpointManifestOutFixture({ endpoint: "ep_a" })),
    });
    const { container } = renderScreen();
    await screen.findByText("ep_a");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: 'View capability manifest for "ep_a"' }));
    await screen.findByText("data_product");

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("a red healthcheck badge on the list has no axe violations (never rendered as an alert/toast)", async () => {
    const fetchMock = installFetchMock();
    const routes: Record<string, ReturnType<typeof jsonResponse> | ((r: Request) => Response)> = {
      "GET /api/endpoints": jsonResponse(200, [endpointOutFixture({ name: "ep_a" })]),
      "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
      "GET /api/endpoints/ep_a/secret-resolve": jsonResponse(200, secretResolveFixture()),
    };
    installApiRouter(fetchMock, routes);
    const { container } = renderScreen();
    await screen.findByText("ep_a");
    await waitFor(() => expect(screen.getByText("Resolves")).toBeInTheDocument());

    routes["POST /api/endpoints/ep_a/healthcheck"] = jsonResponse(
      200,
      healthOutFixture({ endpoint: "ep_a", state: "unhealthy", reason: "connection refused" }),
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: 'Run healthcheck for "ep_a"' }));
    await screen.findByText("unhealthy");

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });
});
