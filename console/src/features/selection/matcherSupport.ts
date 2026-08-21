// Which matcher kinds the pair's SOURCE can actually be measured against, and why not when it
// cannot -- "matchers the source cannot support are disabled with the reason shown".
//
// Where the answer comes from, and why it is a capability lookup rather than a decision
// -------------------------------------------------------------------------------------
//
// The engine already answers this exact question, in exactly one place:
// `selection/source_tree.py`'s `tags_offered` / `owners_offered`, which ask the source's
// capability manifest whether the field is declared at all for an entity type and, if so,
// whether its mode is anything other than `na` (RM-01 decision D6: Databricks `tags` is `ro`
// with a SQL warehouse configured and `na` without one). This module mirrors that predicate --
// `supports(entity_type)`, then `field_capability(entity_type, field).mode !== "na"` -- against
// `EndpointManifestOut`, the wire form of the same manifest.
//
// That is deliberately NOT the thing C4 forbids. C4 forbids the console re-deriving an
// INCLUSION DECISION -- whether an object is in or out, and which rule decided -- because a
// second implementation of that can disagree with the run. Nothing here decides anything about
// any object: it decides whether a rule the operator has not written yet could ever reach a
// verdict, which is a property of the endpoint, not of a candidate. There is no API that
// answers "which matchers can this source evaluate" directly, so this is the only place the
// question can be asked from the browser at all -- see the task report's seam list.
//
// Three states, not two
// ---------------------
//
// `unavailable` is a real claim about the source and must only be made from a manifest that was
// actually read. `GET /api/endpoints/{name}/manifest` can legitimately come back with
// `manifest: null` and an `unavailable_reason` (the tenant would not answer). Reporting that as
// "the source cannot report tags" would be a fabricated fact, and disabling the matcher on the
// strength of it would block a rule that may be perfectly valid. So an unreadable manifest is
// its own `unknown` state: the matcher stays selectable, and the reason says the manifest could
// not be read rather than pretending it said no.
import type { EndpointManifestOut, EntityType, MatcherKind, RuleScope } from "./selectionApi";
import { MATCHER_LABEL, SCOPE_LABEL } from "./labels";

/** Which neutral entity type a scope's candidates are (`_ENTITY_TYPES_BY_SCOPE` in
 * `routes/preview.py`): object scope walks `data_product`, dataset scope walks `dataset`. */
export const ENTITY_TYPE_BY_SCOPE: Record<RuleScope, EntityType> = {
  object: "data_product",
  dataset: "dataset",
};

/** Which candidate fact each matcher kind consults (`_FACT_BY_MATCHER` in `selection/rules.py`).
 * A glob reads the qualified name, which every candidate carries intrinsically -- there is no
 * manifest field for it, so a glob rule is never gated by capability. */
const MANIFEST_FIELD_BY_MATCHER: Partial<Record<MatcherKind, string>> = {
  tag: "tags",
  owner: "owners",
};

export type MatcherAvailability =
  | { state: "available" }
  | { state: "unavailable"; reason: string }
  | { state: "unknown"; reason: string };

export type MatcherSupport = Record<MatcherKind, MatcherAvailability>;

/** Decide one matcher kind against one already-read manifest, for one scope. */
function availabilityFor(
  manifest: EndpointManifestOut | null,
  scope: RuleScope,
  matcher: MatcherKind,
  endpointName: string,
): MatcherAvailability {
  const field = MANIFEST_FIELD_BY_MATCHER[matcher];
  if (field === undefined) {
    // glob: the qualified name is intrinsic to a candidate, not a manifest-declared field.
    return { state: "available" };
  }

  if (manifest === null) {
    return {
      state: "unknown",
      reason: `The capability manifest for source endpoint "${endpointName}" has not been read yet, so whether it can report ${field} is unknown.`,
    };
  }
  if (manifest.manifest == null) {
    const why = manifest.unavailable_reason ?? "the connector did not return one";
    return {
      state: "unknown",
      reason: `Source endpoint "${endpointName}" did not return a capability manifest (${why}), so whether it can report ${field} is unknown. A ${MATCHER_LABEL[matcher].toLowerCase()} rule is still allowed, but it may come back undetermined for every object.`,
    };
  }

  const entityType = ENTITY_TYPE_BY_SCOPE[scope];
  const entity = manifest.manifest.entities[entityType];
  const scopeLabel = SCOPE_LABEL[scope].toLowerCase();

  if (entity === undefined || !entity.supported) {
    return {
      state: "unavailable",
      reason: `Source endpoint "${endpointName}" declares no support for the "${entityType}" entity type at all, so it cannot report ${field} for ${scopeLabel}.`,
    };
  }

  const capability = entity.fields[field];
  if (capability === undefined) {
    return {
      state: "unavailable",
      reason: `Source endpoint "${endpointName}" declares no "${field}" field for "${entityType}", so a ${MATCHER_LABEL[matcher].toLowerCase()} rule could never reach a verdict for ${scopeLabel}.`,
    };
  }
  if (capability.mode === "na") {
    return {
      state: "unavailable",
      reason: `Source endpoint "${endpointName}" declares "${field}" as "na" (no equivalent, or not currently readable) for "${entityType}", so a ${MATCHER_LABEL[matcher].toLowerCase()} rule could never reach a verdict for ${scopeLabel}.`,
    };
  }

  return { state: "available" };
}

/** The per-matcher availability for one scope of one pair's source endpoint. */
export function matcherSupportFor(
  manifest: EndpointManifestOut | null,
  scope: RuleScope,
  endpointName: string,
): MatcherSupport {
  return {
    glob: availabilityFor(manifest, scope, "glob", endpointName),
    tag: availabilityFor(manifest, scope, "tag", endpointName),
    owner: availabilityFor(manifest, scope, "owner", endpointName),
  };
}

/** Whether a matcher may be chosen. Only a manifest that was actually read and actually said
 * "no" disables one -- an unread manifest never does (see the module doc comment). */
export function isMatcherSelectable(availability: MatcherAvailability): boolean {
  return availability.state !== "unavailable";
}
