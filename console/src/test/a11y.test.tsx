// The accessibility gate (`pnpm a11y`). Runs axe-core directly against the rendered
// application shell and fails on any violation — not vitest-axe (unmaintained at 0.1.0,
// a compatibility risk against Vitest 4; see the T13.1 report). This is deliberately
// its own script/file, separate from the general `pnpm test` suite, so CI (T13.8) can
// gate on it independently.
import { render } from "@testing-library/react";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import axe from "axe-core";
import { describe, expect, it } from "vitest";

import App from "../App";

describe("accessibility", () => {
  it("renders the scaffolded shell with no axe violations", async () => {
    const { container } = render(
      <ThemeProvider defaultTheme="light">
        <App />
      </ThemeProvider>,
    );

    const results = await axe.run(container);

    expect(results.violations).toEqual([]);
  });
});
