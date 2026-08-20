// A generic, connector-agnostic editor for `EndpointOut.settings` (`Record<string,
// JsonValue>`). Deliberately NOT built on `KeyValueEditor` (`@elabs-ai/components-ui`), even
// though that component exists and looks like an obvious fit -- two reasons, both load-bearing:
//
//   1. `KeyValueEditor`'s `KeyValueRow` carries an opt-in `secret` flag that renders a row
//      masked (`type="password"`) with a reveal toggle -- built for editing an actual secret
//      VALUE inline (an env-var editor). That is precisely the shape C2 forbids this form from
//      ever offering: an endpoint holds a secret *reference*, never a value, and nothing here
//      may look like a safe place to type one. Never wiring that flag up removes the temptation
//      entirely rather than trusting every future edit to leave it off.
//   2. The DoD requires a settings validation error (specifically `InlineSecretRejectedError`,
//      the one settings-level error the server DOES name a field for -- see
//      `errorMapping.ts`) to render against the offending ROW, not a form-wide banner.
//      `KeyValueEditorProps` (per `brand-ui docs KeyValueEditor`) has no `error`/row-level
//      error prop to hang that on. `FieldRow` does (`error?: ReactNode`, sets `aria-invalid`
//      + `role="alert"`), so each row here is one `FieldRow` wrapping one `Input` -- the same
//      primitives `KeyValueEditor` itself is built from, composed instead of reused, precisely
//      because reuse could not satisfy the DoD.
//
// There is no connector config schema exposed anywhere in this API (see the task report's
// "seams" section) -- this editor cannot know a connector's real field names, types, or which
// ones are secret-typed ahead of time. It is intentionally dumb: an ordered list of plain-text
// key/value pairs. On submit (`EndpointFormSheet.tsx`), each value is `JSON.parse`d when that
// succeeds (so `"8080"`, `"true"`, `"{\"a\":1}"` become a number/boolean/object, matching a
// connector's typed `ConfigModel` fields without this editor needing to know their types) and
// kept as a plain string otherwise.
import { Button, FieldRow, IconButton, Input } from "@elabs-ai/components-ui";
import { Plus, Trash2 } from "lucide-react";

export interface SettingsRow {
  id: string;
  key: string;
  value: string;
}

let nextRowId = 0;
export function newSettingsRow(key = "", value = ""): SettingsRow {
  nextRowId += 1;
  return { id: `settings-row-${nextRowId}`, key, value };
}

/** `Record<string, JsonValue>` -> ordered rows, for seeding the editor from an existing
 * endpoint's `settings` when opening the edit form. Values that are not already strings are
 * `JSON.stringify`d so round-tripping through the editor without any change reproduces the
 * same value on submit (see the module doc's JSON.parse-on-submit note). */
export function settingsToRows(settings: Record<string, unknown>): SettingsRow[] {
  return Object.entries(settings).map(([key, value]) =>
    newSettingsRow(key, typeof value === "string" ? value : JSON.stringify(value)),
  );
}

/** Ordered rows -> `Record<string, JsonValue>` for the request body. Blank keys are dropped
 * (an in-progress "add row" the operator has not named yet); a later row wins over an earlier
 * one with the same key, matching plain-object `{...a, ...b}` semantics. */
export function rowsToSettings(rows: SettingsRow[]): Record<string, unknown> {
  const settings: Record<string, unknown> = {};
  for (const row of rows) {
    const key = row.key.trim();
    if (!key) continue;
    try {
      settings[key] = JSON.parse(row.value);
    } catch {
      settings[key] = row.value;
    }
  }
  return settings;
}

export function SettingsEditor({
  rows,
  onRowsChange,
  fieldErrors,
  disabled = false,
}: {
  rows: SettingsRow[];
  onRowsChange: (rows: SettingsRow[]) => void;
  /** Keyed by settings KEY (not row id) -- `InlineSecretRejectedError` names the connector's
   * own field name, which is what the operator typed as this row's key. */
  fieldErrors: Record<string, string>;
  disabled?: boolean;
}) {
  function updateRow(id: string, patch: Partial<Pick<SettingsRow, "key" | "value">>) {
    onRowsChange(rows.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  }

  function removeRow(id: string) {
    onRowsChange(rows.filter((row) => row.id !== id));
  }

  return (
    <div className="flex flex-col gap-3">
      {rows.length === 0 ? (
        <p className="text-caption text-muted-foreground">No settings yet.</p>
      ) : null}
      {rows.map((row) => {
        const error = row.key.trim() ? fieldErrors[row.key.trim()] : undefined;
        return (
          <div key={row.id} className="flex items-start gap-2">
            <div className="w-2/5">
              <FieldRow label="Key" error={error}>
                <Input
                  value={row.key}
                  onChange={(event) => updateRow(row.id, { key: event.target.value })}
                  disabled={disabled}
                  placeholder="setting_name"
                />
              </FieldRow>
            </div>
            <div className="flex-1">
              <FieldRow label="Value">
                <Input
                  type="text"
                  value={row.value}
                  onChange={(event) => updateRow(row.id, { value: event.target.value })}
                  disabled={disabled}
                  placeholder="value"
                />
              </FieldRow>
            </div>
            <IconButton
              label={`Remove ${row.key || "setting"}`}
              icon={<Trash2 />}
              variant="ghost"
              disabled={disabled}
              className="mt-6"
              onClick={() => removeRow(row.id)}
            />
          </div>
        );
      })}
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={disabled}
        onClick={() => onRowsChange([...rows, newSettingsRow()])}
        className="self-start"
      >
        <Plus aria-hidden className="mr-1.5 size-4" />
        Add setting
      </Button>
    </div>
  );
}
