/* The live preview: what the rule set currently in view would select, evaluated by the engine's
 * own evaluator (C4) against the pair's live source.
 *
 * Four things here are honesty requirements, not layout preferences.
 *
 * 1. Whose numbers these are. `PreviewOut.rule_set_source` echoes `"stored"` or `"draft"`, and
 *    the panel says which in words, in a badge, and in the live-region sentence. A draft's
 *    counts and the saved configuration's counts are different claims about the world and must
 *    never be visually conflated.
 *
 * 2. `undetermined` is its own figure and its own state. `ScopeCountsOut` guarantees
 *    `included + excluded == total` -- that partition is what the run will actually do.
 *    `undetermined` is a SEPARATE tally over those same candidates and OVERLAPS them: a
 *    candidate excluded by the default because a tag rule could not be evaluated is counted in
 *    both `excluded` and `undetermined`. So the four figures do not add up to `total`, on
 *    purpose, and the panel says so rather than letting an operator do the arithmetic and
 *    conclude the numbers are broken. Subtracting `undetermined` out of `excluded` to make them
 *    disjoint would report something the engine never said.
 *
 * 3. `truncated` means these are not totals. When the walk stopped at `max_candidates`, every
 *    figure describes only the `candidates_examined` the engine got through, and each tile's
 *    own caption says so -- a truncated count rendered as a total is a lie the operator has no
 *    way to detect.
 *
 * 4. A stale number is marked stale. While a newer draft is being previewed, the figures on
 *    screen belong to the previous one; they stay visible (blanking them helps nobody) but are
 *    explicitly labelled as being recalculated.
 *
 * The live region is one `role="status"` sentence, not one per tile. Its text changes exactly
 * twice per settled edit -- once to "recalculating", once to the finished summary -- because
 * React only rewrites the node when the string differs, so a debounced burst of edits does not
 * become a burst of announcements.
 */
import { useId } from "react";
import {
  Alert,
  AlertDescription,
  Badge,
  Button,
  MetricCard,
  StatePanel,
  Text,
} from "@elabs-ai/components-ui";
import { CircleHelp, TriangleAlert } from "lucide-react";

import { DecisionBadge, UndeterminedBadge } from "./DecisionParts";
import { RULE_SET_SOURCE_LABEL, SCOPE_LABEL } from "./labels";
import type { PreviewOut, PreviewSampleItemOut, RuleScope, ScopeCountsOut } from "./selectionApi";

export type PreviewState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "paused"; reason: string }
  | { status: "error"; message: string }
  | { status: "loaded"; preview: PreviewOut };

export interface PreviewPanelProps {
  state: PreviewState;
  /** The last preview that actually landed, kept on screen while a newer one is in flight. */
  lastPreview: PreviewOut | null;
  /** True while what is displayed no longer describes the rule set currently in the editor. */
  stale: boolean;
  onRetry: () => void;
  onSelectSample: (item: PreviewSampleItemOut) => void;
}

function totalCaption(counts: ScopeCountsOut, truncated: boolean, examined: number): string {
  return truncated
    ? `of ${counts.total} examined before the preview stopped at ${examined} candidates — not a total`
    : `of ${counts.total} in the source`;
}

function ScopeCounts({
  scope,
  counts,
  truncated,
  examined,
}: {
  scope: RuleScope;
  counts: ScopeCountsOut;
  truncated: boolean;
  examined: number;
}) {
  const caption = totalCaption(counts, truncated, examined);
  return (
    <section aria-label={`${SCOPE_LABEL[scope]} counts`} className="flex flex-col gap-2">
      <Text variant="caption" tone="muted" className="uppercase tracking-wide">
        {SCOPE_LABEL[scope]}
      </Text>
      <div className="grid gap-3 sm:grid-cols-3">
        <MetricCard
          label="Included"
          value={counts.included}
          valueFormat="number"
          description={caption}
        />
        <MetricCard
          label="Excluded"
          value={counts.excluded}
          valueFormat="number"
          description={caption}
        />
        <MetricCard
          label="Cannot tell"
          value={counts.undetermined}
          valueFormat="number"
          icon={<CircleHelp />}
          description="Counted again inside included or excluded above — a separate flag, not a third bucket."
        />
      </div>
      <Text variant="caption" tone="muted">
        Included + excluded = {counts.total}
        {truncated ? " examined so far" : ""}. &quot;Cannot tell&quot; ({counts.undetermined}) is
        counted a second time within those two, so the three figures deliberately do not add up to{" "}
        {counts.total}.
      </Text>
    </section>
  );
}

function SampleList({
  items,
  truncated,
  onSelect,
}: {
  items: readonly PreviewSampleItemOut[];
  truncated: boolean;
  onSelect: (item: PreviewSampleItemOut) => void;
}) {
  if (items.length === 0) return null;
  return (
    <section aria-label="Preview sample" className="flex flex-col gap-2">
      <Text variant="caption" tone="muted" className="uppercase tracking-wide">
        Sample
      </Text>
      <Text variant="caption" tone="muted">
        The first {items.length} candidates the preview examined, in the order the engine walks
        them (schemas, then tables and views) — not a random sample
        {truncated ? ", and not from a complete walk" : ""}.
      </Text>
      <ul className="flex flex-col gap-1">
        {items.map((item) => (
          <li key={`${item.scope}-${item.object_id}`}>
            <button
              type="button"
              onClick={() => onSelect(item)}
              className="flex w-full flex-col items-start gap-1 rounded-md border border-border bg-card p-2 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <span className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium">{item.qualified_name ?? item.object_id}</span>
                <DecisionBadge included={item.included} />
                {item.has_undetermined ? <UndeterminedBadge /> : null}
              </span>
              <span className="text-caption text-muted-foreground">{item.explain}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** The one sentence the live region announces. Built from the same fields the tiles render, so
 * what is announced and what is shown cannot drift. */
function summarySentence(preview: PreviewOut): string {
  const label = RULE_SET_SOURCE_LABEL[preview.rule_set_source];
  const parts = (["object", "dataset"] as const).map((scope) => {
    const counts = preview.counts[scope];
    return `${SCOPE_LABEL[scope]}: ${counts.included} included, ${counts.excluded} excluded, ${counts.undetermined} cannot tell, of ${counts.total}`;
  });
  const truncation = preview.truncated
    ? ` Partial: the preview stopped after examining ${preview.candidates_examined} candidates, so these are not totals.`
    : "";
  return `Preview of ${label.toLowerCase()}. ${parts.join(". ")}.${truncation}`;
}

export function PreviewPanel({
  state,
  lastPreview,
  stale,
  onRetry,
  onSelectSample,
}: PreviewPanelProps) {
  const headingId = useId();
  const preview = state.status === "loaded" ? state.preview : lastPreview;
  const showingStale = preview !== null && (stale || state.status === "loading");

  const liveText =
    state.status === "loading"
      ? "Recalculating the preview for the rule set currently in the editor."
      : state.status === "paused"
        ? state.reason
        : state.status === "error"
          ? `The preview failed: ${state.message}`
          : preview
            ? summarySentence(preview)
            : "";

  return (
    <div className="flex flex-col gap-4" aria-labelledby={headingId}>
      <div className="flex flex-wrap items-center gap-2">
        <span id={headingId} className="text-title font-medium">
          Preview
        </span>
        {preview ? (
          <Badge variant={preview.rule_set_source === "draft" ? "warning" : "secondary"}>
            {RULE_SET_SOURCE_LABEL[preview.rule_set_source]}
          </Badge>
        ) : null}
        {showingStale ? <Badge variant="outline">Recalculating…</Badge> : null}
      </div>

      <Text variant="caption" tone="muted">
        {preview?.rule_set_source === "draft"
          ? "These numbers are what the unsaved draft in the editor would select. Nothing has been saved and no run has happened."
          : "These numbers are what the saved rules would select on the next run. Nothing has been written."}
      </Text>

      {state.status === "error" ? (
        <StatePanel
          kind="error"
          title="Could not preview this rule set"
          description={state.message}
          actions={<Button onClick={onRetry}>Retry preview</Button>}
        />
      ) : null}

      {state.status === "paused" ? (
        <Alert variant="warning">
          <AlertDescription>{state.reason}</AlertDescription>
        </Alert>
      ) : null}

      {preview?.truncated ? (
        <Alert variant="warning">
          <AlertDescription>
            <strong>
              <TriangleAlert aria-hidden className="mr-1 inline size-4" />
              Partial result — these are not totals.
            </strong>{" "}
            The preview stopped after examining {preview.candidates_examined} candidates. Every
            figure below counts only those; the source has more. Narrow the rules, or read the
            counts as a sample of a larger source.
          </AlertDescription>
        </Alert>
      ) : null}

      {preview === null && state.status === "loading" ? (
        <StatePanel kind="loading" title="Evaluating the rule set against the source…" />
      ) : null}

      {preview === null && state.status === "idle" ? (
        <StatePanel
          kind="empty"
          title="No preview yet"
          description="Choose a sync pair to evaluate its rules against its source."
        />
      ) : null}

      {preview ? (
        <div className="flex flex-col gap-4" aria-busy={showingStale || undefined}>
          <ScopeCounts
            scope="object"
            counts={preview.counts.object}
            truncated={preview.truncated}
            examined={preview.candidates_examined}
          />
          <ScopeCounts
            scope="dataset"
            counts={preview.counts.dataset}
            truncated={preview.truncated}
            examined={preview.candidates_examined}
          />
          <SampleList
            items={preview.sample}
            truncated={preview.truncated}
            onSelect={onSelectSample}
          />
        </div>
      ) : null}

      <p role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {liveText}
      </p>
    </div>
  );
}
