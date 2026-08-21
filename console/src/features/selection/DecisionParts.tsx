/* The one place a decision is turned into pixels, shared by the source tree, the selected-node
 * detail and the preview sample -- so the three can never describe the same server answer three
 * different ways.
 *
 * Every value rendered here is copied off a `SelectionResultOut` / `DatasetSelectionOut` /
 * `PreviewSampleItemOut` the engine produced. Nothing in this file computes an inclusion, a
 * count, or a deciding rule: C4's whole point is that the sync loop and the preview call one
 * evaluator, and a console that re-derived any of it client-side would put the second
 * implementation back, on the other side of the wire.
 *
 * "Cannot tell" is not a third decision
 * -------------------------------------
 *
 * `SelectionResultOut.decision` is always include or exclude -- every candidate is exactly one
 * of the two, and that is what the run will actually do. `undetermined` is a SEPARATE list on
 * the same result: rules that could not be evaluated at all because the source cannot supply
 * the fact they need (a tag rule against a source whose manifest reports no tags -- RM-01 D6).
 * Such a rule is neither a match nor a considered non-match, so it does not change the decision
 * -- but any of them could have changed it had the fact been available, which is exactly the
 * situation where a preview and a run can come apart.
 *
 * So the badge pair is deliberate: the decision badge always says what the run will do, and the
 * "Cannot tell" badge is rendered NEXT TO it, never instead of it. Replacing "Excluded" with
 * "Cannot tell" would hide the decision the run is actually going to make; dropping "Cannot
 * tell" would present a decision as settled when a missing fact could flip it.
 */
import { Badge, Text } from "@elabs-ai/components-ui";
import { CircleCheck, CircleHelp, CircleMinus } from "lucide-react";

import { CANDIDATE_FACT_LABEL, DECISION_SOURCE_LABEL, MATCHER_LABEL } from "./labels";
import type { DecisionSource, SelectionResultOut, UndeterminedRuleOut } from "./selectionApi";

/** Included / Excluded -- the partition, straight off `SelectionResultOut.included`. */
export function DecisionBadge({ included }: { included: boolean }) {
  return included ? (
    <Badge variant="success">
      <CircleCheck aria-hidden className="mr-1 size-3" />
      Included
    </Badge>
  ) : (
    <Badge variant="outline">
      <CircleMinus aria-hidden className="mr-1 size-3" />
      Excluded
    </Badge>
  );
}

/** The separate, overlapping flag. Rendered alongside a decision badge, never in place of one. */
export function UndeterminedBadge({ count }: { count?: number }) {
  return (
    <Badge variant="warning">
      <CircleHelp aria-hidden className="mr-1 size-3" />
      {count === undefined ? "Cannot tell" : `Cannot tell (${count})`}
    </Badge>
  );
}

/** Where the decision came from -- override, a rule, or the default. An override is not "a rule
 * at the top of the list": it beats every rule outright and no rule is consulted at all (C3), so
 * it gets its own wording rather than being folded in with rule attribution. */
export function DecisionSourceBadge({ source }: { source: DecisionSource }) {
  return (
    <Badge variant={source === "override" ? "info" : "secondary"}>
      {DECISION_SOURCE_LABEL[source]}
    </Badge>
  );
}

/** Every rule of the candidate's scope that could not be evaluated against it, with the fact
 * that was missing. Never summarized away -- the engine reports all of them, including ones
 * ordered AFTER the rule that decided, because any of them could have changed the outcome. */
export function UndeterminedRuleList({ items }: { items: readonly UndeterminedRuleOut[] }) {
  if (items.length === 0) return null;
  return (
    <ul className="flex flex-col gap-1">
      {items.map((item, index) => (
        <li key={`${item.matcher_kind}-${item.pattern}-${index}`} className="flex flex-col">
          <Text variant="caption" className="font-medium">
            {MATCHER_LABEL[item.matcher_kind]} rule {item.decision} &quot;{item.pattern}&quot; could
            not be evaluated: {CANDIDATE_FACT_LABEL[item.missing]} unknown
          </Text>
          <Text variant="caption" tone="muted">
            {item.explain}
          </Text>
        </li>
      ))}
    </ul>
  );
}

/** One candidate's full answer: the decision, what produced it, the engine's own one-line
 * explanation, and every undetermined rule. `explain` is rendered verbatim rather than
 * re-composed from the parts, so the sentence the console shows is the same sentence a run
 * report would carry for the same object. */
export function SelectionResultDetail({
  result,
  heading,
}: {
  result: SelectionResultOut;
  heading?: string;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      {heading ? (
        <Text variant="caption" tone="muted" className="uppercase tracking-wide">
          {heading}
        </Text>
      ) : null}
      <div className="flex flex-wrap items-center gap-1.5">
        <DecisionBadge included={result.included} />
        <DecisionSourceBadge source={result.source} />
        {result.undetermined.length > 0 ? (
          <UndeterminedBadge count={result.undetermined.length} />
        ) : null}
      </div>
      <Text variant="body">{result.explain}</Text>
      <UndeterminedRuleList items={result.undetermined} />
    </div>
  );
}
