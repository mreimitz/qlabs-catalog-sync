// Entering a client's credentials in the browser (amended C2), driven through the real
// `apiClient` over a stubbed `fetch` like every other test in this feature.
//
// The behaviour under test is the one the original C2 made impossible: an operator adds a
// client, types that client's credential, and the endpoint works -- no file on the host, no
// service restart. What has to stay true while that is possible is what the rest of these
// tests pin: the value goes one way only, and nothing ever renders it back.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { afterEach, describe, expect, it, vi } from "vitest";

import { installFetchMock } from "../../test/apiFixtures";
import { installApiRouter, jsonResponse, type Routes } from "./testHelpers";
import { CredentialsPanel } from "./CredentialsPanel";

const CREDENTIAL = "dapi-typed-by-the-operator-7c31";

function renderPanel(endpointName: string | null, secretFields: string[] = ["client_secret"]) {
  return render(
    <ThemeProvider defaultTheme="light">
      <CredentialsPanel endpointName={endpointName} secretFields={secretFields} />
    </ThemeProvider>,
  );
}

function storedSecret(overrides: Record<string, unknown> = {}) {
  return { field: "client_secret", is_set: false, updated_at: null, key_id: null, ...overrides };
}

describe("CredentialsPanel", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("saves a typed credential and never renders it again", async () => {
    const fetchMock = installFetchMock();
    let capturedBody: unknown = null;
    const routes: Routes = {
      "GET /api/endpoints/acme/secrets": jsonResponse(200, [storedSecret()]),
      "PUT /api/endpoints/acme/secrets/client_secret": async (request: Request) => {
        capturedBody = await request.clone().json();
        return new Response(null, { status: 204 });
      },
    };
    installApiRouter(fetchMock, routes);
    renderPanel("acme");
    const user = userEvent.setup();

    const input = await screen.findByLabelText("client_secret");
    await user.type(input, CREDENTIAL);
    routes["GET /api/endpoints/acme/secrets"] = jsonResponse(200, [
      storedSecret({ is_set: true, updated_at: "2026-08-24T10:00:00Z" }),
    ]);
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(capturedBody).toEqual({ value: CREDENTIAL }));
    // Cleared from the DOM the moment it is saved: a credential must not sit in a control
    // after the write, and an empty input is also the honest depiction of a write-only field.
    await waitFor(() => expect((input as HTMLInputElement).value).toBe(""));
    expect(document.body.textContent).not.toContain(CREDENTIAL);
  });

  it("masks the credential while it is being typed", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/endpoints/acme/secrets": jsonResponse(200, [storedSecret()]),
    });
    renderPanel("acme");

    const input = await screen.findByLabelText("client_secret");

    expect(input).toHaveAttribute("type", "password");
    // Not the browser's saved-password autofill: this field is never the operator's own
    // login, and offering to remember a tenant credential in the browser is not a favour.
    expect(input).toHaveAttribute("autocomplete", "new-password");
  });

  it("shows which fields are already saved without showing what they hold", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/endpoints/acme/secrets": jsonResponse(200, [
        storedSecret({ field: "client_secret", is_set: true, updated_at: "2026-08-24T10:00:00Z" }),
        storedSecret({ field: "token", is_set: false }),
      ]),
    });
    renderPanel("acme", ["client_secret", "token"]);

    expect(await screen.findByText(/^saved /)).toBeInTheDocument();
    expect(screen.getByText("not set")).toBeInTheDocument();
    // A saved field offers "Replace", not "Save": the operator cannot see what is stored, so
    // the button has to say what pressing it will do.
    expect(screen.getByRole("button", { name: "Replace" })).toBeInTheDocument();
  });

  it("removes a saved credential", async () => {
    const fetchMock = installFetchMock();
    let deleted = false;
    const routes: Routes = {
      "GET /api/endpoints/acme/secrets": jsonResponse(200, [
        storedSecret({ is_set: true, updated_at: "2026-08-24T10:00:00Z" }),
      ]),
      "DELETE /api/endpoints/acme/secrets/client_secret": async () => {
        deleted = true;
        routes["GET /api/endpoints/acme/secrets"] = jsonResponse(200, [storedSecret()]);
        return new Response(null, { status: 204 });
      },
    };
    installApiRouter(fetchMock, routes);
    renderPanel("acme");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Remove" }));

    await waitFor(() => expect(deleted).toBe(true));
    await waitFor(() => expect(screen.getByText("not set")).toBeInTheDocument());
  });

  it("collects credentials while registering and hands them to the form", async () => {
    // A credential cannot be sealed until its endpoint exists, so while registering the panel
    // holds what was typed instead of writing it. It must not offer its own Save button here:
    // there is one button on this form, and pressing it saves everything.
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {});
    const pending: Record<string, string>[] = [];
    render(
      <ThemeProvider defaultTheme="light">
        <CredentialsPanel
          endpointName={null}
          secretFields={["client_secret"]}
          onPendingChange={(next) => pending.push(next)}
        />
      </ThemeProvider>,
    );
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("client_secret"), "typed-while-registering");

    expect(pending.at(-1)).toEqual({ client_secret: "typed-while-registering" });
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    // Nothing is written yet -- the form does that, once, after the endpoint exists.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("surfaces a refused credential instead of pretending it saved", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/endpoints/acme/secrets": jsonResponse(200, [storedSecret()]),
      "PUT /api/endpoints/acme/secrets/client_secret": jsonResponse(503, {
        code: "master_key_unavailable",
        message: "no master key is configured, so stored credentials cannot be read or written",
        field: null,
        entity: null,
        correlation_id: null,
      }),
    });
    renderPanel("acme");
    const user = userEvent.setup();

    await user.type(await screen.findByLabelText("client_secret"), CREDENTIAL);
    await user.click(screen.getByRole("button", { name: "Save" }));

    // The deployment is missing its master key -- an operator-fixable setup problem, and the
    // one failure mode a console that stores credentials introduces. It must reach the screen.
    expect(await screen.findByText(/no master key is configured/)).toBeInTheDocument();
    expect(screen.getByText("not set")).toBeInTheDocument();
  });
});
