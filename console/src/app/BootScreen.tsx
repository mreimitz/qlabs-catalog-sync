import { StatePanel } from "@elabs-ai/components-ui";

/** Shown for the brief window between the bundle mounting and the boot sequence's
 * `GET /api/auth/session` resolving (`auth.py`'s module contract). Neither "signed in" nor
 * "signed out" is known yet, so this must not guess either way -- it is a loading state, not
 * an empty or an error one. */
export function BootScreen() {
  return (
    <div className="flex min-h-dvh items-center justify-center">
      <StatePanel kind="loading" title="Loading QLabs Catalog Sync" loadingLabel="Loading session" />
    </div>
  );
}
