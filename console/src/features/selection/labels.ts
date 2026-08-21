// Display labels for the selection vocabulary, in one place so the tree, the rule editor and
// the preview never describe the same enum value two different ways. Mirrors
// `../pairs/labels.ts`'s purpose.
//
// Wording is load-bearing here in a way it is not on other screens, so the choices are stated
// rather than left to taste:
//
//  * "Included" / "Excluded" are the two halves of ONE partition. Every candidate is exactly
//    one of them -- that is what `ScopeCountsOut` guarantees (`included + excluded == total`).
//  * "Cannot tell" is NOT a third member of that partition. It is a separate flag over the
//    same candidates: a rule that could not be evaluated (a tag rule against a source whose
//    manifest cannot report tags -- RM-01 D6) is neither a match nor a considered non-match,
//    so the candidate still has a real decision AND still carries the flag. The engine counts
//    it in both `excluded` and `undetermined`. Rendering it as a replacement for "Excluded"
//    would show a number the real run contradicts, so this file has no label that could be
//    mistaken for one.
//  * Scope wording says what the scope DECIDES, not just what it is called (C5): object scope
//    decides which `catalog.schema` become data products; dataset scope decides which tables
//    and views inside an already-selected schema become that product's members.
import type {
  CandidateFact,
  DecisionSource,
  MatcherKind,
  RuleScope,
  SelectionDecision,
} from "./selectionApi";

export const RULE_SCOPES: readonly RuleScope[] = ["object", "dataset"];

export const SCOPE_LABEL: Record<RuleScope, string> = {
  object: "Schemas",
  dataset: "Tables & views",
};

export const SCOPE_NOUN: Record<RuleScope, string> = {
  object: "schema",
  dataset: "table or view",
};

export const SCOPE_DESCRIPTION: Record<RuleScope, string> = {
  object:
    "Object scope: which catalog.schema become Qlik data products. A schema that is not included here is not synced, and neither is anything inside it.",
  dataset:
    "Dataset scope: which tables and views inside an already-included schema become that data product's members. A table in an excluded schema stays excluded whatever these rules say.",
};

/** The shape a pattern (and an override's pinned object) has per scope -- the same
 * `SEGMENTS_BY_SCOPE` the engine validates against. */
export const SCOPE_QUALIFIED_NAME_SHAPE: Record<RuleScope, string> = {
  object: "catalog.schema",
  dataset: "catalog.schema.table",
};

export const DECISION_LABEL: Record<SelectionDecision, string> = {
  include: "Include",
  exclude: "Exclude",
};

/** The decision as a state, for a node that has already been decided. */
export const DECIDED_LABEL: Record<SelectionDecision, string> = {
  include: "Included",
  exclude: "Excluded",
};

export const MATCHER_LABEL: Record<MatcherKind, string> = {
  glob: "Name glob",
  tag: "Tag",
  owner: "Owner",
};

export const MATCHER_HINT: Record<MatcherKind, string> = {
  glob: "A glob over the object's qualified name.",
  tag: "A source-reported tag, written as 'key' or 'key=value'.",
  owner: "The object's owner e-mail, or a glob like '*@acme.com'.",
};

export const MATCHER_KINDS: readonly MatcherKind[] = ["glob", "tag", "owner"];

/** What produced a decision. "Default" is not a guess -- an empty rule set selects nothing,
 * deliberately (the defect this replaced was a silent default-INCLUDE). */
export const DECISION_SOURCE_LABEL: Record<DecisionSource, string> = {
  override: "Pinned by an override",
  rule: "Decided by a rule",
  default: "No rule matched (default: exclude)",
};

/** The fact a matcher needed and could not get, for an `UndeterminedRuleOut.missing`. */
export const CANDIDATE_FACT_LABEL: Record<CandidateFact, string> = {
  qualified_name: "qualified name",
  tags: "tags",
  owners: "owners",
};

export const RULE_SET_SOURCE_LABEL = {
  stored: "Saved rules",
  draft: "Unsaved draft",
} as const;
