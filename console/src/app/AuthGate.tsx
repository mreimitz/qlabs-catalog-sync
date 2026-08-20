import { useEffect, type ReactNode } from "react";

import { loadSession } from "../auth/authApi";
import { SignInScreen } from "../auth/SignInScreen";
import { useSession } from "../auth/useSession";
import { BootScreen } from "./BootScreen";

/** Decides whether the operator sees the sign-in screen, a boot spinner, or the real app --
 * and, critically, does this WITHOUT ever navigating. `AuthGate` is rendered inside
 * `BrowserRouter` (see `App.tsx`) but never touches `window.location` itself, so swapping
 * between `SignInScreen` and `children` never changes the URL: an expired session shows
 * sign-in at whatever path the operator was on, and signing back in remounts `children`
 * (the routed app), which resolves against the URL that never moved. That is the whole
 * mechanism behind the DoD item "an expired session returns the operator to sign-in without
 * losing the route they were on, and returns them there after signing back in" -- there is no
 * saved-location state to restore because the location was never left. */
export function AuthGate({ children }: { children: ReactNode }) {
  const session = useSession();

  useEffect(() => {
    // The boot sequence (auth.py's module doc): the bundle loads -> GET /api/auth/session ->
    // 200 means signed in (and hands back the CSRF token this SPA cannot read from the
    // HttpOnly cookie itself), 401 means show sign-in. Runs once. A LATER 401 from any other
    // call is handled generically by the API client's own middleware (../api/client.ts), not
    // here -- this effect only decides the state nothing else has decided yet.
    void loadSession();
  }, []);

  if (session.status === "loading") {
    return <BootScreen />;
  }
  if (session.status === "signed-out") {
    return <SignInScreen />;
  }
  return <>{children}</>;
}
