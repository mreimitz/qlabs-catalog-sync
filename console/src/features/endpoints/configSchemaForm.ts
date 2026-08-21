// Pure, React-free logic for turning a connector's `config_schema` (a JSON Schema `dict`,
// secrets already stripped server-side -- see `connectors.py`'s `_split_secret_properties`)
// into something `SchemaSettingsForm.tsx` can render as labelled, typed controls.
//
// Kept separate from the component so the parsing/validation rules are unit-testable without
// React Testing Library, and so a future non-form consumer (there is none yet) does not have to
// pull in React to reuse them.
//
// `config_schema` is typed `{[key: string]: JsonValue} | null` in the generated schema, and
// `JsonValue` itself is `unknown` (`schema.ts`: `JsonValue: unknown`) -- the OpenAPI generator
// has no way to describe "arbitrary JSON Schema" more precisely. Every function here treats its
// input as untrusted `unknown` and degrades to `"unsupported"` rather than throwing, because a
// connector's schema is written by connector authors, not by this console, and a shape this
// form does not understand must never crash the form (T13.3's own "degrade honestly" DoD).

/** What kind of control this form renders for one property. `"unsupported"` is the deliberate
 * fallback -- see the module doc and `SchemaSettingsForm.tsx`'s "raw text" control -- for
 * anything that is not exactly one of the five shapes below. A property is NEVER dropped for
 * having an unrecognised shape; it degrades to `"unsupported"` instead. */
export type ConfigFieldKind =
  | { kind: "string" }
  | { kind: "boolean" }
  | { kind: "number" }
  | { kind: "enum"; values: string[] }
  | { kind: "string-array" }
  | { kind: "unsupported" };

export interface ConfigFieldDescriptor {
  /** The property name -- also the `settings` key this field reads/writes. */
  name: string;
  /** Pydantic always emits a Title-Cased `title` for a model field; falls back to a humanised
   * `name` on the off chance a hand-written schema omits it. */
  title: string;
  description?: string;
  required: boolean;
  /** True when the property's schema was `anyOf: [<type>, {"type": "null"}]` -- pydantic's
   * rendering of `X | None` (e.g. `sql_warehouse_id`, `scope`). Drives the "blank clears it"
   * behaviour in `SchemaSettingsForm.tsx`: for a nullable field, clearing the control back to
   * blank removes the key from `settings` entirely rather than sending an explicit empty value
   * a validator might reject (`DatabricksConfig._validate_sql_warehouse_id`: blank is refused,
   * omitted is fine -- these must not be conflated). */
  nullable: boolean;
  /** True when the schema declared a `default` at all (including `default: null`) --
   * distinguished from `hasDefault: false` because `undefined` is not a valid JSON Schema
   * default and pydantic never emits `default: undefined`. */
  hasDefault: boolean;
  /** The declared default value, meaningful only when `hasDefault` is true. */
  default?: unknown;
  field: ConfigFieldKind;
}

export function isJsonObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `"catalog_schema_patterns"` -> `"Catalog Schema Patterns"` -- only used when a property's
 * schema omits `title` (pydantic always sets it; this is a defensive fallback for a hand-written
 * or future non-pydantic schema, never observed from the real connectors). */
function humanize(name: string): string {
  return name
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/** Unwrap pydantic's `Optional[X]` rendering: `{"anyOf": [<X's own schema>, {"type": "null"}],
 * "default": ..., "title": ..., "description": ...}`. `title`/`description`/`default` live on
 * the OUTER envelope (verified against the real `databricks`/`qlik` schemas -- see
 * `recordedConnectors.ts`), not on the inner branch, so callers keep reading those from the node
 * they were given; this only narrows which node to classify the underlying TYPE from.
 *
 * Returns `null` when the shape is not this exact one-non-null-branch pattern -- a genuine union
 * of two or more real types is not a shape this form claims to understand, so it is left to
 * classify (and therefore degrade to `"unsupported"`) rather than guessing which branch wins. */
function unwrapNullable(
  node: Record<string, unknown>,
): { inner: Record<string, unknown>; nullable: boolean } | null {
  if (!Array.isArray(node.anyOf)) {
    return { inner: node, nullable: false };
  }
  const branches = node.anyOf.filter(isJsonObject);
  if (branches.length !== node.anyOf.length) {
    // Some branch wasn't even an object schema -- not a shape recognised below.
    return null;
  }
  const nullBranches = branches.filter((branch) => branch.type === "null");
  const nonNullBranches = branches.filter((branch) => branch.type !== "null");
  if (nullBranches.length === 1 && nonNullBranches.length === 1) {
    return { inner: nonNullBranches[0]!, nullable: true };
  }
  return null;
}

function classifyFieldKind(inner: Record<string, unknown>): ConfigFieldKind {
  if (Array.isArray(inner.enum)) {
    const values = inner.enum.filter((entry): entry is string => typeof entry === "string");
    // Only a string enum renders as a Select; a numeric/mixed enum falls back honestly.
    if (values.length > 0 && values.length === inner.enum.length) {
      return { kind: "enum", values };
    }
    return { kind: "unsupported" };
  }
  switch (inner.type) {
    case "string":
      return { kind: "string" };
    case "boolean":
      return { kind: "boolean" };
    case "integer":
    case "number":
      return { kind: "number" };
    case "array": {
      const items = inner.items;
      if (isJsonObject(items) && items.type === "string") {
        return { kind: "string-array" };
      }
      return { kind: "unsupported" };
    }
    default:
      return { kind: "unsupported" };
  }
}

function describeConfigProperty(
  name: string,
  rawNode: unknown,
  required: boolean,
): ConfigFieldDescriptor {
  const node = isJsonObject(rawNode) ? rawNode : {};
  const title = typeof node.title === "string" && node.title.length > 0 ? node.title : humanize(name);
  const description = typeof node.description === "string" ? node.description : undefined;
  const hasDefault = Object.prototype.hasOwnProperty.call(node, "default");
  const defaultValue = node.default;

  const unwrapped = unwrapNullable(node);
  if (unwrapped === null) {
    return {
      name,
      title,
      description,
      required,
      nullable: false,
      hasDefault,
      default: defaultValue,
      field: { kind: "unsupported" },
    };
  }
  return {
    name,
    title,
    description,
    required,
    nullable: unwrapped.nullable,
    hasDefault,
    default: defaultValue,
    field: classifyFieldKind(unwrapped.inner),
  };
}

/** Every property `config_schema.properties` declares, in schema (== pydantic field) order,
 * each described well enough to render. Never throws and never drops a property -- an
 * unparseable `schema` (not an object, no `properties`) simply yields `[]`, which
 * `SchemaSettingsForm.tsx` treats the same as "this connector declares no settings", not as an
 * error. */
export function describeConfigSchema(schema: unknown): ConfigFieldDescriptor[] {
  if (!isJsonObject(schema) || !isJsonObject(schema.properties)) {
    return [];
  }
  const required = new Set(
    Array.isArray(schema.required)
      ? schema.required.filter((entry): entry is string => typeof entry === "string")
      : [],
  );
  return Object.entries(schema.properties).map(([name, node]) =>
    describeConfigProperty(name, node, required.has(name)),
  );
}

/** The value this field would submit right now: whatever `settings` already holds for it, or
 * the schema's own declared default when the operator has not touched it yet. Used both to
 * RENDER a field's current value and, via `isPresent` below, to validate it -- the two must
 * agree, or the required check could pass against a value the field does not actually show. */
export function effectiveValue(descriptor: ConfigFieldDescriptor, settings: Record<string, unknown>): unknown {
  if (Object.prototype.hasOwnProperty.call(settings, descriptor.name)) {
    return settings[descriptor.name];
  }
  return descriptor.hasDefault ? descriptor.default : undefined;
}

function isPresent(descriptor: ConfigFieldDescriptor, value: unknown): boolean {
  switch (descriptor.field.kind) {
    case "boolean":
      return typeof value === "boolean";
    case "number":
      return typeof value === "number";
    case "string-array":
      return Array.isArray(value) && value.length > 0;
    case "string":
    case "enum":
    case "unsupported":
      return typeof value === "string" && value.trim().length > 0;
  }
}

/** Required property names whose effective value (see `effectiveValue`) is missing --
 * `EndpointFormSheet.tsx` disables Submit while this is non-empty, the same way it already
 * gates on `name`/`connector`/`role`. Mirrors what the server itself enforces (verified against
 * the real service: a missing required property comes back `endpoint_settings_invalid` with
 * `field: null` -- a banner-only error the operator cannot act on precisely -- so getting this
 * client-side check right is the only precise "which field" signal available before submit). */
export function missingRequiredFields(
  descriptors: ConfigFieldDescriptor[],
  settings: Record<string, unknown>,
): string[] {
  return descriptors
    .filter((descriptor) => descriptor.required)
    .filter((descriptor) => !isPresent(descriptor, effectiveValue(descriptor, settings)))
    .map((descriptor) => descriptor.name);
}

/** Keys present in a stored/loaded `settings` object that neither the current schema describes
 * nor `config_secret_fields` names. This is the "connector was upgraded, a field was removed"
 * case the task brief calls out: `EndpointFormSheet.tsx` renders these via the plain
 * `SettingsEditor` ("Additional settings") instead of dropping them, so opening and saving an
 * endpoint whose stored settings drifted from its connector's current schema is a no-op, not
 * silent data loss.
 *
 * `secretFields` is deliberately also excluded here, not just from the schema's own
 * `properties` -- see `EndpointFormSheet.tsx`'s `seedSettingsState` doc comment for why a
 * secret-named key already sitting in stored settings must not become an editable row either. */
export function unknownSettingNames(
  descriptors: ConfigFieldDescriptor[],
  secretFields: readonly string[],
  settings: Record<string, unknown>,
): string[] {
  const known = new Set<string>([...descriptors.map((descriptor) => descriptor.name), ...secretFields]);
  return Object.keys(settings).filter((key) => !known.has(key));
}
