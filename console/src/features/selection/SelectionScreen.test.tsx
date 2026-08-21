// Behaviour tests for the selection screen, driving the REAL `apiClient` through a stubbed
// `globalThis.fetch` -- never a mock of `apiClient` or of this feature's own modules. Every
// fixture is a real `components["schemas"][...]` shape (`./testHelpers.ts`), and the assertions
// that matter are about the REQUEST that would go over the wire and about the exact words
// rendered from the response, because both are places this screen can quietly lie.
//
// The mutation each test is written to kill is named in its own title or first line.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import { beforeAll, describe, expect, it } from "vitest";

import { installFetchMock } from "../../test/apiFixtures";
import { SelectionScreen } from "./SelectionScreen";
import {
  PAIR_ID,
  bodyOf,
  datasetNodeFixture,
  datasetSelectionFixture,
  errorModelFixture,
  installApiRouter,
  jsonResponse,
  lastRequestTo,
  manifestFixture,
  overrideFixture,
  previewFixture,
  countsFixture,
  resultFixture,
  ruleFixture,
  sampleItemFixture,
  schemaNodeFixture,
  sourceTreePageFixture,
  syncPairOutFixture,
  undeterminedFixture,
  type DatasetNodeOut,
  type EndpointManifestOut,
  type PreviewOut,
  type SchemaNodeOut,
  type SelectionOverrideOut,
  type SelectionRuleOut,
  type Routes,
} from "./testHelpers";

beforeAll(() => {
  // The Radix jsdom polyfills every screen test in this app needs -- see
  // `../pairs/PairsScreen.a11y.test.tsx`'s identical block.
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

const OBJECT_RULES: SelectionRuleOut[] = [
  ruleFixture({ id: "rule-a", ordinal: 0, decision: "include", pattern: "analytics.*" }),
  ruleFixture({ id: "rule-b", ordinal: 1, decision: "exclude", pattern: "analytics.staging" }),
];
const DATASET_RULES: SelectionRuleOut[] = [
  ruleFixture({ id: "rule-d", ordinal: 0, scope: "dataset", pattern: "analytics.sales.*" }),
];

interface RouteOptions {
  pairs?: unknown[];
  manifest?: EndpointManifestOut;
  objectRules?: SelectionRuleOut[];
  datasetRules?: SelectionRuleOut[];
  objectOverrides?: SelectionOverrideOut[];
  datasetOverrides?: SelectionOverrideOut[];
  schemaNodes?: SchemaNodeOut[];
  schemaHasMore?: boolean;
  datasetNodes?: DatasetNodeOut[];
  datasetHasMore?: boolean;
  preview?: (body: { rules?: unknown[] }) => PreviewOut;
  extra?: Routes;
}

function makeRoutes(options: RouteOptions = {}): Routes {
  const {
    pairs = [syncPairOutFixture()],
    manifest = manifestFixture(),
    objectRules = OBJECT_RULES,
    datasetRules = DATASET_RULES,
    objectOverrides = [],
    datasetOverrides = [],
    schemaNodes = [schemaNodeFixture()],
    schemaHasMore = false,
    datasetNodes = [datasetNodeFixture()],
    datasetHasMore = false,
    preview = (body) =>
      previewFixture({ rule_set_source: body.rules === undefined ? "stored" : "draft" }),
    extra = {},
  } = options;

  return {
    "GET /api/pairs": jsonResponse(200, pairs),
    "GET /api/endpoints/databricks_prod/manifest": jsonResponse(200, manifest),
    [`GET /api/pairs/${PAIR_ID}/rules`]: (request) =>
      jsonResponse(
        200,
        new URL(request.url).searchParams.get("scope") === "object" ? objectRules : datasetRules,
      ),
    [`GET /api/pairs/${PAIR_ID}/overrides`]: (request) =>
      jsonResponse(
        200,
        new URL(request.url).searchParams.get("scope") === "object"
          ? objectOverrides
          : datasetOverrides,
      ),
    [`GET /api/pairs/${PAIR_ID}/source-tree`]: (request) => {
      const params = new URL(request.url).searchParams;
      const isObject = params.get("scope") === "object";
      return jsonResponse(
        200,
        sourceTreePageFixture({
          nodes: isObject ? schemaNodes : datasetNodes,
          offset: Number(params.get("offset") ?? 0),
          limit: Number(params.get("limit") ?? 200),
          has_more: isObject ? schemaHasMore : datasetHasMore,
          next_offset: (isObject ? schemaHasMore : datasetHasMore)
            ? (isObject ? schemaNodes.length : datasetNodes.length)
            : null,
        }),
      );
    },
    [`POST /api/pairs/${PAIR_ID}/preview`]: async (request) =>
      jsonResponse(200, preview((await bodyOf(request)) as { rules?: unknown[] })),
    ...extra,
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
  await user.click(await screen.findByRole("option", { name: /prod_databricks_to_qlik/ }));
  await screen.findByRole("button", { name: /save rules/i });
}

/** The rule editor's rows, in the order they are rendered = the evaluation order it claims. */
function ruleRows(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>("[data-rule-key]"));
}

function patternValues(container: HTMLElement): string[] {
  return ruleRows(container).map(
    (row) => within(row).getByRole("textbox").getAttribute("value") ?? "",
  );
}

describe("SelectionScreen — loading and pair choice", () => {
  it("reads no source at all until a pair is chosen", async () => {
    const fetchMock = installFetchMock();
    const { calls } = installApiRouter(fetchMock, makeRoutes());

    renderScreen();
    await screen.findByRole("combobox", { name: /sync pair/i });

    expect(calls).toEqual(["GET /api/pairs"]);
    expect(calls.some((call) => call.includes("source-tree"))).toBe(false);
    expect(calls.some((call) => call.includes("preview"))).toBe(false);
  });

  it("loads both scopes' rules and overrides, the source manifest and the first schema page when one is chosen", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const { calls } = installApiRouter(fetchMock, makeRoutes());

    renderScreen();
    await selectThePair(user);

    expect(calls).toContain(`GET /api/pairs/${PAIR_ID}/rules?scope=object`);
    expect(calls).toContain(`GET /api/pairs/${PAIR_ID}/rules?scope=dataset`);
    expect(calls).toContain(`GET /api/pairs/${PAIR_ID}/overrides?scope=object`);
    expect(calls).toContain(`GET /api/pairs/${PAIR_ID}/overrides?scope=dataset`);
    expect(calls).toContain("GET /api/endpoints/databricks_prod/manifest");
    expect(calls.some((call) => call.includes("source-tree?scope=object"))).toBe(true);
  });
});

describe("mutation 1 — rules render in ordinal order, never insertion order", () => {
  it("renders a scrambled rules array in the evaluation order its ordinals declare", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        // Array order deliberately disagrees with ordinal order.
        objectRules: [
          ruleFixture({ id: "c", ordinal: 2, pattern: "third.*" }),
          ruleFixture({ id: "a", ordinal: 0, pattern: "first.*" }),
          ruleFixture({ id: "b", ordinal: 1, pattern: "second.*" }),
        ],
      }),
    );

    const { container } = renderScreen();
    await selectThePair(user);

    expect(patternValues(container)).toEqual(["first.*", "second.*", "third.*"]);
  });

  it("states that the last matching rule decides, not the first", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, makeRoutes());

    const { container } = renderScreen();
    await selectThePair(user);

    // Asserted on the whole panel: the sentence spans several elements (the emphasised
    // "last"), and it is the sentence, not any one node, that must be there.
    expect(container).toHaveTextContent(
      /Rules are evaluated top to bottom and the last matching rule decides — not the first\./i,
    );
    expect(container).toHaveTextContent(/Anything no rule matches is excluded\./i);
  });
});

describe("mutation 2 — the deciding rule is the LAST match the engine reported", () => {
  it("names the second rule, not the first, for a node the engine says the second rule excluded", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        schemaNodes: [
          schemaNodeFixture({
            object_id: "s-staging",
            qualified_name: "analytics.staging",
            // Both rules match "analytics.staging". The engine reports the LAST one.
            result: resultFixture({
              decision: "exclude",
              included: false,
              source: "rule",
              rule_id: "rule-b",
              explain: "excluded by rule #1 exclude glob 'analytics.staging'",
            }),
          }),
        ],
      }),
    );

    renderScreen();
    await selectThePair(user);

    const node = await screen.findByRole("treeitem", { name: /analytics\.staging/ });
    expect(node).toHaveTextContent("Excluded");
    expect(node).toHaveTextContent("excluded by rule #1 exclude glob 'analytics.staging'");
    // A client that re-derived "first match wins" over [include analytics.*, exclude
    // analytics.staging] would say rule 1 of 2, and would say Included.
    expect(node).toHaveTextContent("rule 2 of 2");
    expect(node).not.toHaveTextContent("rule 1 of 2");
  });
});

describe("mutation 3 — undetermined is its own count, never folded into excluded", () => {
  it("renders included, excluded and cannot-tell exactly as the engine tallied them", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        preview: () =>
          previewFixture({
            counts: {
              object: countsFixture({ total: 10, included: 2, excluded: 8, undetermined: 3 }),
              dataset: countsFixture({ total: 4, included: 1, excluded: 3, undetermined: 0 }),
            },
            candidates_examined: 14,
          }),
      }),
    );

    const user2 = user;
    renderScreen();
    await selectThePair(user2);

    const schemas = await screen.findByRole("region", { name: "Schemas counts" });
    // Folding undetermined out of excluded would render 5 here, not 8.
    expect(within(schemas).getByText("Excluded").closest("div")?.parentElement).toHaveTextContent("8");
    expect(within(schemas).getByText("Included").closest("div")?.parentElement).toHaveTextContent("2");
    expect(within(schemas).getByText("Cannot tell").closest("div")?.parentElement).toHaveTextContent("3");
    expect(schemas).toHaveTextContent(/counted a second time within those two/i);
    expect(schemas).toHaveTextContent(/deliberately do not add up/i);
  });

  it("gives a node with an unevaluable rule its own state alongside its decision, never in place of it", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        schemaNodes: [
          schemaNodeFixture({
            result: resultFixture({
              decision: "exclude",
              included: false,
              source: "default",
              rule_id: null,
              explain:
                "excluded by default (no rule matched); 1 rule(s) undetermined: rule #1 exclude tag 'pii' could not be evaluated: tags unknown",
              undetermined: [undeterminedFixture()],
            }),
          }),
        ],
      }),
    );

    renderScreen();
    await selectThePair(user);

    const node = await screen.findByRole("treeitem", { name: /analytics\.sales/ });
    expect(node).toHaveTextContent("Excluded");
    expect(node).toHaveTextContent("Cannot tell (1)");
  });
});

describe("mutation 4 — every node carries its deciding-rule attribution", () => {
  it("renders the engine's own explanation on each tree node", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        schemaNodes: [
          schemaNodeFixture({
            object_id: "s1",
            qualified_name: "analytics.sales",
            result: resultFixture({ explain: "included by rule #0 include glob 'analytics.*'" }),
          }),
          schemaNodeFixture({
            object_id: "s2",
            qualified_name: "finance.reporting",
            result: resultFixture({
              decision: "exclude",
              included: false,
              source: "default",
              rule_id: null,
              explain: "excluded by default (no rule matched)",
            }),
          }),
        ],
      }),
    );

    renderScreen();
    await selectThePair(user);

    expect(await screen.findByRole("treeitem", { name: /analytics\.sales/ })).toHaveTextContent(
      "included by rule #0 include glob 'analytics.*'",
    );
    expect(screen.getByRole("treeitem", { name: /finance\.reporting/ })).toHaveTextContent(
      "excluded by default (no rule matched)",
    );
  });

  it("traces a node back to the exact rule row that decided it", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        schemaNodes: [
          schemaNodeFixture({
            result: resultFixture({
              rule_id: "rule-b",
              decision: "exclude",
              included: false,
              explain: "excluded by rule #1 exclude glob 'analytics.staging'",
            }),
          }),
        ],
      }),
    );

    const { container } = renderScreen();
    await selectThePair(user);

    await user.click(await screen.findByRole("treeitem", { name: /analytics\.sales/ }));
    await user.click(await screen.findByRole("button", { name: /show the deciding rule/i }));

    const rows = ruleRows(container);
    expect(rows[1]).toHaveAttribute("aria-current", "true");
    expect(rows[0]).not.toHaveAttribute("aria-current");
  });
});

describe("mutation 5 — reorder sends the complete ordered id list", () => {
  it("sends every rule id of the scope, in the new order, not a move-to-index delta", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const reorderCalls: unknown[] = [];
    const { requests } = installApiRouter(
      fetchMock,
      makeRoutes({
        objectRules: [
          ruleFixture({ id: "rule-1", ordinal: 0, pattern: "one.*" }),
          ruleFixture({ id: "rule-2", ordinal: 1, pattern: "two.*" }),
          ruleFixture({ id: "rule-3", ordinal: 2, pattern: "three.*" }),
        ],
        extra: {
          [`POST /api/pairs/${PAIR_ID}/rules/reorder`]: async (request) => {
            reorderCalls.push(await bodyOf(request));
            return jsonResponse(200, []);
          },
        },
      }),
    );

    const { container } = renderScreen();
    await selectThePair(user);

    await user.click(screen.getByRole("button", { name: "Move rule 1 down" }));
    expect(patternValues(container)).toEqual(["two.*", "one.*", "three.*"]);

    await user.click(screen.getByRole("button", { name: /save rules/i }));
    await waitFor(() => expect(reorderCalls).toHaveLength(2));

    const objectReorder = reorderCalls.find(
      (body) => (body as { scope: string }).scope === "object",
    );
    expect(objectReorder).toEqual({ scope: "object", rule_ids: ["rule-2", "rule-1", "rule-3"] });
    // The whole point: it is the COMPLETE list, not the one rule that moved.
    expect((objectReorder as { rule_ids: string[] }).rule_ids).toHaveLength(3);

    const request = lastRequestTo(requests, `POST /api/pairs/${PAIR_ID}/rules/reorder`);
    expect(new URL(request.url).pathname).toBe(`/api/pairs/${PAIR_ID}/rules/reorder`);
  });

  it("reorders by drag with the same primitive the keyboard uses", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        objectRules: [
          ruleFixture({ id: "rule-1", ordinal: 0, pattern: "one.*" }),
          ruleFixture({ id: "rule-2", ordinal: 1, pattern: "two.*" }),
        ],
      }),
    );

    const { container } = renderScreen();
    await selectThePair(user);

    const rows = ruleRows(container);
    const handle = rows[0]?.querySelector("[data-drag-handle]");
    if (!handle || !rows[1]) throw new Error("expected a drag handle and a second row");

    const transfer = { setData: () => {}, effectAllowed: "" } as unknown as DataTransfer;
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.dragStart(handle, { dataTransfer: transfer });
    fireEvent.dragOver(rows[1], { dataTransfer: transfer });
    fireEvent.drop(rows[1], { dataTransfer: transfer });

    expect(patternValues(container)).toEqual(["two.*", "one.*"]);
  });
});

describe("mutation 6 — an override is pinned by qualified name", () => {
  it("sends the catalog.schema qualified name, never the connector's opaque object_id", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const posted: unknown[] = [];
    installApiRouter(
      fetchMock,
      makeRoutes({
        schemaNodes: [
          schemaNodeFixture({
            object_id: "01234567-89ab-cdef-0123-456789abcdef",
            qualified_name: "analytics.sales",
          }),
        ],
        extra: {
          [`POST /api/pairs/${PAIR_ID}/overrides`]: async (request) => {
            posted.push(await bodyOf(request));
            return jsonResponse(201, overrideFixture({ object_id: "analytics.sales", decision: "exclude" }));
          },
        },
      }),
    );

    renderScreen();
    await selectThePair(user);

    await user.click(await screen.findByRole("treeitem", { name: /analytics\.sales/ }));
    await user.click(await screen.findByRole("button", { name: /always exclude/i }));

    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({
      scope: "object",
      object_id: "analytics.sales",
      decision: "exclude",
    });
    expect(JSON.stringify(posted[0])).not.toContain("01234567-89ab-cdef-0123-456789abcdef");
  });

  it("refuses to offer a pin at all for a node the source gave no qualified name for", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const { calls } = installApiRouter(
      fetchMock,
      makeRoutes({
        schemaNodes: [
          schemaNodeFixture({ object_id: "opaque-id-only", qualified_name: null, display_name: "sales" }),
        ],
      }),
    );

    renderScreen();
    await selectThePair(user);

    await user.click(await screen.findByRole("treeitem", { name: /sales/ }));
    const detail = await screen.findByRole("region", { name: "Selected object" });
    expect(detail).toHaveTextContent(/cannot be pinned/i);
    expect(detail).toHaveTextContent(/must be pinned by its catalog\.schema qualified name/i);
    expect(within(detail).queryByRole("button", { name: /always include/i })).toBeNull();
    expect(calls.some((call) => call.includes("overrides") && call.startsWith("POST"))).toBe(false);
  });

  it("shows an existing pin as beating every rule, not as another rule", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        objectOverrides: [overrideFixture({ object_id: "analytics.sales", decision: "include" })],
        schemaNodes: [
          schemaNodeFixture({
            result: resultFixture({
              source: "override",
              rule_id: null,
              explain: "included by override 'analytics.sales'",
            }),
          }),
        ],
      }),
    );

    renderScreen();
    await selectThePair(user);

    const node = await screen.findByRole("treeitem", { name: /analytics\.sales/ });
    expect(node).toHaveTextContent("pinned");
    await user.click(node);
    const detail = await screen.findByRole("region", { name: "Selected object" });
    expect(detail).toHaveTextContent("Pinned by an override");
    expect(detail).toHaveTextContent(/beats every rule outright/i);
  });
});

describe("mutation 7 — a draft preview is never mistaken for the saved configuration", () => {
  it("labels stored numbers as saved and draft numbers as an unsaved draft", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const bodies: { rules?: unknown[] }[] = [];
    installApiRouter(
      fetchMock,
      makeRoutes({
        preview: (body) => {
          bodies.push(body);
          return previewFixture({
            rule_set_source: body.rules === undefined ? "stored" : "draft",
          });
        },
      }),
    );

    const { container } = renderScreen();
    await selectThePair(user);

    await waitFor(() => expect(bodies).toHaveLength(1), { timeout: 3000 });
    // A stored preview OMITS `rules` -- that is how the route is asked for the saved set.
    expect(bodies[0]).not.toHaveProperty("rules");
    expect(await screen.findByText("Saved rules")).toBeInTheDocument();
    expect(screen.getByText(/what the saved rules would select on the next run/i)).toBeInTheDocument();

    const firstPattern = within(ruleRows(container)[0]!).getByRole("textbox");
    await user.clear(firstPattern);
    await user.type(firstPattern, "analytics.sales");

    await waitFor(() => expect(bodies.length).toBeGreaterThan(1), { timeout: 3000 });
    expect(bodies[bodies.length - 1]).toHaveProperty("rules");
    expect(await screen.findByText("Unsaved draft")).toBeInTheDocument();
    expect(
      screen.getByText(/what the unsaved draft in the editor would select/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/what the saved rules would select on the next run/i)).toBeNull();
  });

  it("sends BOTH scopes' rules in a draft preview, so the other scope is not silently dropped", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const bodies: { rules?: { scope: string; pattern: string }[] }[] = [];
    installApiRouter(
      fetchMock,
      makeRoutes({
        preview: (body) => {
          bodies.push(body as { rules?: { scope: string; pattern: string }[] });
          return previewFixture({
            rule_set_source: body.rules === undefined ? "stored" : "draft",
          });
        },
      }),
    );

    const { container } = renderScreen();
    await selectThePair(user);
    await waitFor(() => expect(bodies).toHaveLength(1), { timeout: 3000 });

    const firstPattern = within(ruleRows(container)[0]!).getByRole("textbox");
    await user.clear(firstPattern);
    await user.type(firstPattern, "analytics.sales");
    await waitFor(() => expect(bodies.length).toBeGreaterThan(1), { timeout: 3000 });

    const sent = bodies[bodies.length - 1]?.rules ?? [];
    expect(sent.map((rule) => `${rule.scope}:${rule.pattern}`)).toContain(
      "dataset:analytics.sales.*",
    );
    expect(sent.filter((rule) => rule.scope === "object")).toHaveLength(2);
  });

  it("says the source tree still shows the saved rules while a draft is unsaved", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, makeRoutes());

    const { container } = renderScreen();
    await selectThePair(user);

    const firstPattern = within(ruleRows(container)[0]!).getByRole("textbox");
    await user.clear(firstPattern);
    await user.type(firstPattern, "analytics.x");

    expect(
      await screen.findByText(/This tree still shows the/i),
    ).toHaveTextContent(/saved.*rules, because the browse route only ever evaluates what is stored/i);
  });
});

describe("mutation 8 — a truncated preview is never rendered as totals", () => {
  it("says the counts are partial, and says so on every figure", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        preview: () =>
          previewFixture({
            truncated: true,
            candidates_examined: 20000,
            counts: {
              object: countsFixture({ total: 700, included: 400, excluded: 300, undetermined: 0 }),
              dataset: countsFixture({ total: 0, included: 0, excluded: 0, undetermined: 0 }),
            },
          }),
      }),
    );

    const { container } = renderScreen();
    await selectThePair(user);
    await screen.findByRole("region", { name: "Schemas counts" });

    expect(container).toHaveTextContent(/Partial result — these are not totals\./i);
    expect(container).toHaveTextContent(/stopped after examining 20000 candidates/i);
    const schemas = screen.getByRole("region", { name: "Schemas counts" });
    // Every figure says it counts only what was examined, so no tile reads as a total.
    expect(schemas).toHaveTextContent(/not a total/i);
    expect(schemas).not.toHaveTextContent(/of 700 in the source/i);
  });

  it("does not claim partial results when the walk completed", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        preview: () =>
          previewFixture({
            counts: {
              object: countsFixture({ total: 3, included: 1, excluded: 2, undetermined: 0 }),
              dataset: countsFixture(),
            },
            candidates_examined: 3,
          }),
      }),
    );

    renderScreen();
    await selectThePair(user);
    await screen.findByRole("region", { name: "Schemas counts" });

    expect(screen.queryByText(/Partial result/i)).toBeNull();
    expect(screen.getByRole("region", { name: "Schemas counts" })).toHaveTextContent(
      /of 3 in the source/i,
    );
  });
});

describe("mutation 9 — an unsupported matcher is disabled with the reason shown", () => {
  it("disables the tag matcher and prints why when the manifest declares tags 'na'", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, makeRoutes({ manifest: manifestFixture({ tags: "na" }) }));

    const { container } = renderScreen();
    await selectThePair(user);

    const row = ruleRows(container)[0]!;
    const tag = within(row).getByRole("radio", { name: /^Tag/ });
    expect(tag).toBeDisabled();
    expect(tag).toHaveAccessibleName(/unavailable/i);
    expect(within(row).getByRole("radio", { name: /^Name glob/ })).toBeEnabled();

    // The reason itself, not just the disabled state.
    expect(
      screen.getByText(/declares "tags" as "na".*cannot report|could never reach a verdict/i),
    ).toBeInTheDocument();
  });

  it("keeps a matcher selectable when the manifest could not be read, and says it is unknown", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        extra: {
          "GET /api/endpoints/databricks_prod/manifest": jsonResponse(
            502,
            errorModelFixture({ code: "connector_unreachable", message: "no", field: null }),
          ),
        },
      }),
    );

    const { container } = renderScreen();
    await selectThePair(user);

    const row = ruleRows(container)[0]!;
    expect(within(row).getByRole("radio", { name: /^Tag/ })).toBeEnabled();
    expect(container).toHaveTextContent(/has not been read yet/i);
    expect(container).not.toHaveTextContent(/could never reach a verdict/i);
  });
});

describe("the source tree stays lazy and bounded", () => {
  it("does not touch the source's table stream until a schema is expanded", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const { calls } = installApiRouter(
      fetchMock,
      makeRoutes({
        schemaNodes: [schemaNodeFixture({ object_id: "s1", qualified_name: "analytics.sales" })],
        datasetHasMore: false,
      }),
    );

    renderScreen();
    await selectThePair(user);
    await screen.findByRole("treeitem", { name: /analytics\.sales/ });

    expect(calls.some((call) => call.includes("source-tree?scope=dataset"))).toBe(false);

    await user.click(screen.getByRole("treeitem", { name: /analytics\.sales/ }));
    await waitFor(() =>
      expect(calls.some((call) => call.includes("source-tree?scope=dataset"))).toBe(true),
    );
  });

  it("expanding several schemas in a row is still one table-stream request", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const { calls } = installApiRouter(
      fetchMock,
      makeRoutes({
        schemaNodes: [
          schemaNodeFixture({ object_id: "s1", qualified_name: "analytics.sales" }),
          schemaNodeFixture({ object_id: "s2", qualified_name: "analytics.staging" }),
          schemaNodeFixture({ object_id: "s3", qualified_name: "finance.reporting" }),
        ],
      }),
    );

    renderScreen();
    await selectThePair(user);

    await user.click(await screen.findByRole("treeitem", { name: /analytics\.sales/ }));
    await user.click(screen.getByRole("treeitem", { name: /analytics\.staging/ }));
    await user.click(screen.getByRole("treeitem", { name: /finance\.reporting/ }));

    await waitFor(() =>
      expect(calls.filter((call) => call.includes("source-tree?scope=dataset"))).toHaveLength(1),
    );
  });

  it("says a schema's children are only what has been read while the table stream has more", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        schemaNodes: [schemaNodeFixture({ object_id: "s1", qualified_name: "analytics.sales" })],
        datasetNodes: [datasetNodeFixture()],
        datasetHasMore: true,
      }),
    );

    renderScreen();
    await selectThePair(user);
    await user.click(await screen.findByRole("treeitem", { name: /analytics\.sales/ }));

    expect(
      await screen.findByRole("treeitem", { name: /more may exist under this schema/i }),
    ).toBeInTheDocument();
  });

  it("never drops a table whose schema has not been read", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        schemaNodes: [schemaNodeFixture({ object_id: "s1", qualified_name: "analytics.sales" })],
        datasetNodes: [
          datasetNodeFixture({ object_id: "t9", qualified_name: "finance.reporting.ledger" }),
        ],
      }),
    );

    renderScreen();
    await selectThePair(user);
    await user.click(await screen.findByRole("treeitem", { name: /analytics\.sales/ }));

    expect(
      await screen.findByRole("treeitem", { name: /whose schema is not among the schemas read/i }),
    ).toBeInTheDocument();
  });
});

describe("a dataset's two halves (C5)", () => {
  it("shows the parent schema's result and the table's own result, both in full", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        datasetNodes: [
          datasetNodeFixture({
            selection: datasetSelectionFixture({
              included: false,
              explain: "excluded because its parent schema was excluded by rule #1",
              parent: resultFixture({
                decision: "exclude",
                included: false,
                rule_id: "rule-b",
                explain: "excluded by rule #1 exclude glob 'analytics.staging'",
              }),
              dataset: resultFixture({
                source: "default",
                rule_id: null,
                explain: "included by rule #0 include glob 'analytics.sales.*'",
              }),
            }),
          }),
        ],
      }),
    );

    renderScreen();
    await selectThePair(user);
    // Only the schema is rendered until it is expanded -- the table stream is not read before.
    const [schemaRow] = await screen.findAllByRole("treeitem");
    await user.click(schemaRow!);
    await user.click(await screen.findByRole("treeitem", { name: /analytics\.sales\.orders/ }));

    const detail = await screen.findByRole("region", { name: "Selected object" });
    expect(detail).toHaveTextContent("Parent schema");
    expect(detail).toHaveTextContent("This table's own dataset-scope rules, in isolation");
    expect(detail).toHaveTextContent("excluded by rule #1 exclude glob 'analytics.staging'");
    expect(detail).toHaveTextContent("included by rule #0 include glob 'analytics.sales.*'");
    expect(detail).toHaveTextContent(/only if its parent schema was included AND/i);
  });
});

describe("the draft is previewable, discardable and saved as one sequence", () => {
  it("discards back to the saved rules", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, makeRoutes());

    const { container } = renderScreen();
    await selectThePair(user);

    const firstPattern = within(ruleRows(container)[0]!).getByRole("textbox");
    await user.clear(firstPattern);
    await user.type(firstPattern, "changed.*");
    expect(patternValues(container)[0]).toBe("changed.*");
    expect(screen.getByRole("button", { name: /save rules/i })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: /discard draft/i }));
    expect(patternValues(container)[0]).toBe("analytics.*");
    expect(screen.getByRole("button", { name: /save rules/i })).toBeDisabled();
  });

  it("creates, updates, deletes and then reorders — in that order", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const order: string[] = [];
    installApiRouter(
      fetchMock,
      makeRoutes({
        objectRules: [ruleFixture({ id: "rule-1", ordinal: 0, pattern: "one.*" })],
        datasetRules: [],
        extra: {
          [`POST /api/pairs/${PAIR_ID}/rules`]: async (request) => {
            order.push(`create ${JSON.stringify(await bodyOf(request))}`);
            return jsonResponse(201, ruleFixture({ id: "rule-new", ordinal: 1, pattern: "two.*" }));
          },
          [`POST /api/pairs/${PAIR_ID}/rules/reorder`]: async (request) => {
            order.push(`reorder ${JSON.stringify(await bodyOf(request))}`);
            return jsonResponse(200, []);
          },
        },
      }),
    );

    const { container } = renderScreen();
    await selectThePair(user);

    await user.click(screen.getByRole("button", { name: /^add rule$/i }));
    const second = within(ruleRows(container)[1]!).getByRole("textbox");
    await user.type(second, "two.*");
    await user.click(screen.getByRole("button", { name: /save rules/i }));

    await waitFor(() => expect(order).toHaveLength(2));
    expect(order[0]).toContain("create");
    // The create omits `ordinal`: the server appends and the reorder below fixes the order.
    expect(order[0]).not.toContain("ordinal");
    expect(order[1]).toBe(
      `reorder ${JSON.stringify({ scope: "object", rule_ids: ["rule-1", "rule-new"] })}`,
    );
  });

  it("keeps the operator's draft when a save is refused, and renders the reason on the row", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        objectRules: [ruleFixture({ id: "rule-1", ordinal: 0, pattern: "one.*" })],
        datasetRules: [],
        extra: {
          [`POST /api/pairs/${PAIR_ID}/rules`]: jsonResponse(
            422,
            errorModelFixture({
              message: "'analytics' must contain exactly 1 '.' separating 2 glob segments",
              field: "pattern",
            }),
          ),
        },
      }),
    );

    const { container } = renderScreen();
    await selectThePair(user);

    await user.click(screen.getByRole("button", { name: /^add rule$/i }));
    await user.type(within(ruleRows(container)[1]!).getByRole("textbox"), "analytics");
    await user.click(screen.getByRole("button", { name: /save rules/i }));

    expect(
      await screen.findByText(/must contain exactly 1 '\.' separating 2 glob segments/, {
        selector: "[role='alert']",
      }),
    ).toBeInTheDocument();
    // The edit survives: the operator still has the thing they need to fix.
    expect(patternValues(container)).toEqual(["one.*", "analytics"]);
  });

  it("pauses the preview rather than firing a request per keystroke for a rule with no pattern", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const bodies: unknown[] = [];
    installApiRouter(
      fetchMock,
      makeRoutes({
        preview: (body) => {
          bodies.push(body);
          return previewFixture({
            rule_set_source: body.rules === undefined ? "stored" : "draft",
          });
        },
      }),
    );

    const { container } = renderScreen();
    await selectThePair(user);
    await waitFor(() => expect(bodies).toHaveLength(1), { timeout: 3000 });

    await user.click(screen.getByRole("button", { name: /^add rule$/i }));
    await waitFor(() => expect(container).toHaveTextContent(/Preview paused/i));
    expect(container).toHaveTextContent(/1 rule\(s\) still need a pattern/i);
    expect(bodies).toHaveLength(1);
  });
});

describe("the preview stays honest while it is being recalculated", () => {
  it("marks the visible numbers as being recalculated instead of presenting them as current", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const gates: (() => void)[] = [];
    installApiRouter(
      fetchMock,
      makeRoutes({
        preview: (body) =>
          previewFixture({ rule_set_source: body.rules === undefined ? "stored" : "draft" }),
        extra: {
          [`POST /api/pairs/${PAIR_ID}/preview`]: async (request) => {
            const body = (await bodyOf(request)) as { rules?: unknown[] };
            if (body.rules !== undefined) {
              await new Promise<void>((resolve) => {
                gates.push(resolve);
              });
            }
            return jsonResponse(
              200,
              previewFixture({ rule_set_source: body.rules === undefined ? "stored" : "draft" }),
            );
          },
        },
      }),
    );

    const { container } = renderScreen();
    await selectThePair(user);
    await screen.findByText("Saved rules");

    const firstPattern = within(ruleRows(container)[0]!).getByRole("textbox");
    await user.clear(firstPattern);
    await user.type(firstPattern, "analytics.x");

    expect(await screen.findByText("Recalculating…", {}, { timeout: 3000 })).toBeInTheDocument();
    // More than one draft preview may be in flight (each keystroke re-arms the debounce);
    // release every one, so what is asserted next is the settled state, not a race.
    await waitFor(() => expect(gates.length).toBeGreaterThan(0));
    for (const release of gates) release();
    await waitFor(() => expect(screen.queryByText("Recalculating…")).toBeNull(), { timeout: 3000 });
  });

  it("asks for the source's tags only when the rule set being evaluated actually uses them", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    const bodies: { resolve_tags: boolean; resolve_owners: boolean }[] = [];
    const { calls } = installApiRouter(
      fetchMock,
      makeRoutes({
        objectRules: [ruleFixture({ id: "rule-1", ordinal: 0, matcher_kind: "tag", pattern: "pii" })],
        datasetRules: [],
        preview: (body) => {
          bodies.push(body as unknown as { resolve_tags: boolean; resolve_owners: boolean });
          return previewFixture({
            rule_set_source: body.rules === undefined ? "stored" : "draft",
          });
        },
      }),
    );

    renderScreen();
    await selectThePair(user);
    await waitFor(() => expect(bodies).toHaveLength(1), { timeout: 3000 });

    // resolve_tags/resolve_owners cost one extra source read() per node, so they are sent
    // exactly when the set being evaluated contains such a rule -- and never speculatively.
    expect(bodies[0]).toMatchObject({ resolve_tags: true, resolve_owners: false });
    // The tree gets the same flag derived from the SAVED rules, so the two cannot disagree
    // about a tag rule: one resolving tags and the other not would show different verdicts for
    // the same object.
    expect(calls.some((call) => call.includes("source-tree") && call.includes("resolve_tags=true"))).toBe(
      true,
    );
  });
});

describe("the preview sample", () => {
  it("names it a sample of the walk order, never a random one, and links a row to its node", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      makeRoutes({
        preview: () =>
          previewFixture({
            counts: {
              object: countsFixture({ total: 1, included: 1, excluded: 0, undetermined: 0 }),
              dataset: countsFixture(),
            },
            candidates_examined: 1,
            sample: [sampleItemFixture()],
          }),
      }),
    );

    renderScreen();
    await selectThePair(user);

    const sample = await screen.findByRole("region", { name: "Preview sample" });
    expect(sample).toHaveTextContent(/in the order the engine walks them/i);
    expect(sample).toHaveTextContent(/not a random sample/i);

    await user.click(within(sample).getByRole("button", { name: /analytics\.sales/ }));
    expect(await screen.findByRole("region", { name: "Selected object" })).toHaveTextContent(
      "analytics.sales",
    );
  });
});
