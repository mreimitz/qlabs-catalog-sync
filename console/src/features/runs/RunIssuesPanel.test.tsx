// Pure-component tests for `RunIssuesPanel` -- no network, `RunIssuesOut` is passed directly
// as a prop. Each `it` names the mutation check from the task brief it kills.
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunIssuesPanel } from "./RunIssuesPanel";
import {
  runErrorOutFixture,
  runIssuesOutFixture,
  runItemOutFixture,
  runOrphanIssueOutFixture,
} from "./testHelpers";

describe("RunIssuesPanel", () => {
  it("mutation check 1: renders an orphan as reported, never as deleted or removed, and never with the destructive badge variant", () => {
    render(
      <RunIssuesPanel
        issues={runIssuesOutFixture({
          has_issues: true,
          orphans: [runOrphanIssueOutFixture({ display_name: "Legacy Orders", still_open: true })],
        })}
      />,
    );

    const orphanBadge = screen.getByText("Orphaned -- reported, not deleted");
    expect(orphanBadge).toBeInTheDocument();
    // A mutation that swapped this badge to `variant="destructive"` (the red/alarm tone) would
    // add `bg-destructive` to its class list -- assert it never does.
    expect(orphanBadge.className).not.toContain("bg-destructive");
    // The bare words "Deleted"/"Removed" (as opposed to our own "not deleted" phrasing) must
    // never appear anywhere in the panel.
    expect(screen.queryByText("Deleted")).not.toBeInTheDocument();
    expect(screen.queryByText("Removed")).not.toBeInTheDocument();
    expect(screen.queryByText(/^deleted$/i)).not.toBeInTheDocument();
  });

  it("mutation check 4: skipped and filtered record outcomes never render with the destructive badge variant", () => {
    render(
      <RunIssuesPanel
        issues={runIssuesOutFixture({
          has_issues: true,
          other_outstanding: [
            runItemOutFixture({ id: "item-skipped", native_key: "sales.a", outcome: "skipped" }),
            runItemOutFixture({ id: "item-filtered", native_key: "sales.b", outcome: "filtered" }),
            runItemOutFixture({ id: "item-failed", native_key: "sales.c", outcome: "failed" }),
          ],
        })}
      />,
    );

    const skippedBadge = screen.getByText("Skipped");
    const filteredBadge = screen.getByText("Filtered");
    const failedBadge = screen.getByText("Failed");
    expect(skippedBadge.className).not.toContain("bg-destructive");
    expect(filteredBadge.className).not.toContain("bg-destructive");
    // A genuinely failed record outcome DOES get the destructive tone -- this half of the
    // assertion is what proves the class check above is actually discriminating, not just
    // structurally absent everywhere.
    expect(failedBadge.className).toContain("bg-destructive");
  });

  it("mutation check 5: every issue category shows the object identity that caused it, not just a bare reason", () => {
    render(
      <RunIssuesPanel
        issues={runIssuesOutFixture({
          has_issues: true,
          unresolved_dataset_members: [
            runItemOutFixture({
              id: "member-issue",
              native_key: "sales.orders.customer_id",
              display_name: "customer_id",
              endpoint: "databricks_prod",
              unresolved_fields: ["dataset_refs"],
            }),
          ],
        })}
      />,
    );

    expect(screen.getByText("customer_id")).toBeInTheDocument();
    expect(screen.getByText("sales.orders.customer_id")).toBeInTheDocument();
    expect(screen.getByText("endpoint: databricks_prod")).toBeInTheDocument();
  });

  it("shows an in-progress note, not an empty-issues message, while the run has not finished", () => {
    render(<RunIssuesPanel issues={runIssuesOutFixture({ issues_recorded: false, in_progress: true })} />);
    expect(screen.getByText(/still in progress/i)).toBeInTheDocument();
    expect(screen.queryByText("No issues recorded for this run.")).not.toBeInTheDocument();
  });

  it("renders a calm 'no issues' message when the run finished clean", () => {
    render(<RunIssuesPanel issues={runIssuesOutFixture({ has_issues: false })} />);
    expect(screen.getByText("No issues recorded for this run.")).toBeInTheDocument();
  });

  it("renders a swept-stale error distinctly from an ordinary error", () => {
    render(
      <RunIssuesPanel
        issues={runIssuesOutFixture({
          has_issues: true,
          errors: [runErrorOutFixture({ is_stale_sweep: true, message: "reaped after restart" })],
        })}
      />,
    );
    expect(screen.getByText("Abandoned (process stopped)")).toBeInTheDocument();
    expect(screen.getByText("reaped after restart")).toBeInTheDocument();
  });
});
