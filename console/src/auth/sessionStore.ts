// The single source of truth for the console's session state (C7).
//
// This is a plain module-level store, not a React context, because the API client
// (`../api/client.ts`) needs to read the current CSRF token at request time and react to a
// 401 by clearing the session -- and it is not a React component. Both the client and the
// `useSession` hook (`./useSession.ts`) read and write through this one store, so there is
// exactly one place session state lives, and the two can never disagree.
//
// What is deliberately NOT here: the session cookie (HttpOnly, this app never reads it) and
// anywhere durable for the CSRF token. The token lives in this in-memory store only -- never
// localStorage, sessionStorage, a cookie this app sets, or a URL -- so it is lost on reload by
// design. `GET /api/auth/session` (see `authApi.ts`) is what re-establishes it, exactly as
// `auth.py`'s module docstring specifies.

export type SessionState =
  | { status: "loading" }
  | { status: "signed-out" }
  | { status: "signed-in"; username: string; csrfToken: string; expiresAt: string };

type Listener = () => void;

let state: SessionState = { status: "loading" };
const listeners = new Set<Listener>();

function emit(): void {
  for (const listener of listeners) {
    listener();
  }
}

/** The current session state. Read by `useSession()` (React, via `useSyncExternalStore`) and
 * by the API client's CSRF middleware (non-React, via `getCsrfToken` below). */
export function getSessionState(): SessionState {
  return state;
}

/** Subscribe to every session-state change; returns an unsubscribe function.
 * `useSyncExternalStore`-compatible. */
export function subscribeSession(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Record a successful sign-in or session recovery (`POST`/`GET /api/auth/session`). */
export function setSignedIn(session: { username: string; csrfToken: string; expiresAt: string }): void {
  state = { status: "signed-in", ...session };
  emit();
}

/** Record that there is no live session -- sign-out, a 401 from any call, or the initial
 * boot check coming back unauthenticated. Idempotent: calling it while already signed out
 * does not re-emit, so (for example) a 401 from a stray request after sign-out is a no-op
 * rather than a redundant re-render. */
export function setSignedOut(): void {
  if (state.status === "signed-out") {
    return;
  }
  state = { status: "signed-out" };
  emit();
}

/** The live session's CSRF token, or `null` when there is none. Read by the API client's
 * middleware (`../api/client.ts`) at request time on every mutating call -- never cached or
 * captured once, because it changes on every sign-in and does not exist while signed out
 * (`auth.py`: the token is bound to the session record, never a global constant). */
export function getCsrfToken(): string | null {
  return state.status === "signed-in" ? state.csrfToken : null;
}
