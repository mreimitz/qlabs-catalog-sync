// Pure-component tests for `EntityTypeSection` -- no network, `SyncRunReportOut` is passed
// directly as a prop. Each `it` names the mutation check from the task brief it kills.
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EntityTypeSection } from "./EntityTypeSection";
import { droppedFieldFixture, recordReportFixture, syncRunReportFixture, withheldFieldFixture } from "./testHelpers";

describe("EntityTypeSection", () => {
  it("mutation check 1: a no-op plan (every record unchanged/no_op) renders the records, not an empty region", () => {
    render(
      <EntityTypeSection
        report={syncRunReportFixture({
          entity_type: "data_product",
          records: [
            recordReportFixture({ native_key: "dp-1", display_name: "analytics.sales", outcome: "unchanged" }),
            recordReportFixture({ native_key: "dp-2", display_name: "finance.reporting", outcome: "no_op" }),
          ],
        })}
      />,
    );
    // The no-op group renders as a real table with both rows visible -- not folded away.
    // Scoped to the group itself: the counts strip above it renders the SAME label text
    // ("Unchanged", "No-op") for its own figures, so an unscoped query would be ambiguous.
    const heading = screen.getByText("No-ops (2)");
    const group = heading.parentElement as HTMLElement;
    expect(within(group).getByText("analytics.sales")).toBeInTheDocument();
    expect(within(group).getByText("finance.reporting")).toBeInTheDocument();
    expect(within(group).getByText("Unchanged")).toBeInTheDocument();
    expect(within(group).getByText("No-op")).toBeInTheDocument();
  });

  it("mutation check 4: dropped, withheld and target_skipped_fields render as three separately labelled lists, never merged", () => {
    render(
      <EntityTypeSection
        report={syncRunReportFixture({
          records: [
            recordReportFixture({
              native_key: "dp-1",
              display_name: "analytics.sales",
              outcome: "written",
              dropped: [droppedFieldFixture({ field: "documentation", reason: "not_applicable" })],
              withheld: [withheldFieldFixture({ field: "status", reason: "activation_not_opted_in" })],
              target_skipped_fields: ["owners"],
            }),
          ],
        })}
      />,
    );

    const card = screen.getByText("analytics.sales").closest("li");
    expect(card).not.toBeNull();
    const scoped = within(card as HTMLElement);

    const droppedBlock = scoped.getByText("Dropped — the target cannot carry these").closest("div");
    const withheldBlock = scoped.getByText("Withheld by engine policy").closest("div");
    const skippedBlock = scoped.getByText("Target reported not written").closest("div");
    expect(droppedBlock).not.toBeNull();
    expect(withheldBlock).not.toBeNull();
    expect(skippedBlock).not.toBeNull();

    // Each field name appears under its OWN heading, never under either of the other two.
    expect(within(droppedBlock as HTMLElement).getByText(/documentation/)).toBeInTheDocument();
    expect(within(droppedBlock as HTMLElement).queryByText(/status/)).not.toBeInTheDocument();
    expect(within(droppedBlock as HTMLElement).queryByText(/owners/)).not.toBeInTheDocument();

    expect(within(withheldBlock as HTMLElement).getByText(/status/)).toBeInTheDocument();
    expect(within(withheldBlock as HTMLElement).queryByText(/documentation/)).not.toBeInTheDocument();
    expect(within(withheldBlock as HTMLElement).queryByText(/owners/)).not.toBeInTheDocument();

    expect(within(skippedBlock as HTMLElement).getByText(/owners/)).toBeInTheDocument();
    expect(within(skippedBlock as HTMLElement).queryByText(/documentation/)).not.toBeInTheDocument();
    expect(within(skippedBlock as HTMLElement).queryByText(/status/)).not.toBeInTheDocument();
  });

  it("mutation check 5: a failed entity-type plan renders a distinct failure block, not an empty plan", () => {
    render(
      <EntityTypeSection
        report={syncRunReportFixture({
          entity_type: "data_product",
          status: "failed",
          records: [],
          errors: [
            {
              kind: "AuthError",
              message: "the source endpoint rejected the credential",
              endpoint: "databricks_prod",
              native_key: null,
              operation: "list_changed",
              retryable: false,
              fatal: true,
            },
          ],
        })}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(/could not produce a plan/i);
    expect(screen.getByText("the source endpoint rejected the credential")).toBeInTheDocument();
    // A failed plan must never read as "nothing to plan" -- the empty-plan sentence stays out.
    expect(screen.queryByText(/no candidate objects were evaluated/i)).not.toBeInTheDocument();
  });

  it("an empty (but not failed) plan reads as a confident, positive fact, not a blank region", () => {
    render(
      <EntityTypeSection report={syncRunReportFixture({ entity_type: "dataset", status: "ok", records: [] })} />,
    );
    expect(screen.getByText(/no candidate objects were evaluated/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("mutation check 6: an orphan record is worded as reported, never as deleted or removed, and never with the destructive badge", () => {
    render(
      <EntityTypeSection
        report={syncRunReportFixture({
          records: [
            recordReportFixture({
              native_key: "gone-1",
              display_name: "legacy.orders",
              outcome: "orphaned",
              detail: "gone at the source; recorded as an orphan and never deleted at the target",
            }),
          ],
        })}
      />,
    );
    // Scoped to the "Other outcomes" group: the counts strip above it renders the same
    // "Orphaned" label text for its own figure, so an unscoped query would be ambiguous.
    const group = screen.getByText("Other outcomes (1)").parentElement as HTMLElement;
    const badge = within(group).getByText("Orphaned");
    expect(badge.className).not.toContain("bg-destructive");
    expect(within(group).getByText("reported, not deleted")).toBeInTheDocument();
    expect(within(group).getByText(/never deleted at the target/i)).toBeInTheDocument();
    expect(screen.queryByText(/^deleted$/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^removed$/i)).not.toBeInTheDocument();
  });

  it("mutation check 7: skipped and filtered outcomes never render with the destructive badge variant, only failed does", () => {
    render(
      <EntityTypeSection
        report={syncRunReportFixture({
          records: [
            recordReportFixture({ native_key: "s1", outcome: "skipped", reason: "not_selected" }),
            recordReportFixture({ native_key: "f1", outcome: "filtered" }),
            recordReportFixture({ native_key: "x1", outcome: "failed" }),
          ],
        })}
      />,
    );
    // Scoped to the "Other outcomes" group -- the counts strip above renders the same three
    // labels ("Skipped", "Filtered", "Failed") for its own figures.
    const group = screen.getByText("Other outcomes (3)").parentElement as HTMLElement;
    const skippedBadge = within(group).getByText("Skipped");
    const filteredBadge = within(group).getByText("Filtered");
    const failedBadge = within(group).getByText("Failed");
    expect(skippedBadge.className).not.toContain("bg-destructive");
    expect(filteredBadge.className).not.toContain("bg-destructive");
    // The genuine failure DOES get the destructive tone -- proves the assertion above
    // discriminates rather than being vacuously true everywhere.
    expect(failedBadge.className).toContain("bg-destructive");
  });

  it("every section states plainly that a dry run applies nothing", () => {
    render(<EntityTypeSection report={syncRunReportFixture()} />);
    expect(screen.getByText(/no changes applied — dry run/i)).toBeInTheDocument();
  });

  it("renders every RunCountsOut field in the counts strip as its own labelled figure", () => {
    render(
      <EntityTypeSection
        report={syncRunReportFixture({
          counts: {
            read: 10,
            created: 1,
            written: 2,
            unchanged: 3,
            no_op: 4,
            skipped: 5,
            orphaned: 6,
            filtered: 7,
            failed: 8,
          },
        })}
      />,
    );
    const region = screen.getByRole("region", { name: /counts/i });
    expect(within(region).getByText("10")).toBeInTheDocument();
    expect(within(region).getByText("1")).toBeInTheDocument();
    expect(within(region).getByText("8")).toBeInTheDocument();
  });
});
