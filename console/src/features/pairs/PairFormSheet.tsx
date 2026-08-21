// Create / edit a sync pair (T13.4). One form for both, mirroring
// `../endpoints/EndpointFormSheet.tsx`'s own shape: creating differs from editing only in
// which endpoint API call submit calls -- every field here (including `name`, unlike the
// endpoint form) stays editable, because `SyncPairUpdateRequest` allows changing all of them
// (`pairs.py`: touching `source`/`target` just re-runs the same guardrails a create does).
import { useEffect, useId, useState, type FormEvent } from "react";
import {
  Alert,
  AlertDescription,
  Button,
  Checkbox,
  FieldRow,
  Input,
  Label,
  NumberInput,
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  Spinner,
  Switch,
  toast,
} from "@elabs-ai/components-ui";

import {
  createPair,
  runEndpointHealthcheck,
  updatePair,
  type EndpointHealthOut,
  type EndpointOut,
  type EntityType,
  type ManualEditMode,
  type SyncPairOut,
} from "./pairsApi";
import { classifyPairError, type TopLevelField } from "./errorMapping";
import { EndpointPickerField } from "./EndpointPickerField";
import { ManualEditPolicyEditor } from "./ManualEditPolicyEditor";
import { ENTITY_TYPE_LABEL, ENTITY_TYPE_VALUES } from "./labels";

export type PairFormMode = { kind: "create" } | { kind: "edit"; pair: SyncPairOut };

const DEFAULT_CADENCE_SECONDS = 900;

export function PairFormSheet({
  open,
  onOpenChange,
  mode,
  endpoints,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: PairFormMode;
  endpoints: EndpointOut[];
  onSaved: (pair: SyncPairOut) => void;
}) {
  const headingId = useId();
  const isEdit = mode.kind === "edit";
  const existing = mode.kind === "edit" ? mode.pair : null;

  const [name, setName] = useState("");
  const [source, setSource] = useState<string | undefined>(undefined);
  const [target, setTarget] = useState<string | undefined>(undefined);
  const [targetSpace, setTargetSpace] = useState("");
  const [entityTypes, setEntityTypes] = useState<EntityType[]>([]);
  const [cadenceSeconds, setCadenceSeconds] = useState<number | null>(DEFAULT_CADENCE_SECONDS);
  const [jitterSeconds, setJitterSeconds] = useState<number | null>(null);
  const [manualDefault, setManualDefault] = useState<ManualEditMode>("source_wins");
  const [manualPerEntity, setManualPerEntity] = useState<Record<string, ManualEditMode>>({});
  // Not editable by this form (see `ManualEditPolicyEditor.tsx`'s doc comment for why), but
  // captured on open and re-sent verbatim on submit -- `manual_edit_policy` is REPLACED as a
  // whole object by `SyncPairUpdateRequest`, not deep-merged, so silently omitting a
  // `per_field` override this form does not know how to render would erase it on the next
  // save through this screen. This is preservation, not editing: a future task that owns a
  // per_field editor should read/write this state directly instead of adding a second copy.
  const [manualPerField, setManualPerField] = useState<Record<string, ManualEditMode>>({});
  const [activationOptIn, setActivationOptIn] = useState(false);
  const [enabled, setEnabled] = useState(false);

  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<TopLevelField, string>>>({});

  const [health, setHealth] = useState<Record<string, EndpointHealthOut>>({});
  const [healthChecking, setHealthChecking] = useState<Record<string, boolean>>({});

  // Re-seed every time the sheet opens (both create and edit) -- see
  // `EndpointFormSheet.tsx`'s identical comment for why `open` plus the record's own id, not
  // `existing`'s identity, is what should re-run this.
  useEffect(() => {
    if (!open) return;
    setSubmitting(false);
    setFormError(null);
    setFieldErrors({});
    setHealth({});
    setHealthChecking({});
    if (existing) {
      setName(existing.name);
      setSource(existing.source);
      setTarget(existing.target);
      setTargetSpace(existing.target_space);
      setEntityTypes(existing.entity_types);
      setCadenceSeconds(existing.cadence_seconds);
      setJitterSeconds(existing.jitter_seconds);
      setManualDefault(existing.manual_edit_policy.default);
      setManualPerEntity(existing.manual_edit_policy.per_entity ?? {});
      setManualPerField(existing.manual_edit_policy.per_field ?? {});
      setActivationOptIn(existing.activation_opt_in);
      setEnabled(existing.enabled);
    } else {
      setName("");
      setSource(undefined);
      setTarget(undefined);
      setTargetSpace("");
      setEntityTypes([]);
      setCadenceSeconds(DEFAULT_CADENCE_SECONDS);
      setJitterSeconds(null);
      setManualDefault("source_wins");
      setManualPerEntity({});
      setManualPerField({});
      // Both opt-ins start OFF -- matching the API's own defaults exactly (`SyncPairCreateRequest`
      // default `false` for both) and mutation check #1 ("default activation to on").
      setActivationOptIn(false);
      setEnabled(false);
    }
  }, [open, existing?.id]);

  function toggleEntityType(type: EntityType, checked: boolean) {
    setEntityTypes((prev) => (checked ? [...prev, type] : prev.filter((entry) => entry !== type)));
    if (!checked) {
      setManualPerEntity((prev) => {
        if (!(type in prev)) return prev;
        const next = { ...prev };
        delete next[type];
        return next;
      });
    }
  }

  async function handleCheckHealth(endpointName: string) {
    setHealthChecking((prev) => ({ ...prev, [endpointName]: true }));
    const result = await runEndpointHealthcheck(endpointName);
    setHealthChecking((prev) => ({ ...prev, [endpointName]: false }));
    if (result.ok) {
      // A red result is a normal, successfully-rendered fact about the endpoint -- never a
      // toast (same discipline as `../endpoints/EndpointsScreen.tsx`'s `handleHealthcheck`).
      setHealth((prev) => ({ ...prev, [endpointName]: result.data }));
    } else {
      toast.error(`Could not run healthcheck for "${endpointName}": ${result.error.message}`);
    }
  }

  const canSubmit =
    name.trim().length > 0 &&
    !!source &&
    !!target &&
    targetSpace.trim().length > 0 &&
    cadenceSeconds != null &&
    cadenceSeconds > 0 &&
    !submitting;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!source || !target || cadenceSeconds == null) return;
    setSubmitting(true);
    setFormError(null);
    setFieldErrors({});

    const payload = {
      name: name.trim(),
      source,
      target,
      target_space: targetSpace.trim(),
      entity_types: entityTypes,
      cadence_seconds: cadenceSeconds,
      // Explicit `null` when blank -- NumberInput reports an empty field as `null`, which is
      // exactly `SyncPairUpdateRequest`'s own "clear the per-pair override" convention
      // (`pairsApi.ts`'s doc comment).
      jitter_seconds: jitterSeconds,
      manual_edit_policy: {
        default: manualDefault,
        ...(Object.keys(manualPerEntity).length > 0 ? { per_entity: manualPerEntity } : {}),
        // Preserved, not edited here -- see the `manualPerField` state declaration above.
        ...(Object.keys(manualPerField).length > 0 ? { per_field: manualPerField } : {}),
      },
      activation_opt_in: activationOptIn,
      enabled,
    };

    const result = existing ? await updatePair(existing.id, payload) : await createPair(payload);
    setSubmitting(false);

    if (result.ok) {
      toast.success(isEdit ? `Sync pair "${result.data.name}" updated.` : `Sync pair "${result.data.name}" created.`);
      onSaved(result.data);
      onOpenChange(false);
      return;
    }

    const classified = classifyPairError(result.error);
    if (classified.kind === "field") {
      setFieldErrors({ [classified.field]: classified.message });
    } else {
      setFormError(classified.message);
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle id={headingId}>{isEdit ? `Edit sync pair "${existing?.name}"` : "New sync pair"}</SheetTitle>
          <SheetDescription>
            {isEdit
              ? "Change what this pair syncs, how often, and whether it runs or is activated in Qlik."
              : "Name a source and a target endpoint, the Qlik space new data products belong to, and what to sync."}
          </SheetDescription>
        </SheetHeader>
        <form onSubmit={(event) => void handleSubmit(event)} aria-labelledby={headingId} noValidate className="flex flex-col gap-4 px-4 pb-4">
          {formError ? (
            <Alert variant="destructive">
              <AlertDescription>{formError}</AlertDescription>
            </Alert>
          ) : null}

          <FieldRow label="Name" error={fieldErrors.name}>
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="prod_databricks_to_qlik"
              required
            />
          </FieldRow>

          <EndpointPickerField
            label="Source endpoint"
            role="source"
            endpoints={endpoints}
            value={source}
            onValueChange={setSource}
            error={fieldErrors.source}
            health={source ? health[source] : undefined}
            checkingHealth={!!(source && healthChecking[source])}
            onCheckHealth={handleCheckHealth}
            disabled={submitting}
          />

          <EndpointPickerField
            label="Target endpoint"
            role="target"
            endpoints={endpoints}
            value={target}
            onValueChange={setTarget}
            error={fieldErrors.target}
            health={target ? health[target] : undefined}
            checkingHealth={!!(target && healthChecking[target])}
            onCheckHealth={handleCheckHealth}
            disabled={submitting}
          />

          <FieldRow
            label="Target Qlik space"
            description='The Qlik space new or updated data products belong to (e.g. "analytics/finance"). The connector never creates a space -- it must already exist in Qlik.'
            error={fieldErrors.target_space}
          >
            <Input
              value={targetSpace}
              onChange={(event) => setTargetSpace(event.target.value)}
              placeholder="analytics/finance"
              required
            />
          </FieldRow>

          <div className="flex flex-col gap-2">
            <Label>Entity types to sync</Label>
            {fieldErrors.entity_types ? (
              <p role="alert" className="text-body font-medium text-destructive-text">
                {fieldErrors.entity_types}
              </p>
            ) : null}
            <div className="flex flex-col gap-2">
              {ENTITY_TYPE_VALUES.map((type) => {
                const checkboxId = `pair-entity-type-${type}`;
                return (
                  <div key={type} className="flex items-center gap-2">
                    <Checkbox
                      id={checkboxId}
                      checked={entityTypes.includes(type)}
                      onCheckedChange={(checked) => toggleEntityType(type, checked === true)}
                      disabled={submitting}
                    />
                    <Label htmlFor={checkboxId}>{ENTITY_TYPE_LABEL[type]}</Label>
                  </div>
                );
              })}
            </div>
          </div>

          <FieldRow label="Cadence" description="How often the scheduler runs this pair, in seconds." error={fieldErrors.cadence_seconds}>
            <NumberInput value={cadenceSeconds} onValueChange={setCadenceSeconds} min={1} disabled={submitting} />
          </FieldRow>

          <FieldRow
            label="Jitter (optional)"
            description="A random spread added to the cadence, in seconds, so multiple pairs do not all fire at once. Leave blank to use the scheduler's computed default."
            error={fieldErrors.jitter_seconds}
          >
            <NumberInput value={jitterSeconds} onValueChange={setJitterSeconds} min={0} disabled={submitting} />
          </FieldRow>

          <ManualEditPolicyEditor
            entityTypes={entityTypes}
            defaultMode={manualDefault}
            perEntity={manualPerEntity}
            onDefaultModeChange={setManualDefault}
            onPerEntityChange={setManualPerEntity}
            disabled={submitting}
          />

          <FieldRow
            label="Activate in Qlik"
            description={
              'Off by default. Turning this on makes the resulting Qlik data product ' +
              'discoverable tenant-wide -- anyone with access to this Qlik tenant can find it, ' +
              'not only people who already know it exists (RM-01 decision D7). Leaving this off ' +
              'still lets the pair sync; the data product just stays unpublished.'
            }
            error={fieldErrors.activation_opt_in}
          >
            <Switch checked={activationOptIn} onCheckedChange={setActivationOptIn} disabled={submitting} />
          </FieldRow>

          <div className="flex items-center gap-3">
            <Switch id="pair-enabled" checked={enabled} onCheckedChange={setEnabled} disabled={submitting} />
            <Label htmlFor="pair-enabled">Enabled</Label>
          </div>
          <p className="text-body text-muted-foreground">
            Whether the scheduler runs this pair at all. Off by default; turn it on when this
            pair is ready to sync, and off again to pause it without deleting it.
          </p>

          <SheetFooter className="px-0">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!canSubmit}>
              {submitting ? <Spinner aria-hidden className="mr-2 size-4" /> : null}
              {submitting ? "Saving…" : isEdit ? "Save changes" : "Create sync pair"}
            </Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  );
}
