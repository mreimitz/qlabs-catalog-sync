// The same screen, driven by responses RECORDED VERBATIM from a running engine
// (`./recordedPayloads.ts`) rather than by hand-written fixtures.
//
// Why this file exists on top of `SelectionScreen.test.tsx`: six times on this build a task
// shipped correct code with passing tests while the test's *fake* differed from reality at
// exactly the point under test. Hand-written fixtures are typed, which catches a missing field
// -- they do not catch a wrong BELIEF about what the service puts in those fields. These
// payloads came off `qlabs-catalog-sync serve` with a real connector, a real evaluator and the
// real routes, so what is asserted here is what an operator would actually see.
//
// The recorded rule set is chosen so the three easiest things to get wrong are all present at
// once: a per-object override that beats every rule, a `tag` rule against a source whose
// manifest declares `tags` as `na` (producing a real non-zero `undetermined`), and a dataset
// whose own dataset-scope rules concluded "no rule matched" while the dataset is nonetheless
// INCLUDED by inheriting its schema.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { beforeAll, describe, expect, it } from "vitest";

import { installFetchMock } from "../../test/apiFixtures";
import { SelectionScreen } from "./SelectionScreen";
import {
  RECORDED_MANIFEST,
  RECORDED_OVERRIDES_OBJECT,
  RECORDED_PREVIEW,
  RECORDED_RULES_DATASET,
  RECORDED_RULES_OBJECT,
  RECORDED_SOURCE_TREE_DATASET,
  RECORDED_SOURCE_TREE_OBJECT,
} from "./recordedPayloads";
import { installApiRouter, jsonResponse, syncPairOutFixture, type Routes } from "./testHelpers";

/** The pair id the recorded rules and overrides actually carry. */
const RECORDED_PAIR_ID = RECORDED_RULES_OBJECT[0]!.pair_id;
/** The source endpoint the recorded manifest actually describes. */
const RECORDED_SOURCE = RECORDED_MANIFEST.endpoint;

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
  if (typeof globalThis.ResizeObserver === "undefined") {
    class FakeResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver;
  }
});

function recordedRoutes(): Routes {
  return {
    "GET /api/pairs": jsonResponse(200, [
      syncPairOutFixture({ id: RECORDED_PAIR_ID, name: "probe_pair", source: RECORDED_SOURCE }),
    ]),
    [`GET /api/endpoints/${RECORDED_SOURCE}/manifest`]: jsonResponse(200, RECORDED_MANIFEST),
    [`GET /api/pairs/${RECORDED_PAIR_ID}/rules`]: (request) =>
      jsonResponse(
        200,
        new URL(request.url).searchParams.get("scope") === "object"
          ? RECORDED_RULES_OBJECT
          : RECORDED_RULES_DATASET,
      ),
    [`GET /api/pairs/${RECORDED_PAIR_ID}/overrides`]: (request) =>
      jsonResponse(
        200,
        new URL(request.url).searchParams.get("scope") === "object"
          ? RECORDED_OVERRIDES_OBJECT
          : [],
      ),
    [`GET /api/pairs/${RECORDED_PAIR_ID}/source-tree`]: (request) =>
      jsonResponse(
        200,
        new URL(request.url).searchParams.get("scope") === "object"
          ? RECORDED_SOURCE_TREE_OBJECT
          : RECORDED_SOURCE_TREE_DATASET,
      ),
    [`POST /api/pairs/${RECORDED_PAIR_ID}/preview`]: jsonResponse(200, RECORDED_PREVIEW),
  };
}

function renderScreen() {
  return render(
    <ThemeProvider defaultTheme="light">
      <SelectionScreen />
    </ThemeProvider>,
  );
}

async function selectThePair(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("combobox", { name: /sync pair/i }));
  await user.click(await screen.findByRole("option", { name: /probe_pair/ }));
  await screen.findByRole("button", { name: /save rules/i });
}

describe("the recorded engine responses render honestly", () => {
  it("renders the five saved object rules in the order the engine's ordinals declare", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, recordedRoutes());

    const { container } = renderScreen();
    await selectThePair(user);

    const patterns = Array.from(container.querySelectorAll<HTMLElement>("[data-rule-key]")).map(
      (row) => within(row).getByRole("textbox").getAttribute("value"),
    );
    // The recorded set is C3's worked example after a real reorder: the exclude was moved
    // ABOVE the include, which is what flipped analytics.prod_staging back to included.
    expect(patterns).toEqual([
      "analytics.*",
      "analytics.staging",
      "analytics.prod_staging",
      "analytics.prod_staging",
      "pii",
    ]);
  });

  it("warns that the recorded tag rule can never reach a verdict against this recorded source", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, recordedRoutes());

    const { container } = renderScreen();
    await selectThePair(user);

    // The recorded manifest really does declare data_product.tags as "na" -- Databricks
    // without a SQL warehouse (RM-01 D6). That is not a fixture author's guess.
    expect(RECORDED_MANIFEST.manifest?.entities.data_product?.fields.tags?.mode).toBe("na");

    const rows = Array.from(container.querySelectorAll<HTMLElement>("[data-rule-key]"));
    const tagRow = rows[4]!;
    expect(within(tagRow).getByRole("radio", { name: /^Tag/ })).toBeDisabled();
    expect(tagRow).toHaveTextContent(
      /uses a matcher this source cannot evaluate, so it comes back undetermined for every object/i,
    );
  });

  it("renders the recorded counts exactly, including the undetermined overlap", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, recordedRoutes());

    renderScreen();
    await selectThePair(user);

    const schemas = await screen.findByRole("region", { name: "Schemas counts" });
    // Recorded: total 4, included 3, excluded 1, undetermined 3 -- included + excluded == total
    // while undetermined overlaps them, which is exactly the shape a naive UI breaks.
    expect(RECORDED_PREVIEW.counts.object).toEqual({
      total: 4,
      included: 3,
      excluded: 1,
      undetermined: 3,
    });
    expect(within(schemas).getByText("Included").closest("div")?.parentElement).toHaveTextContent("3");
    expect(within(schemas).getByText("Excluded").closest("div")?.parentElement).toHaveTextContent("1");
    expect(within(schemas).getByText("Cannot tell").closest("div")?.parentElement).toHaveTextContent("3");
    expect(schemas).toHaveTextContent("Included + excluded = 4");
    expect(schemas).toHaveTextContent(/deliberately do not add up to 4/i);
  });

  it("shows a node the engine said an override decided as an override, not as a rule", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, recordedRoutes());

    renderScreen();
    await selectThePair(user);

    const node = await screen.findByRole("treeitem", { name: /finance\.reporting/ });
    expect(node).toHaveTextContent("Included");
    expect(node).toHaveTextContent("pinned");
    expect(node).toHaveTextContent("included by override include 'finance.reporting'");
    // An override beats every rule outright, so there is no rule position to show.
    expect(node).not.toHaveTextContent(/rule \d+ of \d+/);

    await user.click(node);
    const detail = await screen.findByRole("region", { name: "Selected object" });
    expect(detail).toHaveTextContent("Pinned by an override");
    expect(within(detail).queryByRole("button", { name: /show the deciding rule/i })).toBeNull();
  });

  it("shows a node's cannot-tell state next to its decision, with the engine's own reason", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, recordedRoutes());

    renderScreen();
    await selectThePair(user);

    const node = await screen.findByRole("treeitem", { name: /analytics\.sales/ });
    expect(node).toHaveTextContent("Included");
    expect(node).toHaveTextContent("Cannot tell (1)");
    // rule_id 2516... is the recorded ordinal-0 rule, so: position 1 of 5.
    expect(node).toHaveTextContent("rule 1 of 5");

    await user.click(node);
    const detail = await screen.findByRole("region", { name: "Selected object" });
    expect(detail).toHaveTextContent(
      "rule #4 exclude tag 'pii' could not be evaluated: tags unknown",
    );
    expect(detail).toHaveTextContent(/Tag rule exclude "pii" could not be evaluated: tags unknown/);
  });

  it("does not read a table as excluded when the engine said it inherits an included schema", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, recordedRoutes());

    renderScreen();
    await selectThePair(user);

    await user.click(await screen.findByRole("treeitem", { name: /finance\.reporting/ }));
    const table = await screen.findByRole("treeitem", { name: /finance\.reporting\.ledger/ });
    await user.click(table);

    const detail = await screen.findByRole("region", { name: "Selected object" });
    // The engine's own answer, verbatim: included by inheritance, even though this table's own
    // dataset-scope rules concluded "no rule matched".
    expect(detail).toHaveTextContent(
      "included: inherits parent schema's selection (included by override include 'finance.reporting'",
    );
    expect(detail).toHaveTextContent("Parent schema");
    expect(detail).toHaveTextContent("This table's own dataset-scope rules, in isolation");
    expect(detail).toHaveTextContent("excluded by default (no rule matched)");
    expect(detail).toHaveTextContent(
      /inside an included schema, "no rule matched" means this table inherits/i,
    );
  });

  it("traces a recorded node back to the recorded rule row that decided it", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, recordedRoutes());

    const { container } = renderScreen();
    await selectThePair(user);

    await user.click(await screen.findByRole("treeitem", { name: /analytics\.sales/ }));
    await user.click(await screen.findByRole("button", { name: /show the deciding rule/i }));

    await waitFor(() => {
      const rows = Array.from(container.querySelectorAll<HTMLElement>("[data-rule-key]"));
      expect(rows[0]).toHaveAttribute("aria-current", "true");
      expect(within(rows[0]!).getByRole("textbox")).toHaveValue("analytics.*");
    });
  });
});
