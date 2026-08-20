// A genuine smoke test for the scaffolded shell, not a placeholder. It exists to make
// `pnpm test` a real gate the moment T13.2 starts building on top of this scaffold —
// see the T13.1 report for why this file, not a real screen, is what's under test here.
import { render, screen } from "@testing-library/react";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { describe, expect, it } from "vitest";

import App from "../App";

function renderApp() {
  return render(
    <ThemeProvider defaultTheme="light">
      <App />
    </ThemeProvider>,
  );
}

describe("scaffolded application shell", () => {
  it("renders the sidebar navigation and the placeholder data table", () => {
    renderApp();

    // The four scaffold nav entries render as accessible buttons.
    expect(screen.getByRole("button", { name: "Data" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tables" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Home" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Settings" })).toBeInTheDocument();

    // The placeholder DataTable actually rendered its sample rows.
    expect(screen.getByText("Customer import")).toBeInTheDocument();
    expect(screen.getByText("Partner feed")).toBeInTheDocument();

    // Exactly one <main> landmark (brand-ui issue 386, called out in App.tsx) —
    // SidebarInset renders it; nesting a second one is the regression this guards.
    expect(screen.getAllByRole("main")).toHaveLength(1);
  });
});
