// The unsaved rule draft: what the operator is editing, how it becomes a preview request, and
// how it becomes a sequence of saves. Pure functions plus one executor -- no React, so the
// order-sensitive parts are testable without rendering anything.
//
// Rule order IS the meaning (C3)
// ------------------------------
//
// Evaluation runs low ordinal to high and the **last** matching rule decides. Not the first --
// "first match wins" and "last match wins" are different systems, and C3 chose the latter
// precisely so an operator can carve an exception out of a broad rule by appending to the list
// ("include analytics.*", then "exclude analytics.staging", then "include
// analytics.prod_staging"). Everything here therefore treats the array's own order as the
// evaluation order, and `draftFromStored` sorts on the server's explicit `ordinal` field rather
// than trusting the array position it arrived in.
//
// A draft rule list replaces the WHOLE rule set, both scopes
// ----------------------------------------------------------
//
// `POST /pairs/{id}/preview` with `rules` set evaluates exactly that list and nothing else
// (`routes/preview.py`'s `_build_preview_rule_set`: it calls `_draft_rules(payload.rules)`
// INSTEAD of `_stored_rules`, for both scopes at once). Sending only the scope the operator is
// currently looking at would therefore silently delete the other scope's rules from the
// preview -- the object-scope counts would be right and the dataset-scope counts would be a
// fiction. `toPreviewRules` always emits both scopes for that reason.
//
// Saving is a sequence, and the order of that sequence is not arbitrary
// ---------------------------------------------------------------------
//
// There is no bulk "save this rule set" route, so a save is: create the new rules, update the
// edited ones, delete the removed ones, then rewrite the order with the COMPLETE ordered id
// list. Deletes must land before the reorder because `reorder_selection_rules` refuses a list
// that does not name exactly the existing ids for that `(pair, scope)`; creates must land
// before it because a rule with no id yet cannot be named in one. The reorder is sent whenever
// the resulting list is non-empty, even when the order looks unchanged -- one extra idempotent
// request buys the guarantee that what was saved is exactly what the draft showed, rather than
// a guess about whether an interleaved create/delete moved anything.
import {
  createRule,
  deleteRule,
  reorderRules,
  updateRule,
  type ApiError_,
  type DraftSelectionRuleIn,
  type MatcherKind,
  type RuleScope,
  type SelectionDecision,
  type SelectionRuleOut,
} from "./selectionApi";
import { RULE_SCOPES } from "./labels";

/** One rule as the operator is editing it. `ruleId` is `null` for a rule that exists only in
 * this draft; `key` is a local identity that survives reordering and renaming so React and the
 * reorder controls have something stable to hold on to before the server has given it an id. */
export interface DraftRule {
  key: string;
  ruleId: string | null;
  scope: RuleScope;
  decision: SelectionDecision;
  matcherKind: MatcherKind;
  pattern: string;
}

export type DraftRules = Record<RuleScope, DraftRule[]>;
export type StoredRules = Record<RuleScope, SelectionRuleOut[]>;

export const EMPTY_DRAFT: DraftRules = { object: [], dataset: [] };
export const EMPTY_STORED: StoredRules = { object: [], dataset: [] };

let keyCounter = 0;

/** A local key for a rule that has no server id yet. Never sent anywhere. */
export function newDraftKey(): string {
  keyCounter += 1;
  return `draft-key-${keyCounter}`;
}

/** Evaluation order, taken from the explicit `ordinal` field rather than array position.
 *
 * `routes/selection.py` returns rules ordinal-ascending, so in practice the two agree -- but
 * `ordinal` is the contract ("evaluation order is never left to be inferred from JSON array
 * position alone, which a client library, a diff tool, or a careless re-serialization could
 * silently scramble") and array order is the convenience. Sorting on the contract is what makes
 * a scrambled array render right instead of rendering a lie. `id` breaks a tie so the result is
 * deterministic even against a malformed response with a duplicated ordinal. */
export function sortByOrdinal(rules: readonly SelectionRuleOut[]): SelectionRuleOut[] {
  return [...rules].sort((a, b) => a.ordinal - b.ordinal || a.id.localeCompare(b.id));
}

export function draftRuleFromStored(rule: SelectionRuleOut): DraftRule {
  return {
    key: `stored-${rule.id}`,
    ruleId: rule.id,
    scope: rule.scope,
    decision: rule.decision,
    matcherKind: rule.matcher_kind,
    pattern: rule.pattern,
  };
}

/** Seed a fresh draft from what the server has stored -- also what "Discard" resets to. */
export function draftFromStored(stored: StoredRules): DraftRules {
  return {
    object: sortByOrdinal(stored.object).map(draftRuleFromStored),
    dataset: sortByOrdinal(stored.dataset).map(draftRuleFromStored),
  };
}

/** The whole draft as one `DraftSelectionRuleIn[]`, both scopes, each scope in evaluation
 * order -- see the module doc comment for why the other scope is never omitted. */
export function toPreviewRules(draft: DraftRules): DraftSelectionRuleIn[] {
  return RULE_SCOPES.flatMap((scope) =>
    draft[scope].map((rule) => ({
      scope: rule.scope,
      decision: rule.decision,
      matcher_kind: rule.matcherKind,
      pattern: rule.pattern,
    })),
  );
}

/** The synthetic id `routes/preview.py` gives the rule at `index` of a draft rule list
 * (`_draft_rules`: `f"draft-{index}"`). Lets a draft preview's `rule_id` be resolved back to
 * the exact row the operator is looking at, instead of being rendered as an opaque token. */
export function draftRuleIdFor(index: number): string {
  return `draft-${index}`;
}

function sameRule(draft: DraftRule, stored: SelectionRuleOut): boolean {
  return (
    draft.decision === stored.decision &&
    draft.matcherKind === stored.matcher_kind &&
    draft.pattern === stored.pattern
  );
}

/** Whether the draft differs from what is saved -- in content, in membership, or in ORDER.
 * Order counts because order is the meaning. */
export function isDirty(draft: DraftRules, stored: StoredRules): boolean {
  return RULE_SCOPES.some((scope) => {
    const storedOrder = sortByOrdinal(stored[scope]);
    const draftOrder = draft[scope];
    if (storedOrder.length !== draftOrder.length) return true;
    return draftOrder.some((rule, index) => {
      const at = storedOrder[index];
      if (at === undefined) return true;
      if (rule.ruleId !== at.id) return true;
      return !sameRule(rule, at);
    });
  });
}

/** Move the rule at `from` to `to`, returning a new array. The one primitive both the drag
 * handler and the keyboard move buttons go through, so they cannot disagree. */
export function moveRule(rules: readonly DraftRule[], from: number, to: number): DraftRule[] {
  if (from === to || from < 0 || to < 0 || from >= rules.length || to >= rules.length) {
    return [...rules];
  }
  const next = [...rules];
  const [moved] = next.splice(from, 1);
  if (moved === undefined) return [...rules];
  next.splice(to, 0, moved);
  return next;
}

export interface ScopeSavePlan {
  scope: RuleScope;
  creates: DraftRule[];
  updates: { ruleId: string; rule: DraftRule }[];
  deletes: string[];
  /** The draft's evaluation order, as keys. Resolved to real ids after the creates land. */
  orderedKeys: string[];
}

/** What one save has to do, per scope. Pure -- the executor below is what performs it. */
export function planSave(draft: DraftRules, stored: StoredRules): ScopeSavePlan[] {
  return RULE_SCOPES.map((scope) => {
    const storedById = new Map(stored[scope].map((rule) => [rule.id, rule]));
    const draftIds = new Set(
      draft[scope].map((rule) => rule.ruleId).filter((id): id is string => id !== null),
    );
    const creates: DraftRule[] = [];
    const updates: { ruleId: string; rule: DraftRule }[] = [];

    for (const rule of draft[scope]) {
      if (rule.ruleId === null) {
        creates.push(rule);
        continue;
      }
      const existing = storedById.get(rule.ruleId);
      // A draft row naming an id the server no longer has (deleted from another session) is
      // treated as a create rather than a PATCH that would 404 -- the operator's intent is
      // "this rule should exist", and re-creating it is the honest reading of that.
      if (existing === undefined) {
        creates.push(rule);
        continue;
      }
      if (!sameRule(rule, existing)) updates.push({ ruleId: rule.ruleId, rule });
    }

    return {
      scope,
      creates,
      updates,
      deletes: stored[scope].map((rule) => rule.id).filter((id) => !draftIds.has(id)),
      orderedKeys: draft[scope].map((rule) => rule.key),
    };
  });
}

export type SaveOutcome =
  | { ok: true }
  | {
      ok: false;
      error: ApiError_;
      step: string;
      /** The draft row the failing request was about, so the error can be rendered against it
       * rather than only as a banner. `null` when the step was not about one row. */
      ruleKey: string | null;
      partiallyApplied: boolean;
    };

/** Execute a save plan against the API.
 *
 * Stops at the first failure and reports whether anything had already been written, because a
 * multi-request save has no transaction and pretending otherwise is exactly the kind of thing
 * that makes a console lie. The caller re-reads the stored rules either way, so what the screen
 * shows afterwards is the server's state, never the optimistic one. */
export async function executeSave(
  pairId: string,
  plans: readonly ScopeSavePlan[],
): Promise<SaveOutcome> {
  let wrote = false;

  for (const plan of plans) {
    const idByKey = new Map<string, string>();

    for (const rule of plan.creates) {
      // `ordinal` omitted on purpose: the server appends, and the reorder below is what fixes
      // the final evaluation order.
      const result = await createRule(pairId, {
        scope: plan.scope,
        decision: rule.decision,
        matcher_kind: rule.matcherKind,
        pattern: rule.pattern,
      });
      if (!result.ok) {
        return {
          ok: false,
          error: result.error,
          step: `creating a ${plan.scope}-scope rule`,
          ruleKey: rule.key,
          partiallyApplied: wrote,
        };
      }
      wrote = true;
      idByKey.set(rule.key, result.data.id);
    }

    for (const { ruleId, rule } of plan.updates) {
      const result = await updateRule(pairId, ruleId, {
        decision: rule.decision,
        matcher_kind: rule.matcherKind,
        pattern: rule.pattern,
      });
      if (!result.ok) {
        return {
          ok: false,
          error: result.error,
          step: `updating a ${plan.scope}-scope rule`,
          ruleKey: rule.key,
          partiallyApplied: wrote,
        };
      }
      wrote = true;
    }

    for (const ruleId of plan.deletes) {
      const result = await deleteRule(pairId, ruleId);
      if (!result.ok) {
        return {
          ok: false,
          error: result.error,
          step: `deleting a ${plan.scope}-scope rule`,
          ruleKey: null,
          partiallyApplied: wrote,
        };
      }
      wrote = true;
    }

    // The complete ordered id list for this (pair, scope) -- never a move-to-index delta.
    const orderedIds = plan.orderedKeys.map((key) => idByKey.get(key) ?? keyToStoredId(key));
    if (orderedIds.length > 0 && orderedIds.every((id): id is string => id !== null)) {
      const result = await reorderRules(pairId, plan.scope, orderedIds);
      if (!result.ok) {
        return {
          ok: false,
          error: result.error,
          step: `reordering ${plan.scope}-scope rules`,
          ruleKey: null,
          partiallyApplied: wrote,
        };
      }
      wrote = true;
    }
  }

  return { ok: true };
}

/** A draft key seeded from a stored rule encodes that rule's id (`stored-<uuid>`); a key made
 * by `newDraftKey` does not, and its id only exists once the create above has returned one. */
function keyToStoredId(key: string): string | null {
  return key.startsWith("stored-") ? key.slice("stored-".length) : null;
}
