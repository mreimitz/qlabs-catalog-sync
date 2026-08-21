// Named *.a11y.test.tsx beside the screen it covers (console/CLAUDE.md's load-bearing naming
// convention -- `pnpm a11y` runs `vitest run a11y`, a path-substring filter, so an accessibility
// test named anything else is silently not gated).
//
// This screen has the three shapes the task brief names as exactly where keyboard and ARIA
// defects appear, so each one is covered by behaviour as well as by axe:
//
//  * a tree with expandable nodes -- roving tabindex, `aria-expanded`, arrow-key navigation;
//  * a drag-reorderable list -- which must have a real keyboard path, not a drag-only one, and
//    must announce where a row landed;
//  * a live-updating count region -- which needs the right politeness or it either spams on
//    every keystroke or never announces at all.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
// Aliased: `Routes` is already this file's route-map type from `./testHelpers`, and the
// react-router component of the same name would shadow it.
import { MemoryRouter, Route, Routes as RouterRoutes } from "react-router-dom";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import axe from "axe-core";
import { beforeAll, describe, expect, it } from "vitest";

import { Shell } from "../../app/Shell";
import { installFetchMock } from "../../test/apiFixtures";
import { SelectionScreen } from "./SelectionScreen";
import {
  PAIR_ID,
  countsFixture,
  datasetNodeFixture,
  installApiRouter,
  jsonResponse,
  manifestFixture,
  overrideFixture,
  previewFixture,
  resultFixture,
  ruleFixture,
  sampleItemFixture,
  schemaNodeFixture,
  sourceTreePageFixture,
  syncPairOutFixture,
  undeterminedFixture,
  type Routes,
  type SchemaNodeOut,
  type SelectionRuleOut,
} from "./testHelpers";

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

const RULES: SelectionRuleOut[] = [
  ruleFixture({ id: "rule-a", ordinal: 0, decision: "include", pattern: "analytics.*" }),
  ruleFixture({ id: "rule-b", ordinal: 1, decision: "exclude", pattern: "analytics.staging" }),
  ruleFixture({ id: "rule-c", ordinal: 2, decision: "include", pattern: "analytics.prod_staging" }),
];

const SCHEMAS: SchemaNodeOut[] = [
  schemaNodeFixture({ object_id: "s1", qualified_name: "analytics.sales" }),
  schemaNodeFixture({
    object_id: "s2",
    qualified_name: "analytics.staging",
    result: resultFixture({
      decision: "exclude",
      included: false,
      rule_id: "rule-b",
      explain: "excluded by rule #1 exclude glob 'analytics.staging'",
      undetermined: [undeterminedFixture()],
    }),
  }),
];

function routes(overrides: Routes = {}, manifest = manifestFixture()): Routes {
  return {
    "GET /api/pairs": jsonResponse(200, [syncPairOutFixture()]),
    "GET /api/endpoints/databricks_prod/manifest": jsonResponse(200, manifest),
    [`GET /api/pairs/${PAIR_ID}/rules`]: (request) =>
      jsonResponse(200, new URL(request.url).searchParams.get("scope") === "object" ? RULES : []),
    [`GET /api/pairs/${PAIR_ID}/overrides`]: (request) =>
      jsonResponse(
        200,
        new URL(request.url).searchParams.get("scope") === "object"
          ? [overrideFixture({ object_id: "analytics.prod_staging" })]
          : [],
      ),
    [`GET /api/pairs/${PAIR_ID}/source-tree`]: (request) => {
      const params = new URL(request.url).searchParams;
      const isObject = params.get("scope") === "object";
      return jsonResponse(
        200,
        sourceTreePageFixture({
          nodes: isObject ? SCHEMAS : [datasetNodeFixture()],
          offset: Number(params.get("offset") ?? 0),
        }),
      );
    },
    [`POST /api/pairs/${PAIR_ID}/preview`]: jsonResponse(
      200,
      previewFixture({
        counts: {
          object: countsFixture({ total: 12, included: 5, excluded: 7, undetermined: 2 }),
          dataset: countsFixture({ total: 40, included: 18, excluded: 22, undetermined: 0 }),
        },
        candidates_examined: 52,
        sample: [sampleItemFixture()],
      }),
    ),
    ...overrides,
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

describe("SelectionScreen accessibility", () => {
  it("the pair picker before a pair is chosen has no axe violations", async () => {
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, routes());

    const { container } = renderScreen();
    await screen.findByRole("combobox", { name: /sync pair/i });

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("the loaded screen -- tree, ordered rule editor and live counts -- has no axe violations", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, routes());

    const { container } = renderScreen();
    await selectThePair(user);
    await screen.findByRole("region", { name: "Schemas counts" });

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("an expanded node, its selected-object detail and its override controls have no axe violations", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, routes());

    const { container } = renderScreen();
    await selectThePair(user);

    const [first] = await screen.findAllByRole("treeitem");
    await user.click(first!);
    await screen.findByRole("region", { name: "Selected object" });

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("a disabled matcher with its reason has no axe violations", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, routes({}, manifestFixture({ tags: "na", owners: "absent" })));

    const { container } = renderScreen();
    await selectThePair(user);

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("the empty rule set state has no axe violations", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(
      fetchMock,
      routes({ [`GET /api/pairs/${PAIR_ID}/rules`]: jsonResponse(200, []) }),
    );

    const { container } = renderScreen();
    await user.click(await screen.findByRole("combobox", { name: /sync pair/i }));
    await user.click(await screen.findByRole("option", { name: /prod_databricks_to_qlik/ }));
    await screen.findByText(/No schemas rules yet/i);

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });
});

describe("reordering has a keyboard path, not only a drag one", () => {
  it("moves a rule with a real, focusable button and Enter", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, routes());

    const { container } = renderScreen();
    await selectThePair(user);

    const moveDown = screen.getByRole("button", { name: "Move rule 1 down" });
    // In the normal tab order -- not a mouse-only affordance.
    expect(moveDown.getAttribute("tabindex")).not.toBe("-1");
    moveDown.focus();
    expect(document.activeElement).toBe(moveDown);
    await user.keyboard("{Enter}");

    const patterns = Array.from(container.querySelectorAll<HTMLElement>("[data-rule-key]")).map(
      (row) => within(row).getByRole("textbox").getAttribute("value"),
    );
    expect(patterns).toEqual(["analytics.staging", "analytics.*", "analytics.prod_staging"]);
  });

  it("announces the new evaluation position politely, once, and says what order means", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, routes());

    renderScreen();
    await selectThePair(user);

    await user.click(screen.getByRole("button", { name: "Move rule 1 down" }));

    const regions = screen.getAllByRole("status");
    const announced = regions.map((region) => region.textContent ?? "").join(" | ");
    expect(announced).toMatch(/moved to evaluation position 2 of 3/i);
    expect(announced).toMatch(/last matching rule decides/i);
    for (const region of regions) {
      expect(region).toHaveAttribute("aria-live", "polite");
      expect(region).toHaveAttribute("aria-atomic", "true");
    }
  });

  it("the drag handle is not a focusable control with no keyboard behaviour", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, routes());

    const { container } = renderScreen();
    await selectThePair(user);

    const handles = container.querySelectorAll("[data-drag-handle]");
    expect(handles.length).toBe(3);
    for (const handle of handles) {
      expect(handle).toHaveAttribute("aria-hidden", "true");
      expect(handle.tagName).not.toBe("BUTTON");
      expect(handle.getAttribute("tabindex")).toBeNull();
    }
  });
});

describe("the count region announces on settle, not on every render", () => {
  it("carries the settled figures and the source of the rule set in one polite sentence", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, routes());

    renderScreen();
    await selectThePair(user);
    await screen.findByRole("region", { name: "Schemas counts" });

    await waitFor(() => {
      const announced = screen
        .getAllByRole("status")
        .map((region) => region.textContent ?? "")
        .join(" | ");
      expect(announced).toMatch(/Preview of saved rules/i);
      expect(announced).toMatch(/Schemas: 5 included, 7 excluded, 2 cannot tell, of 12/i);
      expect(announced).toMatch(/Tables & views: 18 included, 22 excluded, 0 cannot tell, of 40/i);
    });
  });
});

describe("the tree is keyboard navigable and states its structure", () => {
  it("moves focus between nodes with the arrow keys and exposes expandability", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, routes());

    renderScreen();
    await selectThePair(user);

    const items = await screen.findAllByRole("treeitem");
    expect(items[0]).toHaveAttribute("aria-expanded", "false");
    expect(items[0]).toHaveAttribute("aria-level", "1");

    items[0]!.focus();
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(items[1]);
  });

  it("keeps every node keyboard reachable when the tree is large enough to be windowed", async () => {
    // jsdom has no layout engine, so `offsetHeight`/`offsetWidth` are 0 and the windowing
    // library computes an empty viewport -- which would make this test pass vacuously against a
    // tree that renders nothing. Giving the scroll container a real measured size is what makes
    // the VIRTUAL code path actually run here, rather than being shipped unexercised.
    const proto = window.HTMLElement.prototype;
    const originalHeight = Object.getOwnPropertyDescriptor(proto, "offsetHeight");
    const originalWidth = Object.getOwnPropertyDescriptor(proto, "offsetWidth");
    Object.defineProperty(proto, "offsetHeight", { configurable: true, get: () => 448 });
    Object.defineProperty(proto, "offsetWidth", { configurable: true, get: () => 900 });

    try {
      const user = userEvent.setup();
      const fetchMock = installFetchMock();
      const many = Array.from({ length: 80 }, (_, index) =>
        schemaNodeFixture({ object_id: `s${index}`, qualified_name: `analytics.schema_${index}` }),
      );
      installApiRouter(
        fetchMock,
        routes({
          [`GET /api/pairs/${PAIR_ID}/source-tree`]: (request) =>
            jsonResponse(
              200,
              sourceTreePageFixture({
                nodes: new URL(request.url).searchParams.get("scope") === "object" ? many : [],
              }),
            ),
        }),
      );

      const { container } = renderScreen();
      await selectThePair(user);

      const items = await screen.findAllByRole("treeitem");
      // Windowed: a slice is mounted, not all 80 -- that IS the responsiveness claim.
      expect(items.length).toBeGreaterThan(0);
      expect(items.length).toBeLessThan(many.length);
      // And what is mounted is still a real, labelled treeitem owned by a real tree, with the
      // set size the whole collection has -- so a screen reader is told "1 of 80", not "1 of 20".
      expect(items[0]).toHaveAttribute("aria-setsize", "80");
      expect(items[0]).toHaveAccessibleName(/analytics\.schema_0/);
      expect(container.querySelector('[role="tree"]')).not.toBeNull();

      items[0]!.focus();
      await user.keyboard("{ArrowDown}");
      expect(document.activeElement).toBe(items[1]);

      const results = await axe.run(container);
      expect(results.violations).toEqual([]);
    } finally {
      if (originalHeight) Object.defineProperty(proto, "offsetHeight", originalHeight);
      else Reflect.deleteProperty(proto, "offsetHeight");
      if (originalWidth) Object.defineProperty(proto, "offsetWidth", originalWidth);
      else Reflect.deleteProperty(proto, "offsetWidth");
    }
  });
});

describe("the screen composes inside the real application shell", () => {
  // This screen cannot be routed from `App.tsx` by this task (T13.7 owns that file in
  // parallel), so it is mounted here inside the REAL `Shell` at the real `/selection` path
  // instead of only being rendered standalone. That is what catches the class of defect a
  // standalone render cannot see: a second landmark, a competing heading level, or a control
  // that only has an accessible name because nothing else on the page claimed it.
  it("renders inside Shell at /selection with no axe violations", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetchMock();
    installApiRouter(fetchMock, {
      ...routes(),
      "GET /api/auth/session": jsonResponse(200, {
        username: "admin",
        csrf_token: "test-csrf-token",
        expires_at: "2026-01-01T00:00:00Z",
      }),
    });

    const { container } = render(
      <ThemeProvider defaultTheme="light">
        <MemoryRouter initialEntries={["/selection"]}>
          <RouterRoutes>
            <Route element={<Shell />}>
              <Route path="/selection" element={<SelectionScreen />} />
            </Route>
          </RouterRoutes>
        </MemoryRouter>
      </ThemeProvider>,
    );

    await selectThePair(user);
    await screen.findByRole("region", { name: "Schemas counts" });

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });
});
