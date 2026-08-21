// Display labels shared by `PairsScreen.tsx` (the list columns), `PairFormSheet.tsx` (the
// entity-type checkboxes) and `ManualEditPolicyEditor.tsx` (the per-entity overrides), kept in
// one place so the list and the form never drift into describing the same enum value two
// different ways.
import type { EntityType, ManualEditMode } from "./pairsApi";

/** Every `EntityType` the SDK defines (`components["schemas"]["EntityType"]`), in the same
 * order the schema declares them. v1 is upstream-only with no access-control sync, so there is
 * deliberately no `principal`/`access_binding` entry to offer here -- see the schema's own
 * doc comment. */
export const ENTITY_TYPE_VALUES: readonly EntityType[] = [
  "data_product",
  "dataset",
  "glossary_term",
  "category",
];

export const ENTITY_TYPE_LABEL: Record<EntityType, string> = {
  data_product: "Data product",
  dataset: "Dataset",
  glossary_term: "Glossary term",
  category: "Category",
};

export const MANUAL_MODE_LABEL: Record<ManualEditMode, string> = {
  source_wins: "Source wins",
  preserve_local: "Preserve local edits",
};
