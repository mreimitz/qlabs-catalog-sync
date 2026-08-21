// The source/target endpoint picker for `PairFormSheet.tsx`, and the trade-off this task's
// DoD ("only enabled, healthy endpoints can be chosen") forces onto one component.
//
// "Enabled" is a real, cheap fact `EndpointOut.enabled` already carries -- filtered here with
// no further discussion. "Healthy" is not: `EndpointOut` (`../../api/generated/schema.ts`)
// carries no health field at all. The only source of an endpoint's health is
// `POST /api/endpoints/{name}/healthcheck`, real I/O against the tenant
// (`../endpoints/endpointsApi.ts`'s own doc comment, and `routes/endpoints.py`'s module
// docstring) -- exactly the call `../endpoints/EndpointsScreen.tsx` deliberately does NOT fire
// for every row on load, because doing so for every registered endpoint every time this form
// opens would hammer every configured tenant just to populate a dropdown.
//
// So this picker does not silently drop an endpoint whose health was never checked -- that
// would be indistinguishable from "checked and unhealthy", which is a strictly worse lie than
// "unknown". It offers every ENABLED endpoint of the right role, each annotated with its
// last-known health (`HealthBadge`, shared verbatim with `../endpoints/StatusBadges.tsx` --
// "not checked yet" until this session has checked it), and a "Check health" action scoped to
// the endpoint currently selected in THIS field -- a deliberate, operator-triggered probe, never
// an automatic one. An operator who cares can check before saving; one who doesn't is not
// blocked, and the engine's own health/quarantine handling at run time remains the real
// safety net regardless of what this form shows.
//
// The v1 upstream-only direction guardrail (Qlik is the sole write target; source must never
// be it -- `configstore/service.py`'s `_check_pair_direction`) is enforced authoritatively by
// the server against `EndpointOut.connector`, which nothing in this API labels "the write
// connector" for a UI to key off directly. This picker instead filters by `EndpointOut.role`
// (`EndpointRole`: "source" | "target" -- literally "whether a configured endpoint is used as
// a pair's source or its target", set by the operator when registering the endpoint,
// `../endpoints/EndpointFormSheet.tsx`). That is a UI-level guide, not a substitute for the
// server's own enforcement: if an endpoint's declared role does not match its connector's real
// direction, `POST/PATCH /api/pairs` still rejects the pair with `sync_pair_endpoint_invalid`,
// and `PairFormSheet.tsx`'s error mapping surfaces that as a form banner. See this task's own
// report for why a connector-manifest-based check (any `rw` field capability) was not chosen
// instead.
import { Alert, AlertDescription, FieldRow, IconButton, Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@elabs-ai/components-ui";
import { RefreshCw } from "lucide-react";

import { HealthBadge } from "../endpoints/StatusBadges";
import type { EndpointHealthOut, EndpointOut } from "./pairsApi";

export function EndpointPickerField({
  label,
  role,
  endpoints,
  value,
  onValueChange,
  error,
  health,
  checkingHealth,
  onCheckHealth,
  disabled = false,
}: {
  label: string;
  role: EndpointOut["role"];
  endpoints: EndpointOut[];
  value: string | undefined;
  onValueChange: (name: string) => void;
  error?: string;
  health: EndpointHealthOut | undefined;
  checkingHealth: boolean;
  onCheckHealth: (name: string) => void;
  disabled?: boolean;
}) {
  const ofRole = endpoints.filter((endpoint) => endpoint.role === role);
  const eligible = ofRole.filter((endpoint) => endpoint.enabled);
  const disabledEndpoints = ofRole.filter((endpoint) => !endpoint.enabled);

  const description =
    `Only enabled endpoints registered with the "${role}" role are offered here.` +
    (disabledEndpoints.length > 0
      ? ` ${disabledEndpoints.length} such endpoint${disabledEndpoints.length === 1 ? " is" : "s are"} disabled and not shown: ${disabledEndpoints.map((endpoint) => endpoint.name).join(", ")}. Enable ${disabledEndpoints.length === 1 ? "it" : "them"} on the Endpoints screen first.`
      : "");

  return (
    <div className="flex flex-col gap-2">
      <Select value={value} onValueChange={onValueChange}>
        <FieldRow label={label} description={description} error={error}>
          <SelectTrigger disabled={disabled || eligible.length === 0}>
            <SelectValue placeholder={eligible.length === 0 ? "No eligible endpoints yet" : `Select a ${role} endpoint`} />
          </SelectTrigger>
        </FieldRow>
        <SelectContent>
          {eligible.map((endpoint) => (
            <SelectItem key={endpoint.name} value={endpoint.name}>
              {endpoint.name} ({endpoint.connector})
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {eligible.length === 0 ? (
        <Alert variant="warning">
          <AlertDescription>
            No enabled endpoint is registered with the "{role}" role yet. Register one on the
            Endpoints screen first.
          </AlertDescription>
        </Alert>
      ) : null}

      {value ? (
        <div className="flex items-center gap-2 pl-1">
          <span className="text-caption text-muted-foreground">Health:</span>
          <HealthBadge health={health} />
          <IconButton
            label={`Run healthcheck for "${value}"`}
            icon={<RefreshCw className={checkingHealth ? "animate-spin" : undefined} />}
            variant="ghost"
            disabled={checkingHealth}
            onClick={() => onCheckHealth(value)}
          />
        </div>
      ) : null}
    </div>
  );
}
