// The three calls `auth.py`'s `AUTH_SESSION_ROUTE` exposes, and nothing else -- every other
// screen's data fetching lives with that screen, not here. Each function updates
// `sessionStore` itself on success, so callers (the boot sequence, `SignInScreen`, the
// sign-out control) never touch the store directly.
import { apiClient, toApiError, type ApiError } from "../api/client";
import { setSignedIn, setSignedOut } from "./sessionStore";

export interface SignInResult {
  ok: boolean;
  error?: ApiError;
}

/** The boot sequence (`auth.py`'s module doc): the bundle loads, this runs once, and its
 * result is the only thing that decides whether the shell or the sign-in screen renders
 * first. 200 means signed in already -- and hands back the CSRF token this SPA cannot read
 * from the `HttpOnly` cookie itself. 401 (or any other failure) means show sign-in; the
 * 401-specific reaction is also handled generically by the API client's own middleware
 * (`../api/client.ts`), this call just makes the *initial* state deterministic without
 * waiting on a request nothing has made yet. */
export async function loadSession(): Promise<void> {
  const { data } = await apiClient.GET("/api/auth/session");
  if (data) {
    setSignedIn({ username: data.username, csrfToken: data.csrf_token, expiresAt: data.expires_at });
  } else {
    setSignedOut();
  }
}

/** `POST /api/auth/session`. On success, updates the session store directly -- there is
 * nothing else for a caller to do; the shell reacts to the store, not to this call's return
 * value. On failure, returns the typed error so `SignInScreen` can render it (one message for
 * every failure reason -- `auth.py`'s `ConsoleAuth.sign_in` gives the route nothing finer to
 * report, and this function must not invent a finer one). */
export async function signIn(username: string, password: string): Promise<SignInResult> {
  const { data, error } = await apiClient.POST("/api/auth/session", {
    body: { username, password },
  });
  if (data) {
    setSignedIn({ username: data.username, csrfToken: data.csrf_token, expiresAt: data.expires_at });
    return { ok: true };
  }
  return { ok: false, error: toApiError(error) };
}

/** `DELETE /api/auth/session`. Clears the local session unconditionally once the request
 * settles -- whether the server confirmed it or the call failed for some other reason (a
 * network error included: `apiClient` rejects rather than returning `{error}` for those, so
 * this is a real `catch`, not just a `finally`), the operator's intent was to leave, and
 * there is nothing safer to do locally than to stop presenting a session that may already be
 * gone server-side. */
export async function signOut(): Promise<void> {
  try {
    await apiClient.DELETE("/api/auth/session");
  } catch {
    // See the comment above: any failure here still means "stop presenting this session".
  } finally {
    setSignedOut();
  }
}
