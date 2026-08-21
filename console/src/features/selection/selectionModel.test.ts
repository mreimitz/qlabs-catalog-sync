// The order-sensitive and shape-sensitive parts of this feature, tested without rendering
// anything: evaluation order, what a draft preview carries, what a save sends, what an override
// may be pinned by, and which matchers a manifest allows. Every one of these is a place where a
// plausible implementation produces a screen that lies.
import { describe, expect, it } from "vitest";

import {
  draftFromStored,
  isDirty,
  moveRule,
  planSave,
  sortByOrdinal,
  toPreviewRules,
  type DraftRules,
  type StoredRules,
} from "./draft";
import { locateDraftRuleId, locateStoredRuleId } from "./ruleRefs";
import { overridePinTarget } from "./overrides";
import { groupDatasets, parentSchemaName, schemaNames, storedRulePositions } from "./sourceTree";
import { isMatcherSelectable, matcherSupportFor } from "./matcherSupport";
import {
  datasetNodeFixture,
  manifestFixture,
  ruleFixture,
  schemaNodeFixture,
  unreadableManifestFixture,
} from "./testHelpers";

const R1 = ruleFixture({ id: "r1", ordinal: 0, pattern: "analytics.*" });
const R2 = ruleFixture({ id: "r2", ordinal: 1, decision: "exclude", pattern: "analytics.staging" });
const R3 = ruleFixture({ id: "r3", ordinal: 2, pattern: "analytics.prod_staging" });
const D1 = ruleFixture({
  id: "d1",
  ordinal: 0,
  scope: "dataset",
  pattern: "analytics.sales.*",
});

function storedFixture(): StoredRules {
  // Deliberately NOT in ordinal order, to prove the sort is on `ordinal` and not on position.
  return { object: [R3, R1, R2], dataset: [D1] };
}

describe("evaluation order", () => {
  it("sortByOrdinal orders on the explicit ordinal, not on the array position it arrived in", () => {
    expect(sortByOrdinal([R3, R1, R2]).map((rule) => rule.id)).toEqual(["r1", "r2", "r3"]);
  });

  it("draftFromStored seeds the draft in evaluation order", () => {
    expect(draftFromStored(storedFixture()).object.map((rule) => rule.ruleId)).toEqual([
      "r1",
      "r2",
      "r3",
    ]);
  });

  it("storedRulePositions numbers a saved rule by its evaluation position, 1-based", () => {
    const positions = storedRulePositions(storedFixture());
    expect(positions.get("r1")).toMatchObject({ scope: "object", position: 1, total: 3 });
    expect(positions.get("r3")).toMatchObject({ scope: "object", position: 3, total: 3 });
    expect(positions.get("d1")).toMatchObject({ scope: "dataset", position: 1, total: 1 });
  });

  it("moveRule reorders without losing or duplicating a rule", () => {
    const draft = draftFromStored(storedFixture()).object;
    expect(moveRule(draft, 0, 2).map((rule) => rule.ruleId)).toEqual(["r2", "r3", "r1"]);
    expect(moveRule(draft, 2, 0).map((rule) => rule.ruleId)).toEqual(["r3", "r1", "r2"]);
    expect(moveRule(draft, 0, 0).map((rule) => rule.ruleId)).toEqual(["r1", "r2", "r3"]);
  });

  it("a reordered draft is dirty even though every rule is unchanged", () => {
    const stored = storedFixture();
    const draft = draftFromStored(stored);
    expect(isDirty(draft, stored)).toBe(false);
    expect(isDirty({ ...draft, object: moveRule(draft.object, 0, 1) }, stored)).toBe(true);
  });
});

describe("a draft preview request", () => {
  it("carries BOTH scopes, because PreviewRequest.rules replaces the whole rule set", () => {
    const rules = toPreviewRules(draftFromStored(storedFixture()));
    expect(rules.map((rule) => `${rule.scope}:${rule.pattern}`)).toEqual([
      "object:analytics.*",
      "object:analytics.staging",
      "object:analytics.prod_staging",
      "dataset:analytics.sales.*",
    ]);
  });

  it("emits the wire field names the API declares, not the draft's own", () => {
    const [first] = toPreviewRules(draftFromStored(storedFixture()));
    expect(first).toEqual({
      scope: "object",
      decision: "include",
      matcher_kind: "glob",
      pattern: "analytics.*",
    });
  });

  it("resolves a draft preview's synthetic rule id back to the row that produced it", () => {
    const draft = draftFromStored(storedFixture());
    // "draft-3" is the fourth entry of the flat list: the dataset-scope rule.
    expect(locateDraftRuleId(draft, "draft-3")).toMatchObject({ scope: "dataset", index: 0 });
    expect(locateDraftRuleId(draft, "draft-1")).toMatchObject({ scope: "object", index: 1 });
  });

  it("resolves a stored rule id to its draft row", () => {
    const draft = draftFromStored(storedFixture());
    expect(locateStoredRuleId(draft, "r2")).toMatchObject({ scope: "object", index: 1 });
    expect(locateStoredRuleId(draft, "never-existed")).toBeNull();
  });
});

describe("planSave", () => {
  it("names the complete ordered id list for the reorder, in draft order", () => {
    const stored = storedFixture();
    const draft: DraftRules = { ...draftFromStored(stored) };
    draft.object = moveRule(draft.object, 0, 2);
    const [objectPlan] = planSave(draft, stored);
    expect(objectPlan?.orderedKeys).toEqual(["stored-r2", "stored-r3", "stored-r1"]);
    expect(objectPlan?.creates).toEqual([]);
    expect(objectPlan?.updates).toEqual([]);
    expect(objectPlan?.deletes).toEqual([]);
  });

  it("splits a mixed edit into creates, updates and deletes", () => {
    const stored = storedFixture();
    const draft = draftFromStored(stored);
    const kept = draft.object[0];
    const edited = draft.object[1];
    if (kept === undefined || edited === undefined) throw new Error("fixture");
    const next: DraftRules = {
      object: [
        kept,
        { ...edited, pattern: "analytics.scratch" },
        { key: "new-1", ruleId: null, scope: "object", decision: "exclude", matcherKind: "glob", pattern: "finance.*" },
      ],
      dataset: [],
    };
    const [objectPlan, datasetPlan] = planSave(next, stored);
    expect(objectPlan?.creates.map((rule) => rule.pattern)).toEqual(["finance.*"]);
    expect(objectPlan?.updates.map((entry) => entry.ruleId)).toEqual(["r2"]);
    expect(objectPlan?.deletes).toEqual(["r3"]);
    expect(datasetPlan?.deletes).toEqual(["d1"]);
  });
});

describe("an override's pin target", () => {
  it("is the object's qualified name, never the connector's opaque object_id", () => {
    const node = schemaNodeFixture({
      object_id: "01234567-89ab-cdef-0123-456789abcdef",
      qualified_name: "analytics.sales",
    });
    expect(overridePinTarget(node)).toEqual({
      pinnable: true,
      scope: "object",
      objectId: "analytics.sales",
    });
  });

  it("is the three-segment qualified name for a dataset", () => {
    expect(overridePinTarget(datasetNodeFixture())).toEqual({
      pinnable: true,
      scope: "dataset",
      objectId: "analytics.sales.orders",
    });
  });

  it("refuses to pin a node the source gave no qualified name for", () => {
    const target = overridePinTarget(schemaNodeFixture({ qualified_name: null }));
    expect(target.pinnable).toBe(false);
    expect(target.pinnable === false && target.reason).toMatch(/qualified name/i);
  });

  it("refuses a name that is not shaped like the scope's qualified name", () => {
    const target = overridePinTarget(schemaNodeFixture({ qualified_name: "analytics" }));
    expect(target.pinnable).toBe(false);
    expect(target.pinnable === false && target.reason).toMatch(/catalog\.schema/);
  });
});

describe("grouping the dataset stream under its schemas", () => {
  it("files a dataset under the catalog.schema prefix of its qualified name", () => {
    expect(parentSchemaName(datasetNodeFixture())).toBe("analytics.sales");
    expect(parentSchemaName(datasetNodeFixture({ qualified_name: null }))).toBeNull();
    expect(parentSchemaName(datasetNodeFixture({ qualified_name: "a.b" }))).toBeNull();
  });

  it("never drops a dataset whose schema has not been read yet", () => {
    const known = schemaNames([schemaNodeFixture({ qualified_name: "analytics.sales" })]);
    const grouped = groupDatasets(
      [
        datasetNodeFixture({ object_id: "t1", qualified_name: "analytics.sales.orders" }),
        datasetNodeFixture({ object_id: "t2", qualified_name: "finance.reporting.ledger" }),
        datasetNodeFixture({ object_id: "t3", qualified_name: null }),
      ],
      known,
    );
    expect(grouped.byParent.get("analytics.sales")?.map((n) => n.object_id)).toEqual(["t1"]);
    expect(grouped.unparented.map((n) => n.object_id)).toEqual(["t2", "t3"]);
  });
});

describe("matcher support from the source's capability manifest", () => {
  it("allows a glob unconditionally -- a qualified name is not a manifest field", () => {
    const support = matcherSupportFor(manifestFixture({ tags: "na" }), "object", "databricks_prod");
    expect(isMatcherSelectable(support.glob)).toBe(true);
  });

  it("refuses a tag matcher when the manifest declares tags 'na', naming why", () => {
    const support = matcherSupportFor(manifestFixture({ tags: "na" }), "object", "databricks_prod");
    expect(support.tag.state).toBe("unavailable");
    expect(isMatcherSelectable(support.tag)).toBe(false);
    expect(support.tag.state !== "available" && support.tag.reason).toMatch(/"na"/);
  });

  it("refuses a tag matcher when the manifest declares no tags field at all", () => {
    const support = matcherSupportFor(
      manifestFixture({ tags: "absent" }),
      "dataset",
      "databricks_prod",
    );
    expect(support.tag.state).toBe("unavailable");
    expect(support.tag.state !== "available" && support.tag.reason).toMatch(/no "tags" field/);
  });

  it("refuses every fact matcher when the entity type itself is unsupported", () => {
    const support = matcherSupportFor(
      manifestFixture({ supported: false }),
      "object",
      "databricks_prod",
    );
    expect(support.tag.state).toBe("unavailable");
    expect(support.owner.state).toBe("unavailable");
  });

  it("does NOT claim the source cannot report tags when the manifest could not be read", () => {
    const support = matcherSupportFor(
      unreadableManifestFixture("databricks_prod", "the workspace refused the request"),
      "object",
      "databricks_prod",
    );
    expect(support.tag.state).toBe("unknown");
    // Unknown must stay selectable: refusing a rule on the strength of a manifest nobody read
    // would be a fabricated fact, not a safe default.
    expect(isMatcherSelectable(support.tag)).toBe(true);
    expect(support.tag.state !== "available" && support.tag.reason).toMatch(/refused the request/);
  });

  it("reads the entity type its scope actually walks", () => {
    const manifest = manifestFixture();
    if (manifest.manifest) {
      manifest.manifest.entities.dataset = {
        ...manifest.manifest.entities.data_product!,
        fields: {},
      };
    }
    expect(matcherSupportFor(manifest, "object", "e").tag.state).toBe("available");
    expect(matcherSupportFor(manifest, "dataset", "e").tag.state).toBe("unavailable");
  });
});
