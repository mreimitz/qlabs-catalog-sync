// Maps the server's `ErrorModel` (`code`/`message`/`field`/`entity`) onto the sync-pair
// create/edit form's own fields, so a validation error renders against the offending input
// instead of a generic banner -- the same discipline `../endpoints/errorMapping.ts` (T13.3)
// established for that form. `code` is what this module switches on -- stable per
// `../../api/client.ts`'s own doc comment; `message` is only ever displayed, never matched on.
//
// `pairs.py` (the server route this form talks to) names a bad field in two different ways:
//
//   1. `RequestValidationError` (a bad top-level `SyncPairCreateRequest`/
//      `SyncPairUpdateRequest` field, e.g. `cadence_seconds` not `> 0`) sets `field` to the
//      dot-joined pydantic `loc`, prefixed with `"body."` (`errors.py`'s
//      `_handle_request_validation_error`) -- same convention as the endpoints form.
//   2. `SyncPairAlreadyExistsError` names the conflicting name only via `entity`, not `field`
//      (`errors.py`'s `_handle_sync_pair_already_exists`) -- but `entity` IS the value of this
//      form's own `name` field when the conflict is about the name just submitted. Unlike the
//      endpoint form (where `name` is locked on edit), a pair's `name` is editable via
//      `SyncPairUpdateRequest`, so this applies on both create and edit.
//
// One case the server deliberately does NOT name a field for: `SyncPairEndpointError`
// (`sync_pair_endpoint_invalid` -- a missing endpoint, a disabled endpoint, or the v1
// upstream-only direction guardrail: Qlik as source, or a non-Qlik target) carries no `field`
// and no `entity` at all -- only a human-readable `message` that happens to say "source" or
// "target" in prose (`configstore/service.py`'s `_check_pair_direction` /
// `pairs.py`'s `_reject_disabled_endpoint`). That text is NOT a stable, parseable contract
// (`message` "is human-readable and may be reworded without notice" per `client.ts`), so this
// module does not regex it apart -- it surfaces as a form-level banner. In practice this
// error should be rare: `PairFormSheet.tsx`'s source/target pickers already only offer
// enabled endpoints whose `role` matches the slot being filled (see that file's doc comment),
// so the guardrail is normally caught by the picker before the request is ever sent; this
// mapping is the honest fallback for the cases the picker cannot rule out client-side (e.g. an
// endpoint disabled by a concurrent edit between page load and submit).
import type { ApiError } from "../../api/client";

/** The top-level `SyncPairCreateRequest`/`SyncPairUpdateRequest` fields this form renders one
 * `FieldRow` per. Anything else found in a `field` is not one of these. */
export const TOP_LEVEL_FIELDS = [
  "name",
  "source",
  "target",
  "target_space",
  "entity_types",
  "cadence_seconds",
  "jitter_seconds",
  "manual_edit_policy",
  "activation_opt_in",
  "enabled",
] as const;
export type TopLevelField = (typeof TOP_LEVEL_FIELDS)[number];

export type ClassifiedError =
  | { kind: "field"; field: TopLevelField; message: string }
  | { kind: "form"; message: string };

function isTopLevelField(value: string): value is TopLevelField {
  return (TOP_LEVEL_FIELDS as readonly string[]).includes(value);
}

/** Strip a `RequestValidationError`'s `"body."` prefix and keep only the first path segment --
 * `"body.manual_edit_policy.default"` still resolves to `"manual_edit_policy"`. */
function firstSegment(field: string): string {
  const withoutBodyPrefix = field.startsWith("body.") ? field.slice("body.".length) : field;
  return withoutBodyPrefix.split(".")[0] ?? withoutBodyPrefix;
}

/** Classify one `ApiError` from a create/update pair call into where this form should render
 * it. Every branch is reached by a named test in `PairsScreen.test.tsx` -- see the task
 * report's mutation table. */
export function classifyPairError(error: ApiError): ClassifiedError {
  // sync_pair_already_exists names the conflicting name only via `entity`, not `field` -- but
  // `entity` IS the value of this form's own `name` field (create and edit both -- see the
  // module doc comment).
  if (error.code === "sync_pair_already_exists" && error.entity != null) {
    return { kind: "field", field: "name", message: error.message };
  }

  if (error.field) {
    const segment = firstSegment(error.field);
    if (isTopLevelField(segment)) {
      return { kind: "field", field: segment, message: error.message };
    }
  }

  return { kind: "form", message: error.message };
}
