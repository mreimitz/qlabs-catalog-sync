// The schema-driven settings form (this task), driven by `GET /api/connectors` payloads
// RECORDED VERBATIM from a running engine (`recordedConnectors.ts`) rather than a hand-written
// belief about what `config_schema` looks like -- see that file's own doc comment for how they
// were captured, and the task brief's own warning about a fixture that differs from reality at
// exactly the point under test.
//
// Drives the REAL `EndpointsScreen` (and, through it, the real `apiClient`) via a stubbed
// `globalThis.fetch`, exactly like `EndpointsScreen.test.tsx` -- never a mock of `apiClient` or
// of this feature's own modules. Each `it` below names the mutation-table entry (from the task
// brief) it kills.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { beforeAll, describe, expect, it } from "vitest";

import { installFetchMock } from "../../test/apiFixtures";
import { EndpointsScreen } from "./EndpointsScreen";
import {
  connectorInfoFixture,
  endpointOutFixture,
  errorModelFixture,
  installApiRouter,
  jsonResponse,
  secretResolveFixture,
  type Routes,
} from "./testHelpers";
import { RECORDED_CONNECTORS, RECORDED_QLIK_CONNECTOR } from "./recordedConnectors";

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

async function openRegisterForm(connectors = RECORDED_CONNECTORS, endpoints: unknown[] = []) {
  const fetchMock = installFetchMock();
  const routes: Routes = {
    "GET /api/endpoints": jsonResponse(200, endpoints),
    "GET /api/connectors": jsonResponse(200, connectors),
  };
  installApiRouter(fetchMock, routes);
  renderScreen();
  await screen.findByRole("button", { name: "Register endpoint" });
  const user = userEvent.setup();
  await user.click(screen.getAllByRole("button", { name: "Register endpoint" })[0]!);
  await screen.findByRole("heading", { name: "Register endpoint" });
  return { user, routes, fetchMock };
}

async function selectConnector(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole("combobox", { name: "Connector" }));
  await user.click(await screen.findByRole("option", { name }));
}

describe("EndpointsScreen -- schema-driven settings form (recorded config_schema)", () => {
  it(
    "MUTATION #1 -- generates a labelled field per qlik property, marks the three required ones, " +
      "and keeps the secret-typed client_secret out of the SETTINGS form",
    async () => {
      const { user } = await openRegisterForm();
      await selectConnector(user, "qlik");

      expect(await screen.findByLabelText("Base Url")).toBeRequired();
      expect(screen.getByLabelText("Client Id")).toBeRequired();
      expect(screen.getByLabelText("Space Id")).toBeRequired();
      // `scope` is optional (not in the real schema's `required`) and nullable.
      expect(screen.getByLabelText("Scope")).not.toBeRequired();

      // A credential IS typed here now (amended C2) -- but in the credentials panel, which
      // encrypts it, and never as an ordinary setting, which would be stored in the clear.
      // That distinction is the whole point, so it is asserted structurally: the control named
      // after the secret field exists, is masked, and is NOT one of the settings inputs.
      const credential = screen.getByLabelText("client_secret");
      expect(credential).toHaveAttribute("type", "password");
      expect(screen.getByLabelText("Base Url")).toHaveAttribute("type", "text");
      expect(
        screen.queryAllByPlaceholderText("value").some((row) => row === credential),
      ).toBe(false);
    },
  );

  it(
    "MUTATION #5 -- a connector with no config_schema (collibra: broken, and any connector " +
      "whose ConfigModel could not produce one) still gets a usable settings editor, never a blank/no editor",
    async () => {
      const { user } = await openRegisterForm([
        ...RECORDED_CONNECTORS,
        connectorInfoFixture({ name: "no_schema_connector", config_schema: null, config_secret_fields: [] }),
      ]);
      await selectConnector(user, "no_schema_connector");

      // The fallback generic key/value editor, not an empty region: a settings row is already
      // there to fill in (`EndpointFormSheet.tsx`'s `seedSettingsState` seeds one blank row for
      // a fresh create, exactly like it always has), and the operator can add more.
      expect(screen.getByPlaceholderText("setting_name")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Add setting" })).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Add setting" }));
      expect(screen.getAllByPlaceholderText("setting_name")).toHaveLength(2);
    },
  );

  it("MUTATION #4 -- Submit stays disabled until every schema-required field is filled, matching what the server enforces", async () => {
    const { user } = await openRegisterForm();
    await user.type(screen.getByLabelText("Name"), "qlik_prod");
    await selectConnector(user, "qlik");
    await user.click(screen.getByRole("combobox", { name: "Role" }));
    await user.click(await screen.findByRole("option", { name: "target" }));

    const submit = screen.getByRole("button", { name: "Register endpoint" });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("Base Url"), "https://acme.eu.qlikcloud.com");
    await user.type(screen.getByLabelText("Client Id"), "abc123");
    expect(submit).toBeDisabled(); // space_id still missing

    await user.type(screen.getByLabelText("Space Id"), "space-1");
    expect(submit).toBeEnabled();
  });

  it("MUTATION #7 -- registering with only the required qlik fields never sends the untouched 'scope' default", async () => {
    const { user, routes, fetchMock } = await openRegisterForm();
    await user.type(screen.getByLabelText("Name"), "qlik_prod");
    await selectConnector(user, "qlik");
    await user.click(screen.getByRole("combobox", { name: "Role" }));
    await user.click(await screen.findByRole("option", { name: "target" }));
    await user.type(screen.getByLabelText("Base Url"), "https://acme.eu.qlikcloud.com");
    await user.type(screen.getByLabelText("Client Id"), "abc123");
    await user.type(screen.getByLabelText("Space Id"), "space-1");

    let capturedBody: { settings?: Record<string, unknown> } | null = null;
    routes["POST /api/endpoints"] = async (request: Request) => {
      capturedBody = await request.clone().json();
      return jsonResponse(
        201,
        endpointOutFixture({
          name: "qlik_prod",
          connector: "qlik",
          role: "target",
          settings: {
            base_url: "https://acme.eu.qlikcloud.com",
            client_id: "abc123",
            space_id: "space-1",
          },
        }),
      );
    };
    routes["GET /api/endpoints/qlik_prod/secret-resolve"] = jsonResponse(200, secretResolveFixture());
    installApiRouter(fetchMock, routes);

    await user.click(screen.getByRole("button", { name: "Register endpoint" }));
    await waitFor(() => expect(capturedBody).not.toBeNull());
    expect(capturedBody!.settings).toEqual({
      base_url: "https://acme.eu.qlikcloud.com",
      client_id: "abc123",
      space_id: "space-1",
    });
    expect(capturedBody!.settings).not.toHaveProperty("scope");
  });

  it("registering a databricks endpoint with only required fields never sends the untouched nullable/array defaults", async () => {
    const { user, routes, fetchMock } = await openRegisterForm();
    await user.type(screen.getByLabelText("Name"), "databricks_prod");
    await selectConnector(user, "databricks");
    await user.click(screen.getByRole("combobox", { name: "Role" }));
    await user.click(await screen.findByRole("option", { name: "source" }));
    await user.type(screen.getByLabelText("Host"), "https://adb-123.7.azuredatabricks.net");
    await user.type(screen.getByLabelText("Client Id"), "sp-123");

    let capturedBody: { settings?: Record<string, unknown> } | null = null;
    routes["POST /api/endpoints"] = async (request: Request) => {
      capturedBody = await request.clone().json();
      return jsonResponse(
        201,
        endpointOutFixture({
          name: "databricks_prod",
          connector: "databricks",
          role: "source",
          settings: { host: "https://adb-123.7.azuredatabricks.net", client_id: "sp-123" },
        }),
      );
    };
    routes["GET /api/endpoints/databricks_prod/secret-resolve"] = jsonResponse(200, secretResolveFixture());
    installApiRouter(fetchMock, routes);

    await user.click(screen.getByRole("button", { name: "Register endpoint" }));
    await waitFor(() => expect(capturedBody).not.toBeNull());
    expect(capturedBody!.settings).toEqual({
      host: "https://adb-123.7.azuredatabricks.net",
      client_id: "sp-123",
    });
  });

  it("the array-of-strings control (catalog_schema_patterns) submits what the operator actually typed", async () => {
    const { user, routes, fetchMock } = await openRegisterForm();
    await user.type(screen.getByLabelText("Name"), "databricks_prod");
    await selectConnector(user, "databricks");
    await user.click(screen.getByRole("combobox", { name: "Role" }));
    await user.click(await screen.findByRole("option", { name: "source" }));
    await user.type(screen.getByLabelText("Host"), "https://adb-123.7.azuredatabricks.net");
    await user.type(screen.getByLabelText("Client Id"), "sp-123");
    await user.type(screen.getByLabelText("Catalog Schema Patterns"), "analytics.*{enter}");

    let capturedBody: { settings?: Record<string, unknown> } | null = null;
    routes["POST /api/endpoints"] = async (request: Request) => {
      capturedBody = await request.clone().json();
      return jsonResponse(201, endpointOutFixture({ name: "databricks_prod", connector: "databricks", role: "source" }));
    };
    routes["GET /api/endpoints/databricks_prod/secret-resolve"] = jsonResponse(200, secretResolveFixture());
    installApiRouter(fetchMock, routes);

    await user.click(screen.getByRole("button", { name: "Register endpoint" }));
    await waitFor(() => expect(capturedBody).not.toBeNull());
    expect(capturedBody!.settings?.catalog_schema_patterns).toEqual(["analytics.*"]);
  });

  it(
    "MUTATION #2 -- a schema property whose shape this form does not support degrades to a raw " +
      "text control instead of being dropped, and its value round-trips unedited",
    async () => {
      // Synthetic: no real connector declares an `object`-typed setting today. This is what
      // proves the generic degrade path works at all, independent of what databricks/qlik
      // happen to ship -- see configSchemaForm.test.ts's own synthetic-shape tests for the
      // pure-logic half of this same proof.
      const weirdConnector = connectorInfoFixture({
        name: "weird",
        config_schema: {
          properties: {
            advanced_tuning: {
              type: "object",
              title: "Advanced Tuning",
              description: "Free-form, connector-specific tuning knobs.",
            },
          },
          required: [],
        },
        config_secret_fields: [],
      });
      const { user, routes, fetchMock } = await openRegisterForm([weirdConnector]);
      await user.type(screen.getByLabelText("Name"), "weird_ep");
      await selectConnector(user, "weird");
      await user.click(screen.getByRole("combobox", { name: "Role" }));
      await user.click(await screen.findByRole("option", { name: "source" }));

      const advancedField = await screen.findByLabelText("Advanced Tuning");
      expect(advancedField).toBeInTheDocument();
      // user-event's `.type()` treats `{` as special-key syntax -- `{{` is its own escape for a
      // literal `{` (https://testing-library.com/docs/user-event/keyboard). A bare `}` (no
      // unclosed `{` before it) is already literal and needs no escaping.
      await user.type(advancedField, '{{"retries":3}');

      let capturedBody: { settings?: Record<string, unknown> } | null = null;
      routes["POST /api/endpoints"] = async (request: Request) => {
        capturedBody = await request.clone().json();
        return jsonResponse(201, endpointOutFixture({ name: "weird_ep", connector: "weird", role: "source" }));
      };
      routes["GET /api/endpoints/weird_ep/secret-resolve"] = jsonResponse(200, secretResolveFixture());
      installApiRouter(fetchMock, routes);

      await user.click(screen.getByRole("button", { name: "Register endpoint" }));
      await waitFor(() => expect(capturedBody).not.toBeNull());
      expect(capturedBody!.settings).toEqual({ advanced_tuning: { retries: 3 } });
    },
  );

  it(
    "MUTATION #3 -- a stored setting the current schema no longer describes is shown under " +
      "'Additional settings' and is still sent unchanged on save",
    async () => {
      const fetchMock = installFetchMock();
      const endpoint = endpointOutFixture({
        name: "qlik_prod",
        connector: "qlik",
        role: "target",
        settings: {
          base_url: "https://acme.eu.qlikcloud.com",
          client_id: "abc123",
          space_id: "space-1",
          legacy_flag: true, // not in the real qlik schema -- simulates a removed/renamed field
        },
      });
      const routes: Routes = {
        "GET /api/endpoints": jsonResponse(200, [endpoint]),
        "GET /api/connectors": jsonResponse(200, RECORDED_CONNECTORS),
        "GET /api/endpoints/qlik_prod/secret-resolve": jsonResponse(200, secretResolveFixture()),
      };
      installApiRouter(fetchMock, routes);
      renderScreen();
      await screen.findByText("qlik_prod");

      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: 'Edit endpoint "qlik_prod"' }));
      await screen.findByRole("heading", { name: 'Edit endpoint "qlik_prod"' });

      // The known fields render as typed controls, pre-filled...
      expect(screen.getByLabelText("Base Url")).toHaveValue("https://acme.eu.qlikcloud.com");
      // ...and the unknown one is shown, not silently absorbed.
      expect(screen.getByText("Additional settings")).toBeInTheDocument();
      expect(screen.getByDisplayValue("legacy_flag")).toBeInTheDocument();

      let capturedBody: { settings?: Record<string, unknown> } | null = null;
      routes["PATCH /api/endpoints/qlik_prod"] = async (request: Request) => {
        capturedBody = await request.clone().json();
        return jsonResponse(200, { ...endpoint });
      };
      installApiRouter(fetchMock, routes);

      await user.click(screen.getByRole("button", { name: "Save changes" }));
      await waitFor(() => expect(capturedBody).not.toBeNull());
      expect(capturedBody!.settings).toMatchObject({ legacy_flag: true });
    },
  );

  it("an untouched, already-stored optional field survives an edit save unchanged (not silently reset to its default)", async () => {
    const fetchMock = installFetchMock();
    const endpoint = endpointOutFixture({
      name: "databricks_prod",
      connector: "databricks",
      role: "source",
      settings: {
        host: "https://adb-123.7.azuredatabricks.net",
        client_id: "sp-123",
        sql_warehouse_id: "wh-already-set",
      },
    });
    const routes: Routes = {
      "GET /api/endpoints": jsonResponse(200, [endpoint]),
      "GET /api/connectors": jsonResponse(200, RECORDED_CONNECTORS),
      "GET /api/endpoints/databricks_prod/secret-resolve": jsonResponse(200, secretResolveFixture()),
    };
    installApiRouter(fetchMock, routes);
    renderScreen();
    await screen.findByText("databricks_prod");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: 'Edit endpoint "databricks_prod"' }));
    await screen.findByRole("heading", { name: 'Edit endpoint "databricks_prod"' });
    expect(screen.getByLabelText("Sql Warehouse Id")).toHaveValue("wh-already-set");

    let capturedBody: { settings?: Record<string, unknown> } | null = null;
    routes["PATCH /api/endpoints/databricks_prod"] = async (request: Request) => {
      capturedBody = await request.clone().json();
      return jsonResponse(200, { ...endpoint });
    };
    installApiRouter(fetchMock, routes);

    // Touch nothing settings-related -- only flip Enabled, which is unrelated.
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(capturedBody).not.toBeNull());
    expect(capturedBody!.settings).toMatchObject({ sql_warehouse_id: "wh-already-set" });
  });

  it("MUTATION #1b -- a legacy stored value under a secret-typed field's own name never renders, even pre-filled from an existing endpoint", async () => {
    const fetchMock = installFetchMock();
    // Defensive/theoretical: C2 means the server has never accepted an inline secret into
    // `settings`, so this specific stored state should not arise in practice -- but the UI must
    // still never surface it if it somehow does (belt-and-braces over trusting the server's own
    // invariant to always have held).
    const endpoint = endpointOutFixture({
      name: "qlik_prod",
      connector: "qlik",
      role: "target",
      settings: {
        base_url: "https://acme.eu.qlikcloud.com",
        client_id: "abc123",
        space_id: "space-1",
        client_secret: "leaked-value",
      },
    });
    installApiRouter(fetchMock, {
      "GET /api/endpoints": jsonResponse(200, [endpoint]),
      "GET /api/connectors": jsonResponse(200, RECORDED_CONNECTORS),
      "GET /api/endpoints/qlik_prod/secret-resolve": jsonResponse(200, secretResolveFixture()),
    });
    renderScreen();
    await screen.findByText("qlik_prod");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: 'Edit endpoint "qlik_prod"' }));
    await screen.findByRole("heading", { name: 'Edit endpoint "qlik_prod"' });

    expect(screen.queryByText("leaked-value")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("leaked-value")).not.toBeInTheDocument();
    // The credentials panel (amended C2) does render masked inputs here -- and every one of
    // them is EMPTY. That is the stronger statement now that credentials can be stored: no
    // saved or leaked credential is ever pre-filled into a control, so nothing can be read
    // back out of the DOM, whatever the server happened to send.
    const masked = Array.from(document.querySelectorAll<HTMLInputElement>('input[type="password"]'));
    expect(masked.every((input) => input.value === "")).toBe(true);
  });

  it(
    "MUTATION #6 -- a server field error against an 'Additional settings' row attaches to that " +
      "row in the schema-driven form too, not only a banner",
    async () => {
      const { user, routes, fetchMock } = await openRegisterForm([RECORDED_QLIK_CONNECTOR]);
      await user.type(screen.getByLabelText("Name"), "qlik_prod");
      await selectConnector(user, "qlik");
      await user.click(screen.getByRole("combobox", { name: "Role" }));
      await user.click(await screen.findByRole("option", { name: "target" }));
      await user.type(screen.getByLabelText("Base Url"), "https://acme.eu.qlikcloud.com");
      await user.type(screen.getByLabelText("Client Id"), "abc123");
      await user.type(screen.getByLabelText("Space Id"), "space-1");

      // client_secret never has a control (MUTATION #1) -- but the freeform "Additional
      // settings" editor still lets an operator type ANY key, exactly like before this task
      // (`SettingsEditor.tsx`'s own doc comment); the safety net is server rejection with a
      // correctly-attributed error, proven here.
      await user.click(screen.getByRole("button", { name: "Add setting" }));
      const keyInputs = screen.getAllByPlaceholderText("setting_name");
      await user.type(keyInputs[keyInputs.length - 1]!, "client_secret");
      const valueInputs = screen.getAllByPlaceholderText("value");
      await user.type(valueInputs[valueInputs.length - 1]!, "sk-should-never-be-sent");

      routes["POST /api/endpoints"] = jsonResponse(
        422,
        errorModelFixture({
          code: "inline_secret_rejected",
          message: "connector 'qlik' settings must not carry secret-typed field(s) 'client_secret' inline",
          field: "client_secret",
          entity: "qlik",
        }),
      );
      installApiRouter(fetchMock, routes);
      await user.click(screen.getByRole("button", { name: "Register endpoint" }));

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("must not carry secret-typed field");
      const row = keyInputs[keyInputs.length - 1]!.closest("div")?.parentElement;
      expect(row).toBeTruthy();
      expect(within(row as HTMLElement).getByRole("alert")).toBe(alert);
      expect(screen.getAllByText(/must not carry secret-typed field/)).toHaveLength(1);
    },
  );
});
