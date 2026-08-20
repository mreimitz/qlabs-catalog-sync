import { useSyncExternalStore } from "react";

import { getSessionState, subscribeSession, type SessionState } from "./sessionStore";

/** The console's session state, reactive: any component that calls this re-renders the
 * instant `sessionStore` changes -- a sign-in, a sign-out, or a 401 from any API call
 * anywhere in the app (`../api/client.ts`'s middleware). This is the ONLY way screens
 * should read session state; nothing should read `sessionStore` directly outside this
 * hook and the API client itself. */
export function useSession(): SessionState {
  return useSyncExternalStore(subscribeSession, getSessionState, getSessionState);
}
