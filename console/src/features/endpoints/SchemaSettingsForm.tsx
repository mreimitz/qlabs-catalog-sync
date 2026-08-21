// The real typed settings form (this task): one labelled, typed control per property in a
// connector's `config_schema`, generated -- never a hardcoded per-connector field list, which
// would be wrong the day a connector adds, renames or removes a field (`connectors.py`'s
// `ConnectorInfo.config_schema` doc comment says so explicitly). `EndpointFormSheet.tsx` renders
// this INSTEAD of the old generic `SettingsEditor` whenever the selected connector has a
// `config_schema`; when it does not (`config_schema` is `null` -- an unavailable connector, or
// one whose `ConfigModel` could not produce a schema), `SettingsEditor` alone is still what
// renders, unchanged, and this component is not used at all.
//
// `configSchemaForm.ts` does the (React-free, unit-tested) parsing; this file only renders what
// it produces. Five `ConfigFieldKind`s become five typed controls:
//
//   string        -> Input
//   boolean       -> Checkbox (never Switch -- a Switch applies immediately and this value only
//                    applies on the sheet's own Submit, exactly the anti-pattern
//                    `brand-ui docs Switch` calls out; see `EndpointFormSheet.tsx`'s own
//                    immediate-apply Enabled toggle for the contrasting *correct* Switch use)
//   number         -> NumberInput
//   enum           -> Select (mirrors the existing Role/Connector fields exactly: FieldRow wraps
//                     only SelectTrigger, value/onValueChange live on the Select root)
//   string-array   -> TagInput
//
// A property whose shape is none of the above -- `kind: "unsupported"` -- still renders: a plain
// text Input using the exact `stringifySettingValue`/`parseSettingValue` convention
// `SettingsEditor.tsx` already uses, so it round-trips unedited exactly like a generic row does.
// **A property is never omitted from this form for having a shape it does not recognise** --
// dropping one silently is exactly the data-loss bug the task brief calls out as unacceptable.
//
// State ownership: `values` is the FULL settings record for every schema-described property this
// form has ever touched or that pre-existed (`EndpointFormSheet.tsx`'s `seedSettingsState`) --
// there is no separate "touched" flag. A property absent from `values` shows its schema default
// (or blank) but is NOT in the record submitted on save; the moment an `onChange` fires (or the
// property pre-existed in a loaded endpoint's stored settings), its key exists in `values` and
// stays there. This is what keeps a schema default the operator never chose out of the request
// body (the task brief's own requirement) without a second piece of state to track "touched"
// separately from "has a value" -- for every real property on `databricks`/`qlik`, sending the
// declared default explicitly and omitting the property behave identically (see the task
// report's mutation-check #7 discussion), so this simpler model costs nothing today and is the
// more defensible default for a hypothetical future schema where it would not.
import {
  Checkbox,
  FieldRow,
  Input,
  NumberInput,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  TagInput,
} from "@elabs-ai/components-ui";

import { effectiveValue, type ConfigFieldDescriptor } from "./configSchemaForm";
import { parseSettingValue, stringifySettingValue } from "./SettingsEditor";

export function SchemaSettingsForm({
  descriptors,
  secretFields,
  values,
  onValuesChange,
  fieldErrors,
  disabled = false,
}: {
  /** From `describeConfigSchema(connector.config_schema)` -- computed once by the caller
   * (`EndpointFormSheet.tsx`) so it can also feed `missingRequiredFields` for the Submit gate
   * without parsing the schema twice. */
  descriptors: ConfigFieldDescriptor[];
  /** `connector.config_secret_fields` -- rendered as an explanation, never a control. See the
   * module doc comment and `configSchemaForm.ts`'s `unknownSettingNames` doc comment for why a
   * secret-named key is excluded here even when one is already sitting in loaded settings. */
  secretFields: readonly string[];
  /** The full settings record -- see the module doc comment's "State ownership" note. */
  values: Record<string, unknown>;
  onValuesChange: (next: Record<string, unknown>) => void;
  /** Keyed by settings KEY, exactly like `SettingsEditor`'s own `fieldErrors` -- the same
   * `InlineSecretRejectedError` channel that can name one of the "Additional settings" rows
   * (`EndpointFormSheet.tsx`) can, in principle, name a generated field's own key too, and this
   * is what lets `FieldRow`'s `error` prop attach it there instead of only a form banner. */
  fieldErrors: Record<string, string>;
  disabled?: boolean;
}) {
  function setValue(name: string, value: unknown) {
    onValuesChange({ ...values, [name]: value });
  }

  /** Removes `name` from `values` entirely -- "the operator cleared this back to blank/default"
   * becomes "omit it", not "send an explicit empty/default value" (the task brief's own
   * requirement; see the module doc comment). Applied uniformly regardless of whether the
   * property is required: a required property left this way simply fails
   * `missingRequiredFields` and blocks Submit, exactly like a required property that was never
   * touched at all -- one rule, not two. */
  function clearValue(name: string) {
    if (!(name in values)) return;
    const next = { ...values };
    delete next[name];
    onValuesChange(next);
  }

  return (
    <div className="flex flex-col gap-4">
      {descriptors.length === 0 ? (
        <p className="text-caption text-muted-foreground">This connector declares no configurable settings.</p>
      ) : (
        descriptors.map((descriptor) => (
          <SchemaSettingsField
            key={descriptor.name}
            descriptor={descriptor}
            value={effectiveValue(descriptor, values)}
            error={fieldErrors[descriptor.name]}
            disabled={disabled}
            onChange={(value) => setValue(descriptor.name, value)}
            onClear={() => clearValue(descriptor.name)}
          />
        ))
      )}

      {secretFields.length > 0 ? (
        <div className="flex flex-col gap-2 rounded-md border border-border p-3">
          <p className="text-caption font-medium">Secret-typed fields</p>
          <p className="text-caption text-muted-foreground">
            This connector also declares the field{secretFields.length === 1 ? "" : "s"} below as
            secret-typed. C2: an endpoint holds a secret <em>reference</em>, never a value -- bind
            one via "Secret reference" above. Never entered or stored here.
          </p>
          <ul className="flex flex-col gap-1">
            {secretFields.map((name) => (
              <li key={name} className="text-caption text-muted-foreground">
                <code>{name}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function SchemaSettingsField({
  descriptor,
  value,
  error,
  disabled,
  onChange,
  onClear,
}: {
  descriptor: ConfigFieldDescriptor;
  value: unknown;
  error: string | undefined;
  disabled: boolean;
  onChange: (value: unknown) => void;
  onClear: () => void;
}) {
  const { field } = descriptor;

  switch (field.kind) {
    case "string": {
      const display = typeof value === "string" ? value : value == null ? "" : String(value);
      return (
        <FieldRow label={descriptor.title} description={descriptor.description} error={error}>
          <Input
            value={display}
            onChange={(event) => {
              const next = event.target.value;
              if (next.trim().length === 0) onClear();
              else onChange(next);
            }}
            disabled={disabled}
            required={descriptor.required}
            placeholder={descriptor.hasDefault && typeof descriptor.default === "string" ? descriptor.default : undefined}
          />
        </FieldRow>
      );
    }

    case "boolean": {
      const checked = value === true;
      return (
        <FieldRow label={descriptor.title} description={descriptor.description} error={error}>
          <Checkbox checked={checked} onCheckedChange={(next) => onChange(next === true)} disabled={disabled} />
        </FieldRow>
      );
    }

    case "number": {
      const numeric = typeof value === "number" ? value : null;
      return (
        <FieldRow label={descriptor.title} description={descriptor.description} error={error}>
          <NumberInput
            value={numeric}
            onValueChange={(next) => (next === null ? onClear() : onChange(next))}
            disabled={disabled}
          />
        </FieldRow>
      );
    }

    case "enum": {
      const selected = typeof value === "string" ? value : undefined;
      return (
        <Select value={selected} onValueChange={(next) => onChange(next)} disabled={disabled}>
          <FieldRow label={descriptor.title} description={descriptor.description} error={error}>
            <SelectTrigger>
              <SelectValue placeholder="Select a value" />
            </SelectTrigger>
          </FieldRow>
          <SelectContent>
            {field.values.map((option) => (
              <SelectItem key={option} value={option}>
                {option}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    }

    case "string-array": {
      const list = Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
      return (
        <FieldRow label={descriptor.title} description={descriptor.description} error={error}>
          <TagInput
            value={list}
            onValueChange={(next) => (next.length === 0 ? onClear() : onChange(next))}
            disabled={disabled}
            placeholder="Type a value, press Enter"
          />
        </FieldRow>
      );
    }

    case "unsupported": {
      const display = value === undefined ? "" : stringifySettingValue(value);
      const fallbackNote = "Shown as raw text -- this field's shape is not one of this form's typed controls.";
      const description = descriptor.description ? `${descriptor.description} ${fallbackNote}` : fallbackNote;
      return (
        <FieldRow label={descriptor.title} description={description} error={error}>
          <Input
            value={display}
            onChange={(event) => {
              const next = event.target.value;
              if (next.trim().length === 0) onClear();
              else onChange(parseSettingValue(next));
            }}
            disabled={disabled}
            required={descriptor.required}
            placeholder="value"
          />
        </FieldRow>
      );
    }
  }
}
