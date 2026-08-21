// Replays the ONE response recorded verbatim from a running engine (`./recordedPayloads.ts`)
// through the real `apiClient`, exactly like `../selection/SelectionScreen.recorded.test.tsx`
// does for its own recorded fixtures. See that file's own doc comment for why this matters: a
// hand-written fixture is typed, which catches a missing field, but not a wrong BELIEF about
// what the service actually sends. `recordedPayloads.ts` explains why this task's capture is
// one real error response rather than a full plan -- this test proves the screen renders that
// exact captured body correctly, not a guess at its shape.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { beforeAll, describe, expect, it } from "vitest";

import { installFetchMock } from "../../test/apiFixtures";
import { DryRunScreen } from "./DryRunScreen";
import { RECORDED_ENDPOINT_SETUP_FAILED_ERROR } from "./recordedPayloads";
import { PAIR_ID, installApiRouter, jsonResponse, syncPairOutFixture } from "./testHelpers";

beforeAll(() => {
  if (!("hasPointerCapture" in Element.prototype)) {
    Object.defineProperty(Element.prototype, "hasPointerCapture", { value: () => false, configurable: true });
  }
  if (!("releasePointerCapture" in Element.prototype)) {
    Object.defineProperty(Element.prototype, "releasePointerCapture", { value: () => {}, configurable: true });
  }
  if (!("setPointerCapture" in Element.prototype)) {
    Object.defineProperty(Element.prototype, "setPointerCapture", { value: () => {}, configurable: true });
  }
  if (!("scrollIntoView" in Element.prototype)) {
    Object.defineProperty(Element.prototype, "scrollIntoView", { value: () => {}, configurable: true });
  }
});

describe("the recorded engine response renders honestly", () => {
  it("renders the real endpoint_setup_failed body as an error state, with the server's own message verbatim", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      "GET /api/pairs": jsonResponse(200, [syncPairOutFixture()]),
      [`POST /api/pairs/${PAIR_ID}/dry-run`]: jsonResponse(422, RECORDED_ENDPOINT_SETUP_FAILED_ERROR),
    });

    render(
      <ThemeProvider defaultTheme="light">
        <DryRunScreen />
      </ThemeProvider>,
    );
    await user.click(await screen.findByRole("combobox", { name: /sync pair/i }));
    await user.click(await screen.findByRole("option", { name: /prod_databricks_to_qlik/ }));
    await user.click(await screen.findByRole("button", { name: /run dry run/i }));

    expect(await screen.findByText("Could not produce a dry-run plan")).toBeInTheDocument();
    // The server's own words, not a paraphrase -- proves the screen forwards `ErrorModel.message`
    // rather than substituting a generic string for a 422.
    expect(screen.getByText(RECORDED_ENDPOINT_SETUP_FAILED_ERROR.message)).toBeInTheDocument();
    // Never rendered as an empty or no-op plan -- this is a request failure, not an answer.
    expect(screen.queryByText(/this run would change nothing/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Unresolved references" })).not.toBeInTheDocument();
  });
});
