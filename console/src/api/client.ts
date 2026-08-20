// The console's one HTTP client (C7, C8). Every screen calls the engine's REST API through
// `apiClient`, never through a bare `fetch`, because three things must not be left to each
// call site to remember:
//
//  1. Base URL is the page's own origin (C8: one artifact, one origin, no CORS) -- an empty
//     `baseUrl` is what makes every request resolve relative to `window.location`. Never an
//     absolute URL, never read from an environment variable naming another origin.
//  2. `credentials: "same-origin"` on every request, or the `HttpOnly` session cookie
//     (`auth.py`'s `SESSION_COOKIE`/`SESSION_COOKIE_SECURE`) is never sent and every
//     authenticated call 401s.
//  3. Every mutating request needs the `X-CSRF-Token` header (`auth.py`'s `CSRF_HEADER`).
//     Implemented as `openapi-fetch` middleware (`csrfMiddleware` below) so a future screen
//     author cannot forget it, and read from the session store *at request time* -- never a
//     captured constant -- because the token changes on every sign-in and does not exist at
//     all while signed out (`auth.py`: the CSRF token is bound to the session record, never a
//     global constant).
//
// A 401 on ANY call -- not just the auth routes -- means the session is gone: expired,
// revoked, or the service restarted (`auth.py`'s module docstring: sessions do not survive a
// restart). `sessionExpiryMiddleware` below is what makes that distinct from an ordinary
// failure: it clears the session store on every 401, and the shell reacts to that store
// (`../auth/useSession.ts`), not to any particular screen having noticed.
import createClient from "openapi-fetch";

import type { components, paths } from "./generated/schema";
import { getCsrfToken, setSignedOut } from "../auth/sessionStore";

const CSRF_HEADER = "X-CSRF-Token";
const MUTATING_METHODS: ReadonlySet<string> = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** Resolves an origin-relative path (`"/api/..."`) against `window.location.origin` before
 * constructing the real `Request`. A browser's own `fetch`/`Request` already does this
 * implicitly against `document.baseURI` -- so in production this changes nothing observable,
 * it just makes C8's "the page's own origin" explicit instead of relying on an implicit
 * browser default. It also happens to be *necessary*, not just tidy: Node's global
 * `fetch`/`Request` (undici, what actually runs under Vitest + jsdom, since jsdom itself does
 * not implement fetch) has no concept of "the current document" and throws on a relative URL
 * unconditionally. Resolving explicitly here is what lets this exact client -- not a mocked
 * substitute -- be driven through a stubbed `fetch` in tests. */
class OriginRelativeRequest extends Request {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    if (typeof input === "string" && input.startsWith("/") && typeof globalThis.location !== "undefined") {
      super(`${globalThis.location.origin}${input}`, init);
    } else {
      super(input, init);
    }
  }
}

export const apiClient = createClient<paths>({
  // C8: same origin as the page that served this bundle -- see the module doc comment.
  baseUrl: "",
  credentials: "same-origin",
  Request: OriginRelativeRequest,
  // A thin wrapper, rather than `fetch: globalThis.fetch`, so the *current* global fetch is
  // read on every call instead of being captured once when this module loads. Production
  // behavior is identical (there is exactly one `fetch` in a real browser); it is what lets
  // tests stub `globalThis.fetch` per test without reconstructing the client.
  fetch: (request) => globalThis.fetch(request),
});

apiClient.use({
  onRequest({ request }) {
    if (MUTATING_METHODS.has(request.method)) {
      const token = getCsrfToken();
      if (token) {
        request.headers.set(CSRF_HEADER, token);
      }
    }
    return request;
  },
  onResponse({ response }) {
    if (response.status === 401) {
      setSignedOut();
    }
    return response;
  },
});

/** The console's one typed failure shape. Every route in `openapi.json` declares
 * `ErrorModel` (`errors.py`'s `API_ERROR_RESPONSES`) as its `default` response, so
 * `openapi-fetch` already types a failed call's `error` field as this shape for every
 * operation -- this is just the name screens import instead of reaching into the schema.
 * `.code` is stable and is what screens should switch on; `.message` is human-readable and
 * may be reworded without notice. */
export type ApiError = components["schemas"]["ErrorModel"];

/** Narrow an unknown value to the typed `ApiError` shape, with a safe fallback for the one
 * case that cannot happen against a conforming server but must still not crash the caller: a
 * response that fails to parse as `ErrorModel` at all. */
export function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { code?: unknown }).code === "string" &&
    typeof (value as { message?: unknown }).message === "string"
  );
}

/** Turn a failed `openapi-fetch` result's `error` field into the typed `ApiError` shape every
 * route promises -- the helper the task brief asks for, so a screen has one name to reach for
 * instead of re-deriving this on every call site. */
export function toApiError(error: unknown): ApiError {
  if (isApiError(error)) {
    return error;
  }
  return {
    code: "unknown_error",
    message: "The server returned an error in an unexpected shape.",
  };
}
