// Per-object overrides: what a tree node can be pinned BY, and how a pinned node is recognised.
//
// An override is pinned by QUALIFIED NAME, never by an opaque id
// --------------------------------------------------------------
//
// `tests/selection/test_preview_sync_agreement.py` is the certification that the console's
// preview and the real sync reach the same answer, and it holds exactly one divergence open on
// purpose: the sync loop sees one dataset change at a time, with no tree and no browse API, so
// it can only derive a dataset's parent schema from the dataset's own qualified name -- the
// derived parent's identity IS that name, because a lone dataset change carries no reference to
// its schema's stable id. `SelectionRuleSet.override_for` tries a candidate's stable id first
// and its qualified name second, so an object-scope override pinned on a schema's opaque id is
// found by the preview and invisible to the run. The console would promise a sync that never
// happens.
//
// `POST /pairs/{id}/overrides` refuses the wrong form with a 422 naming exactly that. This
// module is what stops the console from ever asking: a node whose `qualified_name` the source
// did not report is NOT pinnable, and its `object_id` (the connector's opaque stable id) is
// never substituted for it. The affordance is disabled with the reason, not hidden and not
// silently sent.
import type { DatasetNodeOut, SchemaNodeOut, SelectionOverrideOut, RuleScope } from "./selectionApi";
import { SCOPE_QUALIFIED_NAME_SHAPE } from "./labels";

/** How many dot-separated segments a scope's qualified name has (`SEGMENTS_BY_SCOPE` in
 * `selection/rules.py`): `catalog.schema` is 2, `catalog.schema.table` is 3. */
const SEGMENTS_BY_SCOPE: Record<RuleScope, number> = { object: 2, dataset: 3 };

export type PinTarget =
  | { pinnable: true; scope: RuleScope; objectId: string }
  | { pinnable: false; reason: string };

/** What this node would be pinned by, or why it cannot be pinned at all. */
export function overridePinTarget(node: SchemaNodeOut | DatasetNodeOut): PinTarget {
  const scope: RuleScope = node.scope;
  const shape = SCOPE_QUALIFIED_NAME_SHAPE[scope];
  const name = node.qualified_name;

  if (name == null || name.trim() === "") {
    return {
      pinnable: false,
      reason: `The source did not report a qualified name for this object, and an override must be pinned by its ${shape} qualified name -- never by the connector's opaque id, which the sync loop cannot see.`,
    };
  }

  const segments = name.split(".");
  if (segments.length !== SEGMENTS_BY_SCOPE[scope] || segments.some((s) => s.trim() === "")) {
    return {
      pinnable: false,
      reason: `The source reported "${name}" for this object, which is not a ${shape} qualified name (${SEGMENTS_BY_SCOPE[scope]} non-empty dot-separated segments). An override pins one exact object by qualified name, so this one cannot be pinned.`,
    };
  }

  return { pinnable: true, scope, objectId: name };
}

/** Overrides for one scope, keyed by the qualified name they pin -- what the tree uses to show
 * "this node is pinned" and which direction the pin points. */
export function overrideIndex(
  overrides: readonly SelectionOverrideOut[],
): Map<string, SelectionOverrideOut> {
  return new Map(overrides.map((override) => [override.object_id, override]));
}
