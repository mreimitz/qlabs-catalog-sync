// Turning a `rule_id` the server reported back into the row the operator is looking at.
//
// The engine names the deciding rule two different ways depending on which rule set it
// evaluated, and both are stable enough to follow:
//
//  * a **stored** rule set reports `str(SelectionRuleRow.id)` -- the rule's real uuid, stable
//    across reordering, which is precisely why `SelectionResult.rule` documents it as "so a
//    console can highlight 'this rule decided' after the operator has dragged the list around";
//  * a **draft** rule set reports `draft-<position>` (`routes/preview.py`'s `_draft_rules`),
//    the index into the flat list the console itself sent -- so the console can always map it
//    back, because it built that list.
//
// Nothing here re-derives WHICH rule decided. That answer arrives from the server on every
// `SelectionResultOut`/`PreviewSampleItemOut`; this module only resolves the identifier in it to
// a position in the draft so the row can be highlighted.
import type { DraftRules } from "./draft";
import { draftRuleIdFor } from "./draft";
import type { RuleScope } from "./selectionApi";
import { RULE_SCOPES } from "./labels";

export interface RuleLocation {
  scope: RuleScope;
  /** Position within that scope, i.e. its evaluation position (0 is evaluated first). */
  index: number;
  key: string;
}

/** Find the draft row that carries this stored rule id. Returns `null` for a rule the draft has
 * since deleted -- which is a real state, not an error: the tree shows the SAVED rules, so it
 * can legitimately name a rule the unsaved draft no longer has. */
export function locateStoredRuleId(draft: DraftRules, ruleId: string): RuleLocation | null {
  for (const scope of RULE_SCOPES) {
    const index = draft[scope].findIndex((rule) => rule.ruleId === ruleId);
    const rule = draft[scope][index];
    if (index >= 0 && rule !== undefined) return { scope, index, key: rule.key };
  }
  return null;
}

/** Find the draft row a `draft-<n>` id names, by rebuilding the same flat order
 * `toPreviewRules` sent: every object-scope rule in evaluation order, then every dataset-scope
 * one. */
export function locateDraftRuleId(draft: DraftRules, ruleId: string): RuleLocation | null {
  let position = 0;
  for (const scope of RULE_SCOPES) {
    for (let index = 0; index < draft[scope].length; index += 1) {
      const rule = draft[scope][index];
      if (rule !== undefined && draftRuleIdFor(position) === ruleId) {
        return { scope, index, key: rule.key };
      }
      position += 1;
    }
  }
  return null;
}

/** Resolve a `rule_id` against whichever rule set produced it. */
export function locateRuleId(
  draft: DraftRules,
  ruleId: string | null,
  ruleSetSource: "stored" | "draft",
): RuleLocation | null {
  if (ruleId === null) return null;
  return ruleSetSource === "draft"
    ? locateDraftRuleId(draft, ruleId)
    : locateStoredRuleId(draft, ruleId);
}
