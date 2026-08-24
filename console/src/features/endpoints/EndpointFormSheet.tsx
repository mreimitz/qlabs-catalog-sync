// Register (C6) / edit an endpoint. One form for both: creating differs from editing only in
// which fields are locked (`name` and `connector` cannot change once an endpoint exists --
// `name` is the row's key, and `connector`'s renders lock in place too, `EndpointUpdateRequest`
// technically allows a `connector` swap but this form does not offer it -- crossing an
// endpoint over to a different connector's settings shape is not "editing", it is registering a
// new endpoint under an old name, and this screen does not need to make that easy) and which
// endpoint API call submit calls.
import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import {
  Alert,
  AlertDescription,
  Button,
  FieldRow,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
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
  createEndpoint,
  putEndpointSecret,
  runEndpointHealthcheck,
  updateEndpoint,
  type ConnectorInfo,
  type EndpointOut,
} from "./endpointsApi";
import { describeConfigSchema, missingRequiredFields } from "./configSchemaForm";
import { classifyEndpointError, type TopLevelField } from "./errorMapping";
import { CredentialsPanel } from "./CredentialsPanel";
import { ManifestPanel } from "./ManifestPanel";
import { SchemaSettingsForm } from "./SchemaSettingsForm";
import { newSettingsRow, rowsToSettings, settingsToRows, SettingsEditor, type SettingsRow } from "./SettingsEditor";

const ROLE_VALUES = ["source", "target"] as const;
type Role = (typeof ROLE_VALUES)[number];

export type EndpointFormMode = { kind: "create" } | { kind: "edit"; endpoint: EndpointOut };

export function EndpointFormSheet({
  open,
  onOpenChange,
  mode,
  connectors,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: EndpointFormMode;
  connectors: ConnectorInfo[];
  onSaved: (endpoint: EndpointOut) => void;
}) {
  const headingId = useId();
  const isEdit = mode.kind === "edit";
  const existing = mode.kind === "edit" ? mode.endpoint : null;

  const [name, setName] = useState("");
  const [connector, setConnector] = useState<string | undefined>(undefined);
  const [role, setRole] = useState<Role | undefined>(undefined);
  const [enabled, setEnabled] = useState(false);
  // `settingsRows` is the WHOLE editor when the selected connector has no `config_schema`
  // (`SettingsEditor`, unchanged from T13.3). When it does, `schemaFieldValues` holds every
  // schema-described property this form has touched or pre-loaded (`SchemaSettingsForm.tsx`'s
  // own doc comment explains why that record IS the "touched" tracking), and
  // `extraSettingsRows` holds the leftover stored keys that connector's CURRENT schema does not
  // describe -- rendered via the same `SettingsEditor` UI, under "Additional settings", so they
  // are shown and kept rather than silently dropped on save (`configSchemaForm.ts`'s
  // `unknownSettingNames`). All three are (re)populated together by `seedSettingsState` below;
  // exactly one of ("settingsRows") or ("schemaFieldValues" + "extraSettingsRows") is actually
  // rendered/submitted at a time, chosen by whether `configSchema` (below) is non-null.
  const [settingsRows, setSettingsRows] = useState<SettingsRow[]>([]);
  const [schemaFieldValues, setSchemaFieldValues] = useState<Record<string, unknown>>({});
  const [extraSettingsRows, setExtraSettingsRows] = useState<SettingsRow[]>([]);
  /** Credentials typed while registering. An endpoint has to exist before one can be sealed
   * against it, so they are held here and written immediately after the create succeeds --
   * two requests behind the one button the operator pressed. */
  const [pendingSecrets, setPendingSecrets] = useState<Record<string, string>>({});
  /** The last "Test connection" result, rendered beside the button. Held here rather than
   * toasted: it is the answer to a question the operator just asked, and it belongs next to
   * the form they are still filling in. */
  const [testResult, setTestResult] = useState<{ healthy: boolean; detail: string } | null>(null);
  const [testing, setTesting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<TopLevelField, string>>>({});
  const [settingsBannerError, setSettingsBannerError] = useState<string | null>(null);
  const [settingsFieldErrors, setSettingsFieldErrors] = useState<Record<string, string>>({});

  /** Populates all three settings representations above from one connector + one stored
   * `settings` record -- the one place that decides, per key, whether it becomes a typed field's
   * value, an "Additional settings" row, or (a stored key that happens to match a secret-typed
   * field name) neither. That last case is a deliberate exception to "never drop a stored
   * setting": re-submitting it would only ever be rejected by the server
   * (`InlineSecretRejectedError`, C2), and rendering it as an editable row would be exactly the
   * "offer an input for a secret field" shape this form must never offer -- so it is excluded
   * from every representation, not silently kept in a fourth, never-rendered place. */
  function seedSettingsState(connectorName: string | undefined, settings: Record<string, unknown>) {
    const info = connectors.find((entry) => entry.name === connectorName);
    const schema = info?.config_schema ?? null;
    if (schema) {
      const schemaDescriptors = describeConfigSchema(schema);
      const knownNames = new Set(schemaDescriptors.map((descriptor) => descriptor.name));
      const secretNames = new Set(info?.config_secret_fields ?? []);
      const typedValues: Record<string, unknown> = {};
      const extras: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(settings)) {
        if (knownNames.has(key)) typedValues[key] = value;
        else if (!secretNames.has(key)) extras[key] = value;
      }
      setSchemaFieldValues(typedValues);
      setExtraSettingsRows(settingsToRows(extras));
      setSettingsRows([newSettingsRow()]);
    } else {
      setSchemaFieldValues({});
      setExtraSettingsRows([]);
      setSettingsRows(Object.keys(settings).length > 0 ? settingsToRows(settings) : [newSettingsRow()]);
    }
  }

  // Re-seed every time the sheet opens (both create and edit) -- so a leftover draft from a
  // previous open (or a previous endpoint) never survives into the next one.
  useEffect(() => {
    if (!open) return;
    setSubmitting(false);
    setFormError(null);
    setFieldErrors({});
    setSettingsBannerError(null);
    setSettingsFieldErrors({});
    setPendingSecrets({});
    setTestResult(null);
    if (existing) {
      setName(existing.name);
      setConnector(existing.connector);
      setRole(existing.role);
      setEnabled(existing.enabled);
      seedSettingsState(existing.connector, existing.settings);
    } else {
      setName("");
      setConnector(undefined);
      setRole(undefined);
      setEnabled(false);
      seedSettingsState(undefined, {});
    }
    // `existing` is derived fresh from `mode` on every render; keying off its identity directly
    // would re-run on every keystroke elsewhere in the tree. `open` plus the endpoint's own
    // `name` is what actually changes between "open the sheet for a different row" events.
    // `connectors` only changes identity when `EndpointsScreen` re-fetches (its own `fetchAll`),
    // never from a keystroke in this sheet, so it is safe to list here without re-running on
    // every render.
  }, [open, existing?.name, connectors]);

  /** The Connector field's `onValueChange` (create mode only -- it is locked, below, once an
   * endpoint exists). Switching connectors mid-registration starts settings over rather than
   * carrying over values keyed by the PREVIOUS connector's field names: a value that happened to
   * share a name with the new connector's own field would silently masquerade as if the operator
   * had chosen it, and one that didn't would just become a confusing, unexplained "Additional
   * settings" row for a connector it was never entered against. */
  function handleConnectorChange(value: string) {
    setConnector(value);
    seedSettingsState(value, {});
  }

  const selectedConnectorInfo = connectors.find((entry) => entry.name === connector);
  const availableConnectors = connectors.filter((entry) => entry.available);
  const unavailableConnectors = connectors.filter((entry) => !entry.available);
  const configSchema = selectedConnectorInfo?.config_schema ?? null;
  const secretFields = selectedConnectorInfo?.config_secret_fields ?? [];
  // Computed once here (not inside `SchemaSettingsForm`) because the Submit gate below needs it
  // too -- one parse of `config_schema`, not two disagreeing ones.
  const schemaDescriptors = useMemo(() => describeConfigSchema(configSchema), [configSchema]);

  const canSubmit =
    name.trim().length > 0 &&
    !!connector &&
    !!role &&
    !submitting &&
    missingRequiredFields(schemaDescriptors, schemaFieldValues).length === 0;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!connector || !role) return;
    setSubmitting(true);
    setFormError(null);
    setFieldErrors({});
    setSettingsBannerError(null);
    setSettingsFieldErrors({});

    // Schema-driven: the typed fields' own record plus whatever "Additional settings" rows are
    // still there (never overlapping keys -- `extraSettingsRows` only ever holds keys
    // `schemaDescriptors` does not describe, by construction in `seedSettingsState`/
    // `handleConnectorChange`). Fallback: exactly today's behaviour, unchanged.
    const settings = configSchema
      ? { ...schemaFieldValues, ...rowsToSettings(extraSettingsRows) }
      : rowsToSettings(settingsRows);
    const result = existing
      ? await updateEndpoint(existing.name, {
          connector,
          role,
          settings,
          enabled,
        })
      : await createEndpoint({
          name: name.trim(),
          connector,
          role,
          settings,
          enabled,
        });

    setSubmitting(false);

    if (result.ok) {
      // The endpoint exists now, so anything typed into the credentials panel can finally be
      // sealed against it. Reported per field rather than as one all-or-nothing failure: an
      // endpoint that saved with one of its two credentials rejected is a real, recoverable
      // state, and closing the sheet claiming success would hide it.
      const rejected: string[] = [];
      for (const [field, value] of Object.entries(pendingSecrets)) {
        const saved = await putEndpointSecret(result.data.name, field, value);
        if (!saved.ok) rejected.push(`${field}: ${saved.error.message}`);
      }
      if (rejected.length > 0) {
        setFormError(
          `Endpoint "${result.data.name}" was saved, but its credentials were not: ${rejected.join("; ")}`,
        );
        onSaved(result.data);
        return;
      }
      toast.success(isEdit ? `Endpoint "${result.data.name}" updated.` : `Endpoint "${result.data.name}" registered.`);
      onSaved(result.data);
      onOpenChange(false);
      return;
    }

    const classified = classifyEndpointError(result.error);
    if (classified.kind === "field") {
      setFieldErrors({ [classified.field]: classified.message });
    } else if (classified.kind === "settings-fields") {
      const next: Record<string, string> = {};
      for (const key of classified.keys) {
        next[key] = classified.message;
      }
      setSettingsFieldErrors(next);
    } else if (classified.kind === "settings-banner") {
      setSettingsBannerError(classified.message);
    } else {
      setFormError(classified.message);
    }
  }

  /** Run the endpoint's own connector healthcheck and report it inline.
   *
   * Only offered for an endpoint that exists: the check builds a real connector from stored
   * settings and stored credentials and talks to the tenant, so there is nothing to test until
   * both have been saved. A red result is an ordinary answer here, not an error -- "these
   * credentials are wrong" is exactly what the operator pressed the button to find out. */
  async function testConnection(endpointName: string) {
    setTesting(true);
    setTestResult(null);
    const result = await runEndpointHealthcheck(endpointName);
    setTesting(false);
    if (!result.ok) {
      setTestResult({ healthy: false, detail: result.error.message });
      return;
    }
    const healthy = result.data.state === "healthy";
    setTestResult({
      healthy,
      detail: healthy
        ? "Connected. This endpoint's settings and credentials work against the tenant."
        : (result.data.reason ?? "The connector reported this endpoint as unhealthy."),
    });
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle id={headingId}>{isEdit ? `Edit endpoint "${existing?.name}"` : "Register endpoint"}</SheetTitle>
          <SheetDescription>
            {isEdit
              ? "Change this endpoint's settings, secret reference, role, or enabled state."
              : "Name an instance of a connector this image already discovered (C6) -- nothing is installed here."}
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
              disabled={isEdit}
              placeholder="qlik_prod"
              required
            />
          </FieldRow>

          <Select value={connector} onValueChange={handleConnectorChange} disabled={isEdit}>
            <FieldRow label="Connector" error={fieldErrors.connector}>
              <SelectTrigger>
                <SelectValue placeholder="Select a discovered connector" />
              </SelectTrigger>
            </FieldRow>
            <SelectContent>
              {availableConnectors.map((entry) => (
                <SelectItem key={entry.name} value={entry.name}>
                  {entry.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {unavailableConnectors.length > 0 ? (
            <Alert variant="warning">
              <AlertDescription>
                {unavailableConnectors.length} discovered connector
                {unavailableConnectors.length === 1 ? " is" : "s are"} unavailable and cannot be
                registered against:{" "}
                {unavailableConnectors
                  .map((entry) => `${entry.name} (${entry.distribution ?? "unknown distribution"}: ${entry.broken_reason ?? "no reason given"})`)
                  .join("; ")}
              </AlertDescription>
            </Alert>
          ) : null}

          {selectedConnectorInfo ? (
            selectedConnectorInfo.manifest ? (
              <details className="rounded-md border border-border p-3">
                <summary className="cursor-pointer text-caption font-medium">
                  What "{selectedConnectorInfo.name}" supports
                </summary>
                <div className="mt-3">
                  <ManifestPanel manifest={selectedConnectorInfo.manifest} />
                </div>
              </details>
            ) : (
              <Alert variant="info">
                <AlertDescription>{selectedConnectorInfo.manifest_unavailable_reason}</AlertDescription>
              </Alert>
            )
          ) : null}

          <Select value={role} onValueChange={(value) => setRole(value as Role)}>
            <FieldRow label="Role" error={fieldErrors.role}>
              <SelectTrigger>
                <SelectValue placeholder="Select source or target" />
              </SelectTrigger>
            </FieldRow>
            <SelectContent>
              {ROLE_VALUES.map((value) => (
                <SelectItem key={value} value={value}>
                  {value}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <CredentialsPanel
            endpointName={mode.kind === "edit" ? mode.endpoint.name : null}
            secretFields={secretFields}
            onPendingChange={setPendingSecrets}
          />

          <div className="flex flex-col gap-2">
            <Label>Settings</Label>
            {configSchema ? (
              <>
                <p className="text-body text-muted-foreground">
                  Generated from "{selectedConnectorInfo?.name}"'s own configuration schema.
                  Required fields are marked; a secret-typed field is never entered here -- see
                  "Secret-typed fields" below and bind a secret reference above instead.
                </p>
                {settingsBannerError ? (
                  <Alert variant="destructive">
                    <AlertDescription>{settingsBannerError}</AlertDescription>
                  </Alert>
                ) : null}
                <SchemaSettingsForm
                  descriptors={schemaDescriptors}
                  secretFields={secretFields}
                  values={schemaFieldValues}
                  onValuesChange={setSchemaFieldValues}
                  fieldErrors={settingsFieldErrors}
                  disabled={submitting}
                />
                <div className="flex flex-col gap-2 pt-2">
                  <Label className="text-caption text-muted-foreground">Additional settings</Label>
                  <p className="text-caption text-muted-foreground">
                    {extraSettingsRows.length > 0
                      ? `Stored on this endpoint but not described by "${selectedConnectorInfo?.name}"'s current configuration schema (e.g. left over from an earlier connector version). Kept as-is on save unless removed here.`
                      : `Anything "${selectedConnectorInfo?.name}"'s configuration schema does not already describe above. Most connectors reject an unrecognised key -- this exists for the rare setting the schema does not (yet) describe.`}
                  </p>
                  <SettingsEditor
                    rows={extraSettingsRows}
                    onRowsChange={setExtraSettingsRows}
                    fieldErrors={settingsFieldErrors}
                    disabled={submitting}
                  />
                </div>
              </>
            ) : (
              <>
                <p className="text-body text-muted-foreground">
                  Non-secret configuration for this connector instance. A value is parsed as JSON
                  when valid (e.g. <code>true</code>, <code>8080</code>) and kept as plain text
                  otherwise. A connector's own secret-typed fields are rejected here -- bind a
                  secret reference above instead.
                </p>
                {settingsBannerError ? (
                  <Alert variant="destructive">
                    <AlertDescription>{settingsBannerError}</AlertDescription>
                  </Alert>
                ) : null}
                <SettingsEditor
                  rows={settingsRows}
                  onRowsChange={setSettingsRows}
                  fieldErrors={settingsFieldErrors}
                  disabled={submitting}
                />
              </>
            )}
          </div>

          {mode.kind === "edit" ? (
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-3">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => void testConnection(mode.endpoint.name)}
                  disabled={testing}
                >
                  {testing ? <Spinner aria-hidden className="mr-2 size-4" /> : null}
                  {testing ? "Testing…" : "Test connection"}
                </Button>
                <p className="text-caption text-muted-foreground">
                  Signs in to the tenant with the saved settings and credentials.
                </p>
              </div>
              {testResult ? (
                <Alert variant={testResult.healthy ? "info" : "destructive"}>
                  <AlertDescription>{testResult.detail}</AlertDescription>
                </Alert>
              ) : null}
            </div>
          ) : null}

          <div className="flex items-center gap-3">
            <Switch id="endpoint-enabled" checked={enabled} onCheckedChange={setEnabled} />
            <Label htmlFor="endpoint-enabled">Enabled</Label>
          </div>

          <SheetFooter className="px-0">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!canSubmit}>
              {submitting ? <Spinner aria-hidden className="mr-2 size-4" /> : null}
              {submitting ? "Saving…" : isEdit ? "Save changes" : "Register endpoint"}
            </Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  );
}
