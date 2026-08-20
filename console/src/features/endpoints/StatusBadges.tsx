// Small presentational badges for the three distinct kinds of status this screen renders,
// each calm and non-alarming even for a "bad" state (a red healthcheck, an unresolvable
// secret) -- these are facts about an endpoint, not errors in the request that asked. See
// `EndpointsScreen.tsx`'s module doc for why these are never a toast.
import { Badge, Tooltip, TooltipContent, TooltipTrigger } from "@elabs-ai/components-ui";

import type { EndpointHealthOut, SecretResolveOut } from "./endpointsApi";

/** The endpoint's connector's live/registrable state, per `ConnectorInfo` (C6). Three
 * distinct facts, three distinct treatments -- see the task's own description:
 *   - not `available`: discovery could not load this entry point (broken).
 *   - `available` with no manifest: loaded, registrable, describes itself once configured.
 *   - `available` with a manifest: loaded and self-describing now.
 * Never conflate "cannot describe itself yet" with "broken" or "supports nothing". */
export function ConnectorStateBadge({
  available,
  hasManifest,
}: {
  available: boolean;
  hasManifest: boolean;
}) {
  if (!available) {
    return <Badge variant="destructive">Unavailable</Badge>;
  }
  if (!hasManifest) {
    return <Badge variant="secondary">Describes itself once configured</Badge>;
  }
  return <Badge variant="success">Available</Badge>;
}

const HEALTH_VARIANT = {
  healthy: "success",
  degraded: "warning",
  unhealthy: "destructive",
} as const;

/** Renders the *last-known* result of a `POST /healthcheck` this operator ran (or "not
 * checked yet" -- healthchecking every row automatically on list load would hammer every
 * configured tenant on every page view, see `endpointsApi.ts`'s doc comment). A red result is
 * a calm status, never an error tone beyond the ordinary warning/destructive badge palette
 * already used for e.g. a paused pipeline elsewhere in this design system -- specifically
 * never a toast (see `EndpointsScreen.tsx`). */
export function HealthBadge({ health }: { health: EndpointHealthOut | null | undefined }) {
  if (!health) {
    return <Badge variant="outline">Not checked yet</Badge>;
  }
  const badge = <Badge variant={HEALTH_VARIANT[health.state]}>{health.state}</Badge>;
  if (!health.reason) {
    return badge;
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span>{badge}</span>
      </TooltipTrigger>
      <TooltipContent>{health.reason}</TooltipContent>
    </Tooltip>
  );
}

/** Whether this endpoint's `secret_ref` resolves (C2) -- fetched for every row on list load
 * (a cheap, local backend lookup, not tenant I/O -- see `endpointsApi.ts`), so an
 * unresolvable reference is visible at a glance, per the task's own DoD, without an extra
 * click. Never the resolved value -- only whether it resolves and why not. */
export function SecretResolveBadge({
  status,
}: {
  status: SecretResolveOut | "loading" | undefined;
}) {
  if (status === undefined || status === "loading") {
    return <Badge variant="outline">Checking…</Badge>;
  }
  const badge = (
    <Badge variant={status.resolvable ? "success" : "destructive"}>
      {status.resolvable ? "Resolves" : "Unresolvable"}
    </Badge>
  );
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span>{badge}</span>
      </TooltipTrigger>
      <TooltipContent>{status.reason}</TooltipContent>
    </Tooltip>
  );
}
