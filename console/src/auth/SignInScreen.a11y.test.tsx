// Named *.a11y.test.tsx beside the screen it covers (console/CLAUDE.md's load-bearing
// naming convention -- `pnpm a11y` runs `vitest run a11y`, a path-substring filter).
import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import axe from "axe-core";
import { beforeEach, describe, expect, it } from "vitest";

import { installFetchMock, jsonResponse, errorModelFixture } from "../test/apiFixtures";
import { setSignedOut } from "./sessionStore";
import { SignInScreen } from "./SignInScreen";

describe("SignInScreen accessibility", () => {
  beforeEach(() => {
    setSignedOut();
  });

  it("has no axe violations at rest", async () => {
    const { container } = render(
      <ThemeProvider defaultTheme="light">
        <SignInScreen />
      </ThemeProvider>,
    );

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("has no axe violations while showing the sign-in failure alert", async () => {
    const fetchMock = installFetchMock();
    fetchMock.mockResolvedValueOnce(jsonResponse(401, errorModelFixture()));

    const { container, getByRole, getByLabelText, findByRole } = render(
      <ThemeProvider defaultTheme="light">
        <SignInScreen />
      </ThemeProvider>,
    );

    const user = userEvent.setup();
    await user.type(getByRole("textbox", { name: "Username" }), "admin");
    await user.type(getByLabelText("Password"), "wrong");
    await user.click(getByRole("button", { name: "Sign in" }));
    await findByRole("alert");

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });
});
