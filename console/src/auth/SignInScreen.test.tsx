import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { beforeEach, describe, expect, it } from "vitest";

import { errorModelFixture, installFetchMock, jsonResponse, sessionInfoFixture } from "../test/apiFixtures";
import { getSessionState, setSignedOut } from "./sessionStore";
import { SignInScreen } from "./SignInScreen";

function renderSignIn() {
  return render(
    <ThemeProvider defaultTheme="light">
      <SignInScreen />
    </ThemeProvider>,
  );
}

describe("SignInScreen", () => {
  beforeEach(() => {
    setSignedOut();
  });

  it("has a real, labeled form with username and password fields", () => {
    renderSignIn();

    expect(screen.getByRole("textbox", { name: "Username" })).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("submits the typed credentials to POST /api/auth/session and signs in on success", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, sessionInfoFixture({ username: "admin" })));
    renderSignIn();

    await user.type(screen.getByRole("textbox", { name: "Username" }), "admin");
    await user.type(screen.getByLabelText("Password"), "the-real-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(getSessionState().status).toBe("signed-in"));
    const request = fetchMock.mock.calls[0][0] as Request;
    expect(request.method).toBe("POST");
    expect(JSON.parse(await request.clone().text())).toEqual({
      username: "admin",
      password: "the-real-password",
    });
  });

  it("shows ONE generic failure message -- never a field-specific 'wrong username' or 'wrong password'", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(
      jsonResponse(401, errorModelFixture({ code: "invalid_credentials", message: "invalid username or password" })),
    );
    renderSignIn();

    await user.type(screen.getByRole("textbox", { name: "Username" }), "admin");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("invalid username or password");
    // The message renders once, not attached to either individual field.
    expect(screen.getAllByText("invalid username or password")).toHaveLength(1);
    expect(getSessionState().status).toBe("signed-out");
  });

  it("disables the submit button while the request is in flight, and keeps it enabled beforehand", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    let resolveFetch!: (value: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>((resolve) => (resolveFetch = resolve)));
    renderSignIn();

    const submit = screen.getByRole("button", { name: "Sign in" });
    expect(submit).toBeEnabled();

    await user.type(screen.getByRole("textbox", { name: "Username" }), "admin");
    await user.type(screen.getByLabelText("Password"), "the-real-password");
    await user.click(submit);

    await waitFor(() => expect(screen.getByRole("button", { name: "Signing in…" })).toBeDisabled());

    resolveFetch(jsonResponse(200, sessionInfoFixture()));
    await waitFor(() => expect(getSessionState().status).toBe("signed-in"));
  });
});
