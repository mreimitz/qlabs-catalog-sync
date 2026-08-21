// The pair's manual-edit-on-Qlik policy (`ManualEditPolicy`): a default mode plus optional
// per-entity-type overrides, per decision.md guardrail 2 ("source-wins overwrite,
// configurable to preserve local edits") and `app-spec.md`'s own description of this screen
// ("manual edit policy ... overridable per entity type or per field").
//
// This editor deliberately builds ONLY the `default` + `per_entity` levels of
// `ManualEditPolicy`, not `per_field` -- a scope call, not an oversight. `per_field` keys are
// `"<entity_type>.<field>"` pairs (`configstore/models.py`'s `ManualEditPolicy.mode_for`), and
// this API exposes no schema anywhere that lists a connector's actual field names (the same
// gap `../endpoints/SettingsEditor.tsx`'s own doc comment already ran into for endpoint
// settings) -- there is nothing to populate a per-field picker with beyond a free-text
// "field.entity" key the operator would have to already know by heart. `per_entity` alone
// still lets an operator flip the common case (preserve local edits for one whole entity type)
// entirely from the browser, which is this task's DoD; a per-field editor is real follow-up
// scope for whichever task first has a field name to offer, not invented here against no data.
import { FieldRow, Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@elabs-ai/components-ui";

import type { EntityType, ManualEditMode } from "./pairsApi";
import { ENTITY_TYPE_LABEL, MANUAL_MODE_LABEL } from "./labels";

const MANUAL_MODE_VALUES: readonly ManualEditMode[] = ["source_wins", "preserve_local"];

/** Sentinel Select value for "no override -- use the pair's default". Radix `Select` rejects
 * an empty-string item value, so `undefined`/"unset" needs a real, non-empty stand-in. */
const USE_DEFAULT = "__use_default__";

export function ManualEditPolicyEditor({
  entityTypes,
  defaultMode,
  perEntity,
  onDefaultModeChange,
  onPerEntityChange,
  disabled = false,
}: {
  /** The pair's currently-selected entity types -- only these get an override row, since an
   * override for an entity type the pair does not sync would be dead configuration. */
  entityTypes: EntityType[];
  defaultMode: ManualEditMode;
  perEntity: Record<string, ManualEditMode>;
  onDefaultModeChange: (mode: ManualEditMode) => void;
  onPerEntityChange: (next: Record<string, ManualEditMode>) => void;
  disabled?: boolean;
}) {
  function setOverride(entityType: EntityType, value: string) {
    if (value === USE_DEFAULT) {
      const next = { ...perEntity };
      delete next[entityType];
      onPerEntityChange(next);
      return;
    }
    onPerEntityChange({ ...perEntity, [entityType]: value as ManualEditMode });
  }

  return (
    <div className="flex flex-col gap-4">
      <Select value={defaultMode} onValueChange={(value) => onDefaultModeChange(value as ManualEditMode)}>
        <FieldRow
          label="Manual-edit policy (default)"
          description="What the engine does when a value it wrote to Qlik was edited there by hand since the last sync. Source wins overwrites that edit on the next run; preserve local edits leaves it alone."
        >
          <SelectTrigger disabled={disabled}>
            <SelectValue />
          </SelectTrigger>
        </FieldRow>
        <SelectContent>
          {MANUAL_MODE_VALUES.map((value) => (
            <SelectItem key={value} value={value}>
              {MANUAL_MODE_LABEL[value]}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {entityTypes.length > 0 ? (
        <div className="flex flex-col gap-3 rounded-md border border-border p-3">
          <p className="text-caption font-medium">Overrides per entity type</p>
          {entityTypes.map((entityType) => {
            const currentValue = perEntity[entityType] ?? USE_DEFAULT;
            return (
              <Select
                key={entityType}
                value={currentValue}
                onValueChange={(value) => setOverride(entityType, value)}
              >
                <FieldRow label={ENTITY_TYPE_LABEL[entityType]}>
                  <SelectTrigger disabled={disabled}>
                    <SelectValue />
                  </SelectTrigger>
                </FieldRow>
                <SelectContent>
                  <SelectItem value={USE_DEFAULT}>Use default ({MANUAL_MODE_LABEL[defaultMode]})</SelectItem>
                  {MANUAL_MODE_VALUES.map((value) => (
                    <SelectItem key={value} value={value}>
                      {MANUAL_MODE_LABEL[value]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
