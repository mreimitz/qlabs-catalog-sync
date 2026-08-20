// Maps the server's `ErrorModel` (`code`/`message`/`field`/`entity`) onto the endpoint
// register/edit form's own fields, so a validation error renders against the offending input
// (the task's DoD) instead of a generic banner. `code` is what this module switches on --
// stable per `../../api/client.ts`'s own doc comment; `message` is only ever displayed, never
// matched on.
//
// The server can name a bad field in three different ways, and this module's whole job is
// telling them apart:
//
//   1. A handler sets `field` explicitly to a name this form owns (`errors.py`:
//      `connector_not_registered` -> "connector", `secret_ref_invalid` -> "secret_ref").
//   2. `RequestValidationError` (a bad top-level `EndpointCreateRequest`/`EndpointUpdateRequest`
//      field, e.g. `name` too short) sets `field` to the dot-joined pydantic `loc`, prefixed
//      with `"body."` (`errors.py`'s `_handle_request_validation_error`).
//   3. `InlineSecretRejectedError` sets `field` to a comma-joined list of the connector's own
//      secret-typed settings KEYS the operator tried to submit inline (C2) --
//      `errors.py`: `field=", ".join(exc.fields)`. There is no schema exposed anywhere in this
//      API to know those key names ahead of time (see the task report's "seams" section); this
//      is the ONLY channel the console has for learning one.
//
// One case the server deliberately does NOT name a field for:
// `EndpointSettingsValidationError` (a non-secret settings value that fails the connector's
// own `ConfigModel`, e.g. an unknown key or a wrong type) carries no `field` at all -- only a
// human-readable `message` that happens to embed the bad key(s)
// (`configstore/service.py`'s `_validate_endpoint_settings`: `"{loc}: {msg}; ..."`). That text
// is NOT a stable, parseable contract (`message` "is human-readable and may be reworded
// without notice" per `client.ts`), so this module does not regex it apart -- it is surfaced as
// a `settings-banner`, scoped to the settings section rather than the whole form, which is the
// most specific placement the API actually supports for this one error shape.
import type { ApiError } from "../../api/client";

/** The top-level `EndpointCreateRequest`/`EndpointUpdateRequest` fields this form renders one
 * `FieldRow` per. Anything else found in a `field`/`entity` is not one of these. */
export const TOP_LEVEL_FIELDS = ["name", "connector", "role", "secret_ref", "enabled"] as const;
export type TopLevelField = (typeof TOP_LEVEL_FIELDS)[number];

export type ClassifiedError =
  | { kind: "field"; field: TopLevelField; message: string }
  | { kind: "settings-fields"; keys: string[]; message: string }
  | { kind: "settings-banner"; message: string }
  | { kind: "form"; message: string };

function isTopLevelField(value: string): value is TopLevelField {
  return (TOP_LEVEL_FIELDS as readonly string[]).includes(value);
}

/** Strip a `RequestValidationError`'s `"body."` prefix (case 2 above) and keep only the first
 * path segment -- `"body.settings.host"` still resolves to `"settings"`, which is not one of
 * `TOP_LEVEL_FIELDS`, so it correctly falls through to the generic form banner rather than
 * mis-attaching to a field this form does not render as its own `FieldRow` (settings' own
 * per-key errors only ever arrive via `InlineSecretRejectedError`, case 3). */
function firstSegment(field: string): string {
  const withoutBodyPrefix = field.startsWith("body.") ? field.slice("body.".length) : field;
  return withoutBodyPrefix.split(".")[0] ?? withoutBodyPrefix;
}

/** Classify one `ApiError` from a create/update endpoint call into where this form should
 * render it. Every branch is reached by a named test in `EndpointsScreen.test.tsx` --
 * see the task report's mutation table. */
export function classifyEndpointError(error: ApiError): ClassifiedError {
  if (error.code === "inline_secret_rejected") {
    const keys = (error.field ?? "")
      .split(",")
      .map((key) => key.trim())
      .filter((key) => key.length > 0);
    return { kind: "settings-fields", keys, message: error.message };
  }

  if (error.code === "endpoint_settings_invalid") {
    return { kind: "settings-banner", message: error.message };
  }

  // endpoint_already_exists names the conflicting name only via `entity`, not `field`
  // (`errors.py`'s `_handle_endpoint_already_exists`) -- but `entity` IS the value of this
  // form's own `name` field when the conflict is about this form's `name` input (create only;
  // an update can never hit this error, `name` is not editable).
  if (error.code === "endpoint_already_exists" && error.entity != null) {
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
