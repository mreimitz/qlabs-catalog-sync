// Focused, pure-component tests for the exact distinctions the task's own DoD calls a hard
// requirement: three run states, not two ("a run in progress is not a failure, and a run
// abandoned by a killed process is distinguishable from one that genuinely failed"). No
// network involved -- `RunStatusBadge` takes its inputs as props.
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunStatusBadge } from "./RunStatusBadge";

describe("RunStatusBadge", () => {
  it("renders an in-progress run as Running, never as a failure, whatever `status` says", () => {
    // Mutation check 2a: an implementation that reads `status` before `inProgress` would
    // render "OK"/"Failed"/etc for a row that is still running. `status: "ok"` here is
    // deliberately the wrong-looking input a naive "read status first" implementation would
    // mis-render, to make that mutation fail loudly rather than by coincidence.
    render(<RunStatusBadge status="ok" inProgress />);
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.queryByText(/fail/i)).not.toBeInTheDocument();
  });

  it("renders a genuinely failed run as Failed", () => {
    render(<RunStatusBadge status="failed" inProgress={false} sweptStale={false} />);
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("renders a swept-stale (abandoned) run distinctly from a genuine failure", () => {
    // Mutation check 2b: collapsing swept-stale into the same "Failed" chip a genuine failure
    // gets would make this assertion fail -- both the label and the fact that "Failed" is
    // absent are asserted.
    render(<RunStatusBadge status="failed" inProgress={false} sweptStale />);
    expect(screen.getByText(/abandoned/i)).toBeInTheDocument();
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
  });

  it("renders ok, partial and skipped as three more distinct, non-failure labels", () => {
    const { rerender } = render(<RunStatusBadge status="ok" inProgress={false} />);
    expect(screen.getByText("OK")).toBeInTheDocument();

    rerender(<RunStatusBadge status="partial" inProgress={false} />);
    expect(screen.getByText("Partial")).toBeInTheDocument();
    expect(screen.queryByText(/fail/i)).not.toBeInTheDocument();

    rerender(<RunStatusBadge status="skipped" inProgress={false} />);
    expect(screen.getByText("Skipped")).toBeInTheDocument();
  });
});
