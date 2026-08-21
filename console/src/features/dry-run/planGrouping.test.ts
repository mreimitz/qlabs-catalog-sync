import { describe, expect, it } from "vitest";

import {
  D2_FIELD,
  D3_FIELD,
  bucketFor,
  groupRecords,
  hasFailedPlan,
  plansNoChanges,
  totalOrphans,
  unresolvedReferences,
} from "./planGrouping";
import {
  countsFixture,
  orphanReportFixture,
  recordReportFixture,
  syncRunReportFixture,
} from "./testHelpers";

describe("bucketFor", () => {
  it("maps created to create", () => {
    expect(bucketFor("created")).toBe("create");
  });
  it("maps written to update", () => {
    expect(bucketFor("written")).toBe("update");
  });
  it("maps unchanged and no_op to no_op", () => {
    expect(bucketFor("unchanged")).toBe("no_op");
    expect(bucketFor("no_op")).toBe("no_op");
  });
  it("maps skipped, filtered, orphaned and failed to other", () => {
    expect(bucketFor("skipped")).toBe("other");
    expect(bucketFor("filtered")).toBe("other");
    expect(bucketFor("orphaned")).toBe("other");
    expect(bucketFor("failed")).toBe("other");
  });
});

describe("groupRecords", () => {
  it("splits records into the four buckets, preserving each bucket's own order", () => {
    const records = [
      recordReportFixture({ native_key: "a", outcome: "created" }),
      recordReportFixture({ native_key: "b", outcome: "written" }),
      recordReportFixture({ native_key: "c", outcome: "unchanged" }),
      recordReportFixture({ native_key: "d", outcome: "no_op" }),
      recordReportFixture({ native_key: "e", outcome: "skipped" }),
      recordReportFixture({ native_key: "f", outcome: "created" }),
    ];
    const grouped = groupRecords(records);
    expect(grouped.creates.map((r) => r.native_key)).toEqual(["a", "f"]);
    expect(grouped.updates.map((r) => r.native_key)).toEqual(["b"]);
    expect(grouped.noOps.map((r) => r.native_key)).toEqual(["c", "d"]);
    expect(grouped.other.map((r) => r.native_key)).toEqual(["e"]);
  });

  it("never drops a record: every bucket's records sum to the input length", () => {
    const records = (
      ["created", "written", "unchanged", "no_op", "skipped", "orphaned", "filtered", "failed"] as const
    ).map((outcome, index) => recordReportFixture({ native_key: `r${index}`, outcome }));
    const grouped = groupRecords(records);
    const total =
      grouped.creates.length + grouped.updates.length + grouped.noOps.length + grouped.other.length;
    expect(total).toBe(records.length);
  });
});

describe("unresolvedReferences", () => {
  it("finds a record flagged with the D2 field name (dataset_refs) as a dataset-member reference", () => {
    const runs = [
      syncRunReportFixture({
        entity_type: "data_product",
        records: [
          recordReportFixture({
            native_key: "dp-1",
            outcome: "written",
            target_skipped_fields: [D2_FIELD],
          }),
        ],
      }),
    ];
    const found = unresolvedReferences(runs);
    expect(found).toHaveLength(1);
    expect(found[0]).toMatchObject({ kind: "dataset_member", entityType: "data_product" });
  });

  it("finds a record flagged with the D3 field name (owners) as an owner reference", () => {
    const runs = [
      syncRunReportFixture({
        entity_type: "data_product",
        records: [
          recordReportFixture({
            native_key: "dp-2",
            outcome: "written",
            target_skipped_fields: [D3_FIELD],
          }),
        ],
      }),
    ];
    const found = unresolvedReferences(runs);
    expect(found).toHaveLength(1);
    expect(found[0]).toMatchObject({ kind: "owner", entityType: "data_product" });
  });

  it("reports one entry per kind when a record names both an unresolved dataset member and an unresolvable owner", () => {
    const runs = [
      syncRunReportFixture({
        records: [
          recordReportFixture({
            native_key: "dp-3",
            outcome: "written",
            target_skipped_fields: [D2_FIELD, D3_FIELD],
          }),
        ],
      }),
    ];
    expect(unresolvedReferences(runs)).toHaveLength(2);
  });

  it("does not flag a record whose target_skipped_fields names an unrelated field", () => {
    const runs = [
      syncRunReportFixture({
        records: [
          recordReportFixture({ native_key: "dp-4", target_skipped_fields: ["description"] }),
        ],
      }),
    ];
    expect(unresolvedReferences(runs)).toHaveLength(0);
  });

  it("finds references across every entity type's report, not only the first", () => {
    const runs = [
      syncRunReportFixture({
        entity_type: "data_product",
        records: [recordReportFixture({ native_key: "dp-1", target_skipped_fields: [] })],
      }),
      syncRunReportFixture({
        entity_type: "dataset",
        records: [
          recordReportFixture({ native_key: "ds-1", target_skipped_fields: [D3_FIELD] }),
        ],
      }),
    ];
    const found = unresolvedReferences(runs);
    expect(found).toHaveLength(1);
    expect(found[0]?.entityType).toBe("dataset");
  });
});

describe("plansNoChanges", () => {
  it("is true when created and written are both zero across every run", () => {
    const runs = [
      syncRunReportFixture({ counts: countsFixture({ created: 0, written: 0, unchanged: 5 }) }),
      syncRunReportFixture({ counts: countsFixture({ created: 0, written: 0, skipped: 2 }) }),
    ];
    expect(plansNoChanges(runs)).toBe(true);
  });

  it("is false when any run would create something", () => {
    const runs = [syncRunReportFixture({ counts: countsFixture({ created: 1 }) })];
    expect(plansNoChanges(runs)).toBe(false);
  });

  it("is false when any run would write something", () => {
    const runs = [syncRunReportFixture({ counts: countsFixture({ written: 1 }) })];
    expect(plansNoChanges(runs)).toBe(false);
  });

  it("ignores skipped and filtered counts -- they are not part of whether anything would change", () => {
    const runs = [
      syncRunReportFixture({
        counts: countsFixture({ created: 0, written: 0, skipped: 40, filtered: 12, failed: 3 }),
      }),
    ];
    expect(plansNoChanges(runs)).toBe(true);
  });
});

describe("hasFailedPlan", () => {
  it("is true when any run's status is failed", () => {
    const runs = [
      syncRunReportFixture({ entity_type: "data_product", status: "ok" }),
      syncRunReportFixture({ entity_type: "dataset", status: "failed" }),
    ];
    expect(hasFailedPlan(runs)).toBe(true);
  });

  it("is false when every run is ok or partial", () => {
    const runs = [
      syncRunReportFixture({ status: "ok" }),
      syncRunReportFixture({ status: "partial" }),
    ];
    expect(hasFailedPlan(runs)).toBe(false);
  });
});

describe("totalOrphans", () => {
  it("flattens orphans across every run's own list", () => {
    const runs = [
      syncRunReportFixture({ orphans: [orphanReportFixture({ native_key: "a" })] }),
      syncRunReportFixture({
        orphans: [orphanReportFixture({ native_key: "b" }), orphanReportFixture({ native_key: "c" })],
      }),
    ];
    expect(totalOrphans(runs).map((o) => o.native_key)).toEqual(["a", "b", "c"]);
  });
});
