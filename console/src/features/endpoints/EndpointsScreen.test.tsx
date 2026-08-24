// Drives the REAL `EndpointsScreen` (and, through it, the real `apiClient`) via a stubbed
// `globalThis.fetch` (`installFetchMock`, T13.2's `../../test/apiFixtures.ts`) and this
// feature's own `installApiRouter` (`./testHelpers.ts`) -- never a mock of `apiClient` or of
// this feature's own modules. Every fixture is built from the real shapes in
// `../../api/generated/schema.ts` via `./testHelpers.ts`'s fixture builders.
//
// The task brief's mutation table drives most of these tests directly -- each `it` below names
// which mutation it kills in its own description.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { toast } from "@elabs-ai/components-ui";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { installFetchMock } from "../../test/apiFixtures";
import { EndpointsScreen } from "./EndpointsScreen";
import {
  connectorInfoFixture,
  endpointOutFixture,
  endpointManifestOutFixture,
  errorModelFixture,
  healthOutFixture,
  installApiRouter,
  jsonResponse,
  manifestFixture,
  secretResolveFixture,
  type Routes,
} from "./testHelpers";

// Radix Select/Dialog need a few DOM APIs jsdom does not implement (pointer capture,
// scrollIntoView) -- confirmed empirically against this exact jsdom/user-event/Radix version
// combination before relying on it across this whole suite. Local to this feature's tests;
// `../../test/setup.ts` is T13.1/T13.2's, not touched here.
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

describe("EndpointsScreen", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  // --------------------------------------------------------------------------------------
  // The three connector states (available+manifest / available+no-manifest / unavailable) --
  // mutation checks #2 and #3.
  // --------------------------------------------------------------------------------------
  describe("connector states in the register form", () => {
    function connectorsFixtureSet() {
      return [
        connectorInfoFixture({ name: "qlik", available: true, manifest: manifestFixture() }),
        connectorInfoFixture({
          name: "databricks",
          available: true,
          manifest: null,
          manifest_unavailable_reason:
            "this connector reports what it supports only once an endpoint using it has been configured",
        }),
        connectorInfoFixture({
          name: "broken_one",
          available: false,
          manifest: null,
          distribution: "acme-broken-connector",
          broken_stage: "load",
          broken_reason: "ImportError: no module named acme_broken_connector",
        }),
      ];
    }

    async function openRegisterForm() {
      const fetchMock = installFetchMock();
      installApiRouter(fetchMock, {
        "GET /api/endpoints": jsonResponse(200, []),
        "GET /api/connectors": jsonResponse(200, connectorsFixtureSet()),
      });
      renderScreen();
      await screen.findByText("No endpoints registered yet");
      const user = userEvent.setup();
      await user.click(screen.getAllByRole("button", { name: "Register endpoint" })[0]!);
      await screen.findByRole("heading", { name: "Register endpoint" });
      return user;
    }

    it("an available connector with a manifest is selectable and its manifest is shown, never as unsupported", async () => {
      const user = await openRegisterForm();
      await user.click(screen.getByRole("combobox", { name: "Connector" }));
      const qlikOption = await screen.findByRole("option", { name: "qlik" });
      await user.click(qlikOption);

      const summary = await screen.findByText('What "qlik" supports');
      await user.click(summary);
      expect(screen.getByText("data_product")).toBeInTheDocument();
      expect(screen.getByText("Supported")).toBeInTheDocument();
    });

    it(
      "MUTATION #2 -- an available connector that cannot describe itself yet renders its reason, " +
        "never as unsupported/empty, and stays selectable",
      async () => {
        const user = await openRegisterForm();
        await user.click(screen.getByRole("combobox", { name: "Connector" }));
        const databricksOption = await screen.findByRole("option", { name: "databricks" });
        await user.click(databricksOption);

        // Selecting it succeeded (proves it is registrable) and the reason is rendered plainly.
        expect(
          screen.getByText(
            "this connector reports what it supports only once an endpoint using it has been configured",
          ),
        ).toBeInTheDocument();
        // Never rendered as "unsupported" or an empty manifest.
        expect(screen.queryByText(/unsupported/i)).not.toBeInTheDocument();
        expect(screen.queryByText(/supports nothing/i)).not.toBeInTheDocument();
      },
    );

    it("MUTATION #3 -- a broken (unavailable) connector is never offered as registrable", async () => {
      const user = await openRegisterForm();
      await user.click(screen.getByRole("combobox", { name: "Connector" }));

      // Not present as a choosable option at all.
      expect(screen.queryByRole("option", { name: "broken_one" })).not.toBeInTheDocument();
      await user.keyboard("{Escape}");

      // But its brokenness IS surfaced, with distribution and reason, not hidden.
      expect(screen.getByText(/broken_one/)).toBeInTheDocument();
      expect(screen.getByText(/acme-broken-connector/)).toBeInTheDocument();
      expect(screen.getByText(/ImportError: no module named acme_broken_connector/)).toBeInTheDocument();
    });
  });

  // --------------------------------------------------------------------------------------
  // MUTATION #4 -- secret-resolve status per row, fetched automatically on load (never
  // requiring a click), visible at a glance.
  // --------------------------------------------------------------------------------------
  it("MUTATION #4 -- resolves every row's secret reference automatically on load, without any click", async () => {
    const fetchMock = installFetchMock();
    const endpoints = [
      endpointOutFixture({ name: "ep_a", secret_ref: "env:A" }),
      endpointOutFixture({ name: "ep_b", secret_ref: null, connector: "fake" }),
    ];
    const router = installApiRouter(fetchMock, {
      "GET /api/endpoints": jsonResponse(200, endpoints),
      "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
      "GET /api/endpoints/ep_a/secret-resolve": jsonResponse(
        200,
        secretResolveFixture({ resolvable: true, reason: "resolved from the environment backend" }),
      ),
      "GET /api/endpoints/ep_b/secret-resolve": jsonResponse(
        200,
        secretResolveFixture({ resolvable: false, reason: "no secret_ref is bound to this endpoint yet" }),
      ),
    });

    renderScreen();
    await screen.findByText("ep_a");

    // No click anywhere -- both rows resolve on their own.
    await waitFor(() => expect(screen.getByText("Resolves")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("Unresolvable")).toBeInTheDocument());
    expect(router.calls).toContain("GET /api/endpoints/ep_a/secret-resolve");
    expect(router.calls).toContain("GET /api/endpoints/ep_b/secret-resolve");

    // Health starts unchecked -- it is never fetched automatically (see the healthcheck tests).
    expect(screen.getAllByText("Not checked yet")).toHaveLength(2);
  });

  // --------------------------------------------------------------------------------------
  // MUTATION #1 -- a red healthcheck is a calm status, never an error toast. Contrasted with a
  // genuine request failure, which DOES toast.
  // --------------------------------------------------------------------------------------
  describe("healthcheck", () => {
    function baseRoutes(): Routes {
      return {
        "GET /api/endpoints": jsonResponse(200, [endpointOutFixture({ name: "ep_a" })]),
        "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
        "GET /api/endpoints/ep_a/secret-resolve": jsonResponse(200, secretResolveFixture()),
      };
    }

    it("MUTATION #1 -- an unhealthy result renders as a status badge, never an error toast", async () => {
      const fetchMock = installFetchMock();
      const routes = baseRoutes();
      installApiRouter(fetchMock, routes);
      renderScreen();
      await screen.findByText("ep_a");
      await waitFor(() => expect(screen.getByText("Resolves")).toBeInTheDocument());

      const toastErrorSpy = vi.spyOn(toast, "error");
      const toastSuccessSpy = vi.spyOn(toast, "success");

      routes["POST /api/endpoints/ep_a/healthcheck"] = jsonResponse(
        200,
        healthOutFixture({ endpoint: "ep_a", state: "unhealthy", reason: "connection refused" }),
      );
      installApiRouter(fetchMock, routes);

      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: 'Run healthcheck for "ep_a"' }));

      await screen.findByText("unhealthy");
      expect(toastErrorSpy).not.toHaveBeenCalled();
      expect(toastSuccessSpy).not.toHaveBeenCalled();
    });

    it("a genuine request failure while healthchecking (distinct from a red result) DOES toast an error", async () => {
      const fetchMock = installFetchMock();
      const routes = baseRoutes();
      installApiRouter(fetchMock, routes);
      renderScreen();
      await screen.findByText("ep_a");
      await waitFor(() => expect(screen.getByText("Resolves")).toBeInTheDocument());

      const toastErrorSpy = vi.spyOn(toast, "error");

      routes["POST /api/endpoints/ep_a/healthcheck"] = jsonResponse(
        422,
        errorModelFixture({ code: "connector_lookup_error", message: "connector 'fake' is broken" }),
      );
      installApiRouter(fetchMock, routes);

      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: 'Run healthcheck for "ep_a"' }));

      await waitFor(() => expect(toastErrorSpy).toHaveBeenCalledTimes(1));
      expect(toastErrorSpy.mock.calls[0]?.[0]).toContain("connector 'fake' is broken");
    });
  });

  // --------------------------------------------------------------------------------------
  // MUTATION #6 -- the per-endpoint manifest is never fetched automatically for every row.
  // --------------------------------------------------------------------------------------
  it("MUTATION #6 -- GET .../manifest is only fetched when 'View capability manifest' is clicked, never on load", async () => {
    const fetchMock = installFetchMock();
    const routes: Routes = {
      "GET /api/endpoints": jsonResponse(200, [endpointOutFixture({ name: "ep_a" })]),
      "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
      "GET /api/endpoints/ep_a/secret-resolve": jsonResponse(200, secretResolveFixture()),
    };
    const router = installApiRouter(fetchMock, routes);

    renderScreen();
    await screen.findByText("ep_a");
    await waitFor(() => expect(screen.getByText("Resolves")).toBeInTheDocument());

    // Give any stray microtask a chance to fire before asserting the negative.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(router.calls.some((call) => call.endsWith("/manifest"))).toBe(false);

    // Now add the route and click the action -- a deliberate, per-row operator action. Mutating
    // the SAME `routes` object the router already reads from (rather than re-installing) keeps
    // `router.calls` as the one log for this whole test.
    routes["GET /api/endpoints/ep_a/manifest"] = jsonResponse(200, endpointManifestOutFixture({ endpoint: "ep_a" }));

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: 'View capability manifest for "ep_a"' }));

    await screen.findByRole("heading", { name: /Capability manifest: ep_a/ });
    await screen.findByText("data_product");
    expect(router.calls.filter((call) => call === "GET /api/endpoints/ep_a/manifest")).toHaveLength(1);
  });

  it("a manifest the connector could not produce right now renders its reason calmly, not as a page error", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/endpoints": jsonResponse(200, [endpointOutFixture({ name: "ep_a" })]),
      "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
      "GET /api/endpoints/ep_a/secret-resolve": jsonResponse(200, secretResolveFixture()),
      "GET /api/endpoints/ep_a/manifest": jsonResponse(
        200,
        endpointManifestOutFixture({ endpoint: "ep_a", manifest: null, unavailable_reason: "connector did not respond within 30s" }),
      ),
    });
    renderScreen();
    await screen.findByText("ep_a");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: 'View capability manifest for "ep_a"' }));
    await screen.findByText("connector did not respond within 30s");
  });

  // --------------------------------------------------------------------------------------
  // MUTATION #5 -- a validation error attaches to the offending field, never only a banner.
  // --------------------------------------------------------------------------------------
  describe("validation errors attach to the offending field", () => {
    async function openRegisterFormFilled(user: ReturnType<typeof userEvent.setup>) {
      await screen.findByText("No endpoints registered yet");
      await user.click(screen.getAllByRole("button", { name: "Register endpoint" })[0]!);
      await screen.findByRole("heading", { name: "Register endpoint" });
      await user.type(screen.getByLabelText("Name"), "new_ep");
      await user.click(screen.getByRole("combobox", { name: "Connector" }));
      await user.click(await screen.findByRole("option", { name: "fake" }));
      await user.click(screen.getByRole("combobox", { name: "Role" }));
      await user.click(await screen.findByRole("option", { name: "source" }));
    }

    it("MUTATION #5a -- a top-level field error (connector_not_registered) attaches to the Connector field, not a generic banner", async () => {
      const fetchMock = installFetchMock();
      const routes: Routes = {
        "GET /api/endpoints": jsonResponse(200, []),
        "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
      };
      installApiRouter(fetchMock, routes);
      renderScreen();
      const user = userEvent.setup();
      await openRegisterFormFilled(user);

      routes["POST /api/endpoints"] = jsonResponse(
        422,
        errorModelFixture({
          code: "connector_not_registered",
          message: "connector 'fake' is not registered",
          field: "connector",
          entity: "fake",
        }),
      );
      installApiRouter(fetchMock, routes);
      await user.click(screen.getByRole("button", { name: "Register endpoint" }));

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("connector 'fake' is not registered");
      // Exactly one occurrence anywhere in the document -- proves it is not ALSO duplicated as
      // a generic top-of-form banner.
      expect(screen.getAllByText("connector 'fake' is not registered")).toHaveLength(1);
      // And it is genuinely wired to the Connector control, not just floating text nearby.
      const connectorCombobox = screen.getByRole("combobox", { name: "Connector" });
      expect(connectorCombobox).toHaveAttribute("aria-describedby", alert.id);
    });

    it("MUTATION #5b -- an inline-secret-rejected error attaches to the specific settings row, not a generic banner", async () => {
      const fetchMock = installFetchMock();
      const routes: Routes = {
        "GET /api/endpoints": jsonResponse(200, []),
        "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
      };
      installApiRouter(fetchMock, routes);
      renderScreen();
      const user = userEvent.setup();
      await openRegisterFormFilled(user);

      await user.click(screen.getByRole("button", { name: "Add setting" }));
      const keyInputs = screen.getAllByPlaceholderText("setting_name");
      await user.type(keyInputs[keyInputs.length - 1]!, "api_key");
      const valueInputs = screen.getAllByPlaceholderText("value");
      await user.type(valueInputs[valueInputs.length - 1]!, "sk-should-never-be-sent");

      routes["POST /api/endpoints"] = jsonResponse(
        422,
        errorModelFixture({
          code: "inline_secret_rejected",
          message: "connector 'fake' settings must not carry secret-typed field(s) 'api_key' inline",
          field: "api_key",
          entity: "fake",
        }),
      );
      installApiRouter(fetchMock, routes);
      await user.click(screen.getByRole("button", { name: "Register endpoint" }));

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("must not carry secret-typed field");
      // Attached to the "api_key" row's Key input specifically.
      const apiKeyRow = keyInputs[keyInputs.length - 1]!.closest("div")?.parentElement;
      expect(apiKeyRow).toBeTruthy();
      expect(within(apiKeyRow as HTMLElement).getByRole("alert")).toBe(alert);
      // Not duplicated as a form-level banner.
      expect(screen.getAllByText(/must not carry secret-typed field/)).toHaveLength(1);
    });
  });

  // --------------------------------------------------------------------------------------
  // MUTATION #7 -- settings rows and the secret REFERENCE field are always plain text. A
  // credential has its own panel (CredentialsPanel, amended C2) and is the ONE masked input in
  // this form; it appears only when editing a registered endpoint, never here. The rule this
  // pins is therefore narrower than it once was, and more useful: a credential must not be
  // typeable into a field that will be stored in the clear as ordinary settings.
  // --------------------------------------------------------------------------------------
  it("MUTATION #7 -- no settings row or the secret-reference field ever renders as a maskable credential input", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/endpoints": jsonResponse(200, []),
      "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
    });
    renderScreen();
    const user = userEvent.setup();
    await screen.findByText("No endpoints registered yet");
    await user.click(screen.getAllByRole("button", { name: "Register endpoint" })[0]!);
    await screen.findByRole("heading", { name: "Register endpoint" });

    // The secret REFERENCE field: a plain text input, never type="password" -- it holds a
    // reference string ("db:my_endpoint", "env:QLIK_ACME"), not a credential value.
    const secretRefInput = screen.getByPlaceholderText("db:my_endpoint");
    expect(secretRefInput).toHaveAttribute("type", "text");

    await user.click(screen.getByRole("button", { name: "Add setting" }));
    const valueInput = screen.getAllByPlaceholderText("value").at(-1)!;
    expect(valueInput).toHaveAttribute("type", "text");

    // No control anywhere in the form offers to mark a settings row "secret" (which would
    // mask/imply it is safe to type a credential into).
    expect(screen.queryByRole("checkbox", { name: /secret/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /secret/i })).not.toBeInTheDocument();
    // Register mode offers no credential input at all: a credential is sealed against the
    // endpoint it belongs to, so there is nothing to attach one to until the endpoint exists.
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  // --------------------------------------------------------------------------------------
  // End-to-end sanity: register, edit, enable/disable, delete -- "entirely from the browser".
  // --------------------------------------------------------------------------------------
  it("registers a new endpoint end to end and shows it in the list with its secret status resolved", async () => {
    const fetchMock = installFetchMock();
    const routes: Routes = {
      "GET /api/endpoints": jsonResponse(200, []),
      "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
    };
    installApiRouter(fetchMock, routes);
    renderScreen();
    const user = userEvent.setup();
    await screen.findByText("No endpoints registered yet");
    await user.click(screen.getAllByRole("button", { name: "Register endpoint" })[0]!);
    await screen.findByRole("heading", { name: "Register endpoint" });

    await user.type(screen.getByLabelText("Name"), "new_ep");
    await user.click(screen.getByRole("combobox", { name: "Connector" }));
    await user.click(await screen.findByRole("option", { name: "fake" }));
    await user.click(screen.getByRole("combobox", { name: "Role" }));
    await user.click(await screen.findByRole("option", { name: "source" }));
    await user.type(screen.getByPlaceholderText("db:my_endpoint"), "env:FAKE");

    let capturedBody: unknown = null;
    routes["POST /api/endpoints"] = async (request: Request) => {
      capturedBody = await request.clone().json();
      return jsonResponse(
        201,
        endpointOutFixture({
          name: "new_ep",
          connector: "fake",
          role: "source",
          secret_ref: "env:FAKE",
          settings: {},
          enabled: false,
        }),
      );
    };
    routes["GET /api/endpoints/new_ep/secret-resolve"] = jsonResponse(200, secretResolveFixture());
    installApiRouter(fetchMock, routes);

    const toastSuccessSpy = vi.spyOn(toast, "success");
    await user.click(screen.getByRole("button", { name: "Register endpoint" }));

    await screen.findByText("new_ep");
    expect(screen.queryByRole("heading", { name: "Register endpoint" })).not.toBeInTheDocument();
    expect(toastSuccessSpy).toHaveBeenCalledWith(expect.stringContaining("new_ep"));
    expect(capturedBody).toMatchObject({
      name: "new_ep",
      connector: "fake",
      role: "source",
      secret_ref: "env:FAKE",
      settings: {},
    });
  });

  it("toggles enabled with an immediate PATCH request", async () => {
    const fetchMock = installFetchMock();
    const endpoint = endpointOutFixture({ name: "ep_a", enabled: true });
    const routes: Routes = {
      "GET /api/endpoints": jsonResponse(200, [endpoint]),
      "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
      "GET /api/endpoints/ep_a/secret-resolve": jsonResponse(200, secretResolveFixture()),
    };
    installApiRouter(fetchMock, routes);
    renderScreen();
    await screen.findByText("ep_a");

    let capturedBody: unknown = null;
    routes["PATCH /api/endpoints/ep_a"] = async (request: Request) => {
      capturedBody = await request.clone().json();
      return jsonResponse(200, { ...endpoint, enabled: false });
    };
    installApiRouter(fetchMock, routes);

    const user = userEvent.setup();
    await user.click(screen.getByRole("switch", { name: 'Disable endpoint "ep_a"' }));

    await waitFor(() => expect(capturedBody).toEqual({ enabled: false }));
  });

  it("deletes an endpoint after confirming, and cancels without deleting", async () => {
    const fetchMock = installFetchMock();
    const routes: Routes = {
      "GET /api/endpoints": jsonResponse(200, [endpointOutFixture({ name: "ep_a" })]),
      "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
      "GET /api/endpoints/ep_a/secret-resolve": jsonResponse(200, secretResolveFixture()),
    };
    installApiRouter(fetchMock, routes);
    renderScreen();
    await screen.findByText("ep_a");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: 'Delete endpoint "ep_a"' }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(screen.getByText("ep_a")).toBeInTheDocument();

    routes["DELETE /api/endpoints/ep_a"] = new Response(null, { status: 204 });
    installApiRouter(fetchMock, routes);
    await user.click(screen.getByRole("button", { name: 'Delete endpoint "ep_a"' }));
    const confirmDialog = await screen.findByRole("alertdialog");
    await user.click(within(confirmDialog).getByRole("button", { name: "Delete endpoint" }));

    await waitFor(() => expect(screen.queryByText("ep_a")).not.toBeInTheDocument());
    expect(screen.getByText("No endpoints registered yet")).toBeInTheDocument();
  });

  it("opening the edit form pre-fills the existing endpoint's values and locks the name", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/endpoints": jsonResponse(
        200,
        [endpointOutFixture({ name: "ep_a", role: "target", settings: { host: "acme.example" } })],
      ),
      "GET /api/connectors": jsonResponse(200, [connectorInfoFixture({ name: "fake" })]),
      "GET /api/endpoints/ep_a/secret-resolve": jsonResponse(200, secretResolveFixture()),
    });
    renderScreen();
    await screen.findByText("ep_a");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: 'Edit endpoint "ep_a"' }));
    await screen.findByRole("heading", { name: 'Edit endpoint "ep_a"' });

    expect(screen.getByLabelText("Name")).toHaveValue("ep_a");
    expect(screen.getByLabelText("Name")).toBeDisabled();
    expect(screen.getByDisplayValue("host")).toBeInTheDocument();
    expect(screen.getByDisplayValue("acme.example")).toBeInTheDocument();
  });

  it("shows a load error with a retry action when the initial fetch fails", async () => {
    const fetchMock = installFetchMock();
    const routes: Routes = {
      "GET /api/endpoints": jsonResponse(500, errorModelFixture({ code: "internal_error", message: "boom" })),
      "GET /api/connectors": jsonResponse(200, []),
    };
    installApiRouter(fetchMock, routes);
    renderScreen();

    await screen.findByText("Could not load endpoints");
    routes["GET /api/endpoints"] = jsonResponse(200, []);
    installApiRouter(fetchMock, routes);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("No endpoints registered yet");
  });
});
