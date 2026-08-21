// Pure-component tests for `UnresolvedReferencesPanel` -- no network, `SyncRunReportOut[]` is
// passed directly as a prop. Each `it` names the mutation check from the task brief it kills.
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UnresolvedReferencesPanel } from "./UnresolvedReferencesPanel";
import { recordReportFixture, syncRunReportFixture } from "./testHelpers";

describe("UnresolvedReferencesPanel", () => {
  it("mutation check 2: an unresolved dataset member (D2) is visible in the top-level region without expanding any record", () => {
    render(
      <UnresolvedReferencesPanel
        runs={[
          syncRunReportFixture({
            entity_type: "data_product",
            records: [
              recordReportFixture({
                native_key: "dp-1",
                display_name: "analytics.sales",
                outcome: "written",
                target_skipped_fields: ["dataset_refs"],
                detail: "1 of 3 dataset member(s) did not resolve (decision D2)",
              }),
            ],
          }),
        ]}
      />,
    );

    const region = screen.getByRole("region", { name: "Unresolved references" });
    // No click, no expand -- the object and its explanation are already in the DOM.
    expect(within(region).getByText("analytics.sales")).toBeInTheDocument();
    expect(within(region).getByText(/did not resolve \(decision D2\)/)).toBeInTheDocument();
    expect(within(region).getByText("Unresolved dataset members (D2) — 1")).toBeInTheDocument();
  });

  it("mutation check 3: an unresolvable owner (D3) is visible in the top-level region without expanding any record", () => {
    render(
      <UnresolvedReferencesPanel
        runs={[
          syncRunReportFixture({
            entity_type: "data_product",
            records: [
              recordReportFixture({
                native_key: "dp-2",
                display_name: "finance.reporting",
                outcome: "written",
                target_skipped_fields: ["owners"],
                detail: "1 of 2 owner(s) did not match a Qlik user (decision D3)",
              }),
            ],
          }),
        ]}
      />,
    );

    const region = screen.getByRole("region", { name: "Unresolved references" });
    expect(within(region).getByText("finance.reporting")).toBeInTheDocument();
    expect(within(region).getByText(/did not match a Qlik user \(decision D3\)/)).toBeInTheDocument();
    expect(within(region).getByText("Unresolvable owners (D3) — 1")).toBeInTheDocument();
  });

  it("reads as a confident zero, not a blank section, when nothing is unresolved", () => {
    render(<UnresolvedReferencesPanel runs={[syncRunReportFixture({ records: [] })]} />);
    const region = screen.getByRole("region", { name: "Unresolved references" });
    expect(within(region).getByText("Unresolved dataset members (D2) — 0")).toBeInTheDocument();
    expect(within(region).getByText("Unresolvable owners (D3) — 0")).toBeInTheDocument();
    expect(within(region).getAllByText("None found in this plan.")).toHaveLength(2);
  });

  it("always states the dry-run resolution limitation, whether or not anything is unresolved", () => {
    render(<UnresolvedReferencesPanel runs={[syncRunReportFixture({ records: [] })]} />);
    expect(
      screen.getByText(/does not call qlik to resolve either one/i),
    ).toBeInTheDocument();
  });

  it("does not flag a record whose target_skipped_fields names an unrelated field", () => {
    render(
      <UnresolvedReferencesPanel
        runs={[
          syncRunReportFixture({
            records: [
              recordReportFixture({ native_key: "dp-3", target_skipped_fields: ["description"] }),
            ],
          }),
        ]}
      />,
    );
    const region = screen.getByRole("region", { name: "Unresolved references" });
    expect(within(region).getByText("Unresolved dataset members (D2) — 0")).toBeInTheDocument();
    expect(within(region).getByText("Unresolvable owners (D3) — 0")).toBeInTheDocument();
  });
});
