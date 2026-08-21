// Drives the REAL `PairsScreen` (and, through it, the real `apiClient`) via a stubbed
// `globalThis.fetch` (`installFetchMock`, `../../test/apiFixtures.ts`) and this feature's own
// `installApiRouter` (`./testHelpers.ts`) -- never a mock of `apiClient` or this feature's own
// modules. Every fixture is built from the real shapes in `../../api/generated/schema.ts` via
// `./testHelpers.ts`'s fixture builders.
//
// The task brief's mutation table drives most of these tests directly -- each `it` below
// names which mutation it kills in its own description.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { toast } from "@elabs-ai/components-ui";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { installFetchMock } from "../../test/apiFixtures";
import { PairsScreen } from "./PairsScreen";
import {
  endpointOutFixture,
  errorModelFixture,
  healthOutFixture,
  installApiRouter,
  jsonResponse,
  syncPairOutFixture,
  type Routes,
} from "./testHelpers";

// Radix Select/Switch/Checkbox need a few DOM APIs jsdom does not implement (pointer capture,
// scrollIntoView) -- see `../endpoints/EndpointsScreen.test.tsx`'s identical block for why.
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

async function openCreateForm() {
  const fetchMock = installFetchMock();
  installApiRouter(fetchMock, {
    "GET /api/pairs": jsonResponse(200, []),
    "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP, DISABLED_SOURCE_EP]),
  });
  renderScreen();
  await screen.findByText("No sync pairs configured yet");
  const user = userEvent.setup();
  await user.click(screen.getAllByRole("button", { name: "New sync pair" })[0]!);
  await screen.findByRole("heading", { name: "New sync pair" });
  return user;
}

describe("PairsScreen", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("loads and lists configured pairs with their entity types, cadence and activation state", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [
        syncPairOutFixture({ name: "pair_one", activation_opt_in: true }),
      ]),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    });
    renderScreen();

    await screen.findByText("pair_one");
    expect(screen.getByText("Data product")).toBeInTheDocument();
    expect(screen.getByText("Dataset")).toBeInTheDocument();
    expect(screen.getByText("900s")).toBeInTheDocument();
    expect(screen.getByText("Discoverable tenant-wide")).toBeInTheDocument();
  });

  it("shows a load error with a retry action when the initial fetch fails", async () => {
    const fetchMock = installFetchMock();
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(500, errorModelFixture({ code: "internal_error", message: "boom" })),
      "GET /api/endpoints": jsonResponse(200, []),
    };
    installApiRouter(fetchMock, routes);
    renderScreen();

    await screen.findByText("Could not load sync pairs");
    routes["GET /api/pairs"] = jsonResponse(200, []);
    installApiRouter(fetchMock, routes);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("No sync pairs configured yet");
  });

  // --------------------------------------------------------------------------------------
  // MUTATION #1 -- activation defaults to OFF on a fresh create form.
  // --------------------------------------------------------------------------------------
  it("MUTATION #1 -- a new pair's activation switch and enabled switch both default to off", async () => {
    await openCreateForm();
    const activationSwitch = screen.getByRole("switch", { name: "Activate in Qlik" });
    const enabledSwitch = screen.getByRole("switch", { name: "Enabled" });
    expect(activationSwitch).not.toBeChecked();
    expect(enabledSwitch).not.toBeChecked();
  });

  // --------------------------------------------------------------------------------------
  // MUTATION #2 -- the activation switch states its consequence on its own surface, tied to
  // it via aria-describedby, not only in a tooltip or a help page.
  // --------------------------------------------------------------------------------------
  it("MUTATION #2 -- the activation switch is described by text stating it makes the product discoverable tenant-wide", async () => {
    await openCreateForm();
    const activationSwitch = screen.getByRole("switch", { name: "Activate in Qlik" });
    const describedBy = activationSwitch.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    const describedByIds = describedBy!.split(" ");
    const describedText = describedByIds
      .map((id) => document.getElementById(id)?.textContent ?? "")
      .join(" ");
    expect(describedText).toContain("discoverable tenant-wide");
    expect(describedText).toMatch(/off by default/i);
  });

  // --------------------------------------------------------------------------------------
  // MUTATION #3 -- a disabled endpoint is never offered in the source or target picker, but
  // its existence (and why it is excluded) is never silently hidden either.
  // --------------------------------------------------------------------------------------
  it("MUTATION #3 -- a disabled endpoint is not offered as a source option, and the picker says why", async () => {
    const user = await openCreateForm();
    await user.click(screen.getByRole("combobox", { name: "Source endpoint" }));
    expect(await screen.findByRole("option", { name: /databricks_prod/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /disabled_source_ep/ })).not.toBeInTheDocument();
    await user.keyboard("{Escape}");

    // Not hidden entirely -- named, with the reason and where to fix it.
    expect(screen.getByText(/disabled_source_ep/)).toBeInTheDocument();
    expect(screen.getByText(/disabled and not shown/)).toBeInTheDocument();
  });

  // --------------------------------------------------------------------------------------
  // MUTATION #4 -- Qlik (or any write-role endpoint) is never offered as a source, and a
  // source-role endpoint is never offered as a target -- the upstream-only guardrail.
  // --------------------------------------------------------------------------------------
  it("MUTATION #4 -- the target-role (Qlik) endpoint is never offered in the source picker, and vice versa", async () => {
    const user = await openCreateForm();

    await user.click(screen.getByRole("combobox", { name: "Source endpoint" }));
    expect(await screen.findByRole("option", { name: /databricks_prod/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /qlik_prod/ })).not.toBeInTheDocument();
    await user.keyboard("{Escape}");

    await user.click(screen.getByRole("combobox", { name: "Target endpoint" }));
    expect(await screen.findByRole("option", { name: /qlik_prod/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /databricks_prod/ })).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
  });

  // --------------------------------------------------------------------------------------
  // MUTATION #5 -- disabling a pair PATCHes `enabled: false`; it never deletes the pair, and
  // the row (and its configuration) survives.
  // --------------------------------------------------------------------------------------
  it("MUTATION #5 -- toggling the Enabled switch off sends a PATCH, never a DELETE, and keeps the pair in the list", async () => {
    const fetchMock = installFetchMock();
    const pair = syncPairOutFixture({ enabled: true });
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(200, [pair]),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    };
    const router = installApiRouter(fetchMock, routes);
    renderScreen();
    await screen.findByText(pair.name);

    let capturedBody: unknown = null;
    routes[`PATCH /api/pairs/${pair.id}`] = async (request: Request) => {
      capturedBody = await request.clone().json();
      return jsonResponse(200, { ...pair, enabled: false });
    };
    installApiRouter(fetchMock, routes);

    const user = userEvent.setup();
    await user.click(screen.getByRole("switch", { name: `Disable sync pair "${pair.name}"` }));

    await waitFor(() => expect(capturedBody).toEqual({ enabled: false }));
    expect(router.calls).not.toContain(`DELETE /api/pairs/${pair.id}`);
    // Still in the list -- disabling is not deleting.
    expect(screen.getByText(pair.name)).toBeInTheDocument();
  });

  it("deletes a pair after confirming, and cancels without deleting", async () => {
    const fetchMock = installFetchMock();
    const pair = syncPairOutFixture();
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(200, [pair]),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    };
    installApiRouter(fetchMock, routes);
    renderScreen();
    await screen.findByText(pair.name);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: `Delete sync pair "${pair.name}"` }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/selection rules and per-object overrides are deleted/);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(screen.getByText(pair.name)).toBeInTheDocument();

    routes[`DELETE /api/pairs/${pair.id}`] = new Response(null, { status: 204 });
    installApiRouter(fetchMock, routes);
    await user.click(screen.getByRole("button", { name: `Delete sync pair "${pair.name}"` }));
    const confirmDialog = await screen.findByRole("alertdialog");
    await user.click(within(confirmDialog).getByRole("button", { name: "Delete sync pair" }));

    await waitFor(() => expect(screen.queryByText(pair.name)).not.toBeInTheDocument());
    expect(screen.getByText("No sync pairs configured yet")).toBeInTheDocument();
  });

  // --------------------------------------------------------------------------------------
  // MUTATION #6 -- a validation error attaches to the offending field, never only a banner.
  // --------------------------------------------------------------------------------------
  it("MUTATION #6 -- a name-conflict error (sync_pair_already_exists) attaches to the Name field, not a generic banner", async () => {
    const fetchMock = installFetchMock();
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(200, []),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    };
    installApiRouter(fetchMock, routes);
    renderScreen();
    const user = userEvent.setup();
    await screen.findByText("No sync pairs configured yet");
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
    installApiRouter(fetchMock, routes);
    await user.click(screen.getByRole("button", { name: "Create sync pair" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("already exists");
    expect(screen.getAllByText(/already exists/)).toHaveLength(1);
    const nameInput = screen.getByLabelText("Name");
    expect(nameInput).toHaveAttribute("aria-describedby", alert.id);
  });

  it("a sync_pair_endpoint_invalid failure (no field/entity) renders as a form banner, not attached to any single field", async () => {
    const fetchMock = installFetchMock();
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(200, []),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    };
    installApiRouter(fetchMock, routes);
    renderScreen();
    const user = userEvent.setup();
    await screen.findByText("No sync pairs configured yet");
    await user.click(screen.getAllByRole("button", { name: "New sync pair" })[0]!);
    await screen.findByRole("heading", { name: "New sync pair" });

    await user.type(screen.getByLabelText("Name"), "new_pair");
    await user.click(screen.getByRole("combobox", { name: "Source endpoint" }));
    await user.click(await screen.findByRole("option", { name: /databricks_prod/ }));
    await user.click(screen.getByRole("combobox", { name: "Target endpoint" }));
    await user.click(await screen.findByRole("option", { name: /qlik_prod/ }));
    await user.type(screen.getByLabelText("Target Qlik space"), "analytics/finance");

    routes["POST /api/pairs"] = jsonResponse(
      422,
      errorModelFixture({
        code: "sync_pair_endpoint_invalid",
        message: "sync pair 'new_pair': target endpoint 'qlik_prod' is registered but disabled",
      }),
    );
    installApiRouter(fetchMock, routes);
    await user.click(screen.getByRole("button", { name: "Create sync pair" }));

    await screen.findByText(/registered but disabled/);
    // Not attached as a field-level alert on any control -- no control has aria-describedby
    // pointing at it, only the top-of-form banner exists.
    expect(screen.getAllByText(/registered but disabled/)).toHaveLength(1);
  });

  // --------------------------------------------------------------------------------------
  // MUTATION #7 -- a red healthcheck run from the picker is a calm status badge, never an
  // error toast; and it is never fetched automatically for every endpoint on open.
  // --------------------------------------------------------------------------------------
  it("MUTATION #7 -- healthcheck is never fetched automatically for a listed endpoint when the form opens", async () => {
    const fetchMock = installFetchMock();
    const router = installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, []),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    });
    renderScreen();
    const user = userEvent.setup();
    await screen.findByText("No sync pairs configured yet");
    await user.click(screen.getAllByRole("button", { name: "New sync pair" })[0]!);
    await screen.findByRole("heading", { name: "New sync pair" });

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(router.calls.some((call) => call.endsWith("/healthcheck"))).toBe(false);
  });

  it("MUTATION #7 -- an unhealthy result from the picker's Check health action renders as a status badge, never an error toast", async () => {
    const user = await openCreateForm();
    await user.click(screen.getByRole("combobox", { name: "Source endpoint" }));
    await user.click(await screen.findByRole("option", { name: /databricks_prod/ }));

    const toastErrorSpy = vi.spyOn(toast, "error");
    const toastSuccessSpy = vi.spyOn(toast, "success");

    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, []),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP, DISABLED_SOURCE_EP]),
      "POST /api/endpoints/databricks_prod/healthcheck": jsonResponse(
        200,
        healthOutFixture({ endpoint: "databricks_prod", state: "unhealthy", reason: "connection refused" }),
      ),
    });

    await user.click(screen.getByRole("button", { name: 'Run healthcheck for "databricks_prod"' }));

    await screen.findByText("unhealthy");
    expect(toastErrorSpy).not.toHaveBeenCalled();
    expect(toastSuccessSpy).not.toHaveBeenCalled();
  });

  it("a genuine request failure while healthchecking from the picker DOES toast an error", async () => {
    const user = await openCreateForm();
    await user.click(screen.getByRole("combobox", { name: "Source endpoint" }));
    await user.click(await screen.findByRole("option", { name: /databricks_prod/ }));

    const toastErrorSpy = vi.spyOn(toast, "error");
    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, []),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP, DISABLED_SOURCE_EP]),
      "POST /api/endpoints/databricks_prod/healthcheck": jsonResponse(
        422,
        errorModelFixture({ code: "connector_lookup_error", message: "connector 'databricks' is broken" }),
      ),
    });

    await user.click(screen.getByRole("button", { name: 'Run healthcheck for "databricks_prod"' }));

    await waitFor(() => expect(toastErrorSpy).toHaveBeenCalledTimes(1));
    expect(toastErrorSpy.mock.calls[0]?.[0]).toContain("connector 'databricks' is broken");
  });

  // --------------------------------------------------------------------------------------
  // End-to-end sanity: create a pair with entity types, cadence, jitter and a per-entity
  // manual-edit override, entirely from the browser.
  // --------------------------------------------------------------------------------------
  it("creates a new sync pair end to end with the exact request body, and shows it in the list", async () => {
    const user = await openCreateForm();

    await user.type(screen.getByLabelText("Name"), "new_pair");
    await user.click(screen.getByRole("combobox", { name: "Source endpoint" }));
    await user.click(await screen.findByRole("option", { name: /databricks_prod/ }));
    await user.click(screen.getByRole("combobox", { name: "Target endpoint" }));
    await user.click(await screen.findByRole("option", { name: /qlik_prod/ }));
    await user.type(screen.getByLabelText("Target Qlik space"), "analytics/finance");
    await user.click(screen.getByRole("checkbox", { name: "Data product" }));

    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    let capturedBody: unknown = null;
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(200, []),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP, DISABLED_SOURCE_EP]),
      "POST /api/pairs": async (request: Request) => {
        capturedBody = await request.clone().json();
        return jsonResponse(
          201,
          syncPairOutFixture({
            name: "new_pair",
            source: "databricks_prod",
            target: "qlik_prod",
            target_space: "analytics/finance",
            entity_types: ["data_product"],
          }),
        );
      },
    };
    installApiRouter(fetchMock, routes);

    const toastSuccessSpy = vi.spyOn(toast, "success");
    await user.click(screen.getByRole("button", { name: "Create sync pair" }));

    await screen.findByText("new_pair");
    expect(screen.queryByRole("heading", { name: "New sync pair" })).not.toBeInTheDocument();
    expect(toastSuccessSpy).toHaveBeenCalledWith(expect.stringContaining("new_pair"));
    expect(capturedBody).toMatchObject({
      name: "new_pair",
      source: "databricks_prod",
      target: "qlik_prod",
      target_space: "analytics/finance",
      entity_types: ["data_product"],
      cadence_seconds: 900,
      jitter_seconds: null,
      manual_edit_policy: { default: "source_wins" },
      activation_opt_in: false,
      enabled: false,
    });
    // No stray `per_entity` key when nothing was overridden.
    expect((capturedBody as { manual_edit_policy: object }).manual_edit_policy).not.toHaveProperty("per_entity");
  });

  it("opening the edit form pre-fills every field from the existing pair, including a per-entity override", async () => {
    const fetchMock = installFetchMock();
    const pair = syncPairOutFixture({
      manual_edit_policy: { default: "source_wins", per_entity: { dataset: "preserve_local" } },
      activation_opt_in: true,
      enabled: true,
      jitter_seconds: 30,
    });
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [pair]),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    });
    renderScreen();
    await screen.findByText(pair.name);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: `Edit sync pair "${pair.name}"` }));
    await screen.findByRole("heading", { name: `Edit sync pair "${pair.name}"` });

    expect(screen.getByLabelText("Name")).toHaveValue(pair.name);
    expect(screen.getByLabelText("Target Qlik space")).toHaveValue(pair.target_space);
    expect(screen.getByRole("checkbox", { name: "Data product" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Dataset" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Category" })).not.toBeChecked();
    expect(screen.getByRole("switch", { name: "Activate in Qlik" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Enabled" })).toBeChecked();
    // The dataset override loaded as "preserve local edits"; data_product has no override, so
    // its row still reads "use default". Scoped to each override row's own combobox (by role)
    // rather than a page-wide text search, since Radix renders the same label text again in
    // hidden `<option>`s for native form semantics.
    expect(screen.getByRole("combobox", { name: "Dataset" })).toHaveTextContent("Preserve local edits");
    expect(screen.getByRole("combobox", { name: "Data product" })).toHaveTextContent(
      "Use default (Source wins)",
    );
  });

  // --------------------------------------------------------------------------------------
  // This form renders no `per_field` editor (see `ManualEditPolicyEditor.tsx`'s doc
  // comment), but `manual_edit_policy` is replaced as a whole object by
  // `SyncPairUpdateRequest`, not deep-merged -- so saving any other change through this form
  // must not silently drop a `per_field` override that already existed on the pair.
  // --------------------------------------------------------------------------------------
  it("saving an edit preserves an existing per_field override this form does not expose editing", async () => {
    const fetchMock = installFetchMock();
    const pair = syncPairOutFixture({
      manual_edit_policy: {
        default: "source_wins",
        per_field: { "dataset.owner": "preserve_local" },
      },
    });
    const routes: Routes = {
      "GET /api/pairs": jsonResponse(200, [pair]),
      "GET /api/endpoints": jsonResponse(200, [SOURCE_EP, TARGET_EP]),
    };
    installApiRouter(fetchMock, routes);
    renderScreen();
    await screen.findByText(pair.name);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: `Edit sync pair "${pair.name}"` }));
    await screen.findByRole("heading", { name: `Edit sync pair "${pair.name}"` });

    let capturedBody: unknown = null;
    routes[`PATCH /api/pairs/${pair.id}`] = async (request: Request) => {
      capturedBody = await request.clone().json();
      return jsonResponse(200, pair);
    };
    installApiRouter(fetchMock, routes);

    // Change something unrelated, then save -- the point is that per_field survives a save
    // this form had no UI to touch it through.
    await user.clear(screen.getByLabelText("Target Qlik space"));
    await user.type(screen.getByLabelText("Target Qlik space"), "analytics/other");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(capturedBody).not.toBeNull());
    expect(
      (capturedBody as { manual_edit_policy: { per_field?: Record<string, string> } }).manual_edit_policy
        .per_field,
    ).toEqual({ "dataset.owner": "preserve_local" });
  });
});
