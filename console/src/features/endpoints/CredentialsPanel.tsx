// Enter, replace and remove an endpoint's credentials (amended C2).
//
// C2 as originally written said an endpoint holds a secret *reference* and the value lives in
// the server's environment. That means editing a file on the host and restarting the service
// for every client an operator adds, which is the opposite of what C1 promises -- so a
// credential is now endpoint configuration like any other, saved through this panel, sealed
// server-side and stored in the configuration database.
//
// Three properties this component is built around:
//
// * **Write-only.** No route returns a saved credential, so nothing here ever displays one. The
//   input is always empty on load, whatever is stored. What an operator gets instead is
//   "Saved <when>" -- honest feedback that does not require reading the value back.
// * **One field at a time.** A connector declares n secret fields (Databricks has
//   `client_secret` and `token`, one per credential route). Each saves and clears on its own,
//   because "replace only the client secret" must not require re-entering anything else.
// * **Only for an endpoint that exists.** A credential is bound to its endpoint by name -- the
//   server seals it against that name -- so there is nothing to attach one to until the
//   endpoint has been registered. Create mode says so rather than offering an input that would
//   fail on submit.
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  AlertDescription,
  Button,
  FieldRow,
  Input,
  Label,
  Spinner,
  toast,
} from "@elabs-ai/components-ui";

import {
  deleteEndpointSecret,
  listEndpointSecrets,
  putEndpointSecret,
  type StoredSecretOut,
} from "./endpointsApi";

/** How a saved-at timestamp is rendered. Absolute rather than relative ("3 minutes ago"): the
 * question this answers is "is this the credential I entered on Tuesday", and a relative label
 * that keeps changing under the operator is worse at answering it. */
function formatSavedAt(iso: string | null | undefined): string {
  if (!iso) return "saved";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "saved";
  return `saved ${parsed.toLocaleString()}`;
}

export function CredentialsPanel({
  endpointName,
  secretFields,
  onChanged,
}: {
  /** The registered endpoint's name, or `null` in create mode -- see the module doc comment. */
  endpointName: string | null;
  /** `connector.config_secret_fields`. Used as the fallback list so the panel can render every
   * field the connector wants even before the first `GET /secrets` returns. */
  secretFields: readonly string[];
  /** Called after a successful save or clear, so the surrounding screen can refresh the
   * endpoint's secret-resolve badge without the operator reloading. */
  onChanged?: () => void;
}) {
  const [statuses, setStatuses] = useState<StoredSecretOut[] | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busyField, setBusyField] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  /** The last save/remove failure, rendered inline as well as toasted. A toast in the corner is
   * the wrong place for "your deployment has no master key" or "that field is not a secret":
   * the operator's eye is on the field they just tried to fill, and the fix is right there. */
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!endpointName) return;
    const result = await listEndpointSecrets(endpointName);
    if (result.ok) {
      setStatuses(result.data);
      setLoadError(null);
    } else {
      setLoadError(result.error.message);
    }
  }, [endpointName]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (!endpointName) {
    return (
      <div className="flex flex-col gap-2 rounded-md border border-border p-3">
        <p className="text-caption font-medium">Credentials</p>
        <p className="text-caption text-muted-foreground">
          Register this endpoint first, then reopen it to enter its credentials. A credential is
          sealed against the endpoint it belongs to, so there is nothing to attach one to yet.
        </p>
      </div>
    );
  }

  // Prefer the server's list (it knows what is already saved); fall back to the connector's
  // declared fields so the panel is never empty while the first request is in flight.
  const rows: StoredSecretOut[] =
    statuses ??
    secretFields.map((field) => ({ field, is_set: false, updated_at: null, key_id: null }));

  if (rows.length === 0) {
    return (
      <div className="flex flex-col gap-2 rounded-md border border-border p-3">
        <p className="text-caption font-medium">Credentials</p>
        <p className="text-caption text-muted-foreground">
          This connector declares no secret-typed fields, so it needs no credential.
        </p>
      </div>
    );
  }

  async function save(field: string) {
    const value = (drafts[field] ?? "").trim();
    if (value.length === 0 || !endpointName) return;
    setBusyField(field);
    const result = await putEndpointSecret(endpointName, field, value);
    setBusyField(null);
    if (result.ok) {
      // Cleared immediately on success: a credential must not sit in the DOM after it has been
      // saved, and an empty input is also the honest depiction of a write-only field.
      setDrafts((current) => ({ ...current, [field]: "" }));
      setActionError(null);
      toast.success(`Saved ${field}`);
      await refresh();
      onChanged?.();
    } else {
      setActionError(result.error.message);
      toast.error(result.error.message);
    }
  }

  async function clear(field: string) {
    if (!endpointName) return;
    setBusyField(field);
    const result = await deleteEndpointSecret(endpointName, field);
    setBusyField(null);
    if (result.ok) {
      setActionError(null);
      toast.success(`Removed ${field}`);
      await refresh();
      onChanged?.();
    } else {
      setActionError(result.error.message);
      toast.error(result.error.message);
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-3">
      <Label>Credentials</Label>
      <p className="text-caption text-muted-foreground">
        Encrypted before it is stored, and never shown again -- there is no way to read a saved
        credential back, here or through the API. Re-enter it to replace it.
      </p>
      {loadError ? (
        <Alert variant="destructive">
          <AlertDescription>{loadError}</AlertDescription>
        </Alert>
      ) : null}
      {actionError ? (
        <Alert variant="destructive">
          <AlertDescription>{actionError}</AlertDescription>
        </Alert>
      ) : null}
      {rows.map((row) => {
        const busy = busyField === row.field;
        const draft = drafts[row.field] ?? "";
        return (
          <div key={row.field} className="flex flex-col gap-2">
            {/* FieldRow's label associates with its single labellable child, so the Input is
                its only child and the actions sit beside it rather than inside. */}
            <FieldRow
              label={row.field}
              description={row.is_set ? formatSavedAt(row.updated_at) : "not set"}
            >
              <Input
                type="password"
                value={draft}
                autoComplete="new-password"
                placeholder={row.is_set ? "Enter a new value to replace it" : "Enter the value"}
                disabled={busy}
                onChange={(event) =>
                  setDrafts((current) => ({ ...current, [row.field]: event.target.value }))
                }
              />
            </FieldRow>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                onClick={() => void save(row.field)}
                disabled={busy || draft.trim().length === 0}
              >
                {busy ? <Spinner aria-hidden className="mr-2 size-4" /> : null}
                {row.is_set ? "Replace" : "Save"}
              </Button>
              {row.is_set ? (
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => void clear(row.field)}
                  disabled={busy}
                >
                  Remove
                </Button>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}
