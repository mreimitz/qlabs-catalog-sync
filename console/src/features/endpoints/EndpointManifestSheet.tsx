// The per-endpoint capability manifest viewer (task DoD: "view the capability manifest so an
// operator can see what the catalog actually supports before building a pair on it").
//
// `GET /api/endpoints/{name}/manifest` runs `setup()` against the real tenant, exactly like a
// healthcheck (`endpointsApi.ts`'s doc comment) -- so this component fetches ONLY when it is
// actually open (`open` transitioning to `true`), never on mount of the list screen and never
// once per row automatically. Opening this sheet for one row is the "deliberate action an
// operator takes" the route's own docstring calls for.
import { useEffect, useState } from "react";
import { Alert, AlertDescription, Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, Skeleton, StatePanel } from "@elabs-ai/components-ui";

import { readEndpointManifest, type EndpointManifestOut } from "./endpointsApi";
import { ManifestPanel } from "./ManifestPanel";
import { toApiError } from "../../api/client";

type FetchState =
  | { status: "loading" }
  | { status: "loaded"; result: EndpointManifestOut }
  | { status: "error"; message: string };

export function EndpointManifestSheet({
  endpointName,
  open,
  onOpenChange,
}: {
  endpointName: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [state, setState] = useState<FetchState>({ status: "loading" });

  useEffect(() => {
    if (!open || !endpointName) return;
    let cancelled = false;
    setState({ status: "loading" });
    void readEndpointManifest(endpointName).then((result) => {
      if (cancelled) return;
      if (result.ok) {
        setState({ status: "loaded", result: result.data });
      } else {
        setState({ status: "error", message: toApiError(result.error).message });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [open, endpointName]);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>Capability manifest{endpointName ? `: ${endpointName}` : ""}</SheetTitle>
          <SheetDescription>
            What this endpoint's connector reports it supports, read live from the configured
            tenant.
          </SheetDescription>
        </SheetHeader>
        <div className="px-4 pb-4">
          {state.status === "loading" ? (
            <div className="flex flex-col gap-2" aria-live="polite" aria-busy="true">
              <Skeleton className="h-6 w-1/2" />
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          ) : state.status === "error" ? (
            <StatePanel kind="error" title="Could not read the manifest" description={state.message} />
          ) : state.result.manifest ? (
            <ManifestPanel manifest={state.result.manifest} />
          ) : (
            // A normal, successfully-rendered fact about this endpoint right now (unreachable
            // tenant, unresolved secret, ...) -- the same "200 describing a state, not an HTTP
            // failure" contract as a red healthcheck. Rendered calmly, not as an error state.
            <Alert variant="info">
              <AlertDescription>
                {state.result.unavailable_reason ?? "This endpoint did not report a manifest."}
              </AlertDescription>
            </Alert>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
