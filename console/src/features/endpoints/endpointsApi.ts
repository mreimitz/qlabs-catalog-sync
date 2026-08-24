// Thin wrappers over the real `apiClient` (`../../api/client`) for the six `/connectors` and
// `/endpoints*` routes this screen needs. Every function returns the typed success value or a
// typed `ApiError` (via `toApiError`) -- never throws for an ordinary 4xx, mirroring
// `../../auth/authApi.ts`'s own `{ok, data?, error?}` shape. No screen in this feature calls
// `apiClient` directly; this is the one place that does, so every request this feature makes is
// visible in one file.
//
// Every call below is wrapped in `try`/`catch`, not just `await`ed bare: `apiClient` REJECTS
// rather than returning `{error}` for a failure below the HTTP layer (`../../auth/authApi.ts`'s
// `signOut` doc comment already documents and relies on this for the same reason). This screen
// fetches automatically on mount (`EndpointsScreen.tsx`'s `fetchAll`), so an uncaught rejection
// here would surface as an unhandled promise rejection outside of any test's or any caller's
// control -- exactly the class of bug this file exists to not have. Caught and converted via
// `toApiError`, it becomes an ordinary `ApiResult` failure the caller already knows how to
// render.
import { apiClient, toApiError, type ApiError } from "../../api/client";
import type { components } from "../../api/generated/schema";

export type ConnectorInfo = components["schemas"]["ConnectorInfo"];
export type EndpointOut = components["schemas"]["EndpointOut"];
export type EndpointCreateRequest = components["schemas"]["EndpointCreateRequest"];
export type EndpointUpdateRequest = components["schemas"]["EndpointUpdateRequest"];
export type EndpointHealthOut = components["schemas"]["EndpointHealthOut"];
export type EndpointManifestOut = components["schemas"]["EndpointManifestOut"];
export type SecretResolveOut = components["schemas"]["SecretResolveOut"];
export type StoredSecretOut = components["schemas"]["StoredSecretOut"];
export type CapabilityManifestOut = components["schemas"]["CapabilityManifestOut"];

export type ApiResult<T> = { ok: true; data: T } | { ok: false; error: ApiError };

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function fail<T>(error: unknown): ApiResult<T> {
  return { ok: false, error: toApiError(error) };
}

/** `GET /api/connectors` -- every connector this running image discovered (C6), available or
 * broken, each with (or without, and why) a live capability manifest. Called once per screen
 * load, never per row. */
export async function listConnectors(): Promise<ApiResult<ConnectorInfo[]>> {
  try {
    const { data, error } = await apiClient.GET("/api/connectors");
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `GET /api/endpoints` -- every registered endpoint. */
export async function listEndpoints(): Promise<ApiResult<EndpointOut[]>> {
  try {
    const { data, error } = await apiClient.GET("/api/endpoints");
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `POST /api/endpoints` -- register an instance of an already-discovered connector (C6). */
export async function createEndpoint(
  payload: EndpointCreateRequest,
): Promise<ApiResult<EndpointOut>> {
  try {
    const { data, error } = await apiClient.POST("/api/endpoints", { body: payload });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `PATCH /api/endpoints/{name}` -- edit settings, secret reference, role, connector or
 * enabled. */
export async function updateEndpoint(
  name: string,
  payload: EndpointUpdateRequest,
): Promise<ApiResult<EndpointOut>> {
  try {
    const { data, error } = await apiClient.PATCH("/api/endpoints/{name}", {
      params: { path: { name } },
      body: payload,
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `DELETE /api/endpoints/{name}`. */
export async function deleteEndpoint(name: string): Promise<ApiResult<void>> {
  try {
    const { error } = await apiClient.DELETE("/api/endpoints/{name}", {
      params: { path: { name } },
    });
    if (!error) return ok(undefined);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `GET /api/endpoints/{name}/secret-resolve` (C2) -- whether the bound reference resolves,
 * never the value. Cheap, local/backend-only work (no tenant I/O -- see
 * `configstore/secrets.py`'s `resolve_status`), so this is safe and intended to be called for
 * every row on list load; unlike `/healthcheck` and `/manifest`, it never touches a tenant. */
export async function resolveEndpointSecret(name: string): Promise<ApiResult<SecretResolveOut>> {
  try {
    const { data, error } = await apiClient.GET("/api/endpoints/{name}/secret-resolve", {
      params: { path: { name } },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `GET /api/endpoints/{name}/secrets` -- every secret-typed field this endpoint's connector
 * declares, and whether a credential has been saved for it. Never the credential: the response
 * has no field capable of carrying one. Fields with nothing saved are listed too, which is what
 * lets the form render "this connector wants a client secret and none has been entered". */
export async function listEndpointSecrets(name: string): Promise<ApiResult<StoredSecretOut[]>> {
  try {
    const { data, error } = await apiClient.GET("/api/endpoints/{name}/secrets", {
      params: { path: { name } },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `PUT /api/endpoints/{name}/secrets/{field}` -- save or replace one credential. The only
 * request in this console that carries a credential value; it is sealed server-side before it
 * reaches the database and no route ever returns it. Idempotent, which is what re-submitting
 * after a typo needs to be. */
export async function putEndpointSecret(
  name: string,
  field: string,
  value: string,
): Promise<ApiResult<void>> {
  try {
    const { error } = await apiClient.PUT("/api/endpoints/{name}/secrets/{field}", {
      params: { path: { name, field } },
      body: { value },
    });
    if (!error) return ok(undefined);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `DELETE /api/endpoints/{name}/secrets/{field}` -- remove one saved credential. A 404 means
 * there was nothing stored, which is a different answer than "removed" and is surfaced as
 * such. */
export async function deleteEndpointSecret(
  name: string,
  field: string,
): Promise<ApiResult<void>> {
  try {
    const { error } = await apiClient.DELETE("/api/endpoints/{name}/secrets/{field}", {
      params: { path: { name, field } },
    });
    if (!error) return ok(undefined);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `POST /api/endpoints/{name}/healthcheck` -- real I/O against the tenant (`setup()` +
 * `healthcheck()`). Always a 200 describing the endpoint's state, red or green -- never an
 * error response for an unhealthy result (see `routes/endpoints.py`'s module docstring). A
 * deliberate, operator-triggered action; never fired automatically for every row. */
export async function runEndpointHealthcheck(
  name: string,
): Promise<ApiResult<EndpointHealthOut>> {
  try {
    const { data, error } = await apiClient.POST("/api/endpoints/{name}/healthcheck", {
      params: { path: { name } },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `GET /api/endpoints/{name}/manifest` -- the capability manifest for a *configured*
 * endpoint; runs `setup()` against the tenant, exactly like the healthcheck. Always a 200:
 * `manifest` or `unavailable_reason`, never both, never neither. A deliberate,
 * operator-triggered action (a button / an expanded panel) -- never fired automatically for
 * every row on list load, which would hammer every configured tenant on every page view. */
export async function readEndpointManifest(
  name: string,
): Promise<ApiResult<EndpointManifestOut>> {
  try {
    const { data, error } = await apiClient.GET("/api/endpoints/{name}/manifest", {
      params: { path: { name } },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}
