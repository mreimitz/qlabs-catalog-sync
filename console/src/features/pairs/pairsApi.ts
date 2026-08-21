// Thin wrappers over the real `apiClient` (`../../api/client`) for the `/pairs` routes this
// screen needs, plus the two endpoint-side calls the source/target pickers need
// (`GET /api/endpoints`, `POST /api/endpoints/{name}/healthcheck`). Mirrors
// `../endpoints/endpointsApi.ts` exactly: no screen in this feature calls `apiClient`
// directly, this is the one place that does, and every call is wrapped in `try`/`catch`
// (never a bare `await`) because `apiClient` REJECTS -- rather than returning `{error}` --
// for a failure below the HTTP layer, and this screen fetches on mount, so an uncaught
// rejection here would be an unhandled promise rejection nobody catches.
//
// `listEndpoints`/`runEndpointHealthcheck` duplicate two of `endpointsApi.ts`'s own
// functions almost verbatim rather than importing them from `../endpoints/endpointsApi`.
// That is deliberate, not an oversight: each feature owns its own one-file API surface (see
// that file's own doc comment -- "No screen in this feature calls apiClient directly; this
// is the one place that does"), so this feature does not reach into a sibling feature's
// internals for two thin wrappers it can trivially restate itself. The alternative --
// importing across `features/*` -- would make `features/pairs` depend on `features/endpoints`
// for no reason beyond avoiding ~15 duplicate lines, and would break the moment endpoints'
// own module reorganizes for endpoints-specific reasons that have nothing to do with pairs.
import { apiClient, toApiError, type ApiError } from "../../api/client";
import type { components } from "../../api/generated/schema";

export type EndpointOut = components["schemas"]["EndpointOut"];
export type EndpointHealthOut = components["schemas"]["EndpointHealthOut"];
export type SyncPairOut = components["schemas"]["SyncPairOut"];
export type SyncPairCreateRequest = components["schemas"]["SyncPairCreateRequest"];
export type SyncPairUpdateRequest = components["schemas"]["SyncPairUpdateRequest"];
export type EntityType = components["schemas"]["EntityType"];
export type ManualEditPolicy = components["schemas"]["ManualEditPolicy"];
export type ManualEditMode = components["schemas"]["ManualEditMode"];

export type ApiResult<T> = { ok: true; data: T } | { ok: false; error: ApiError };

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function fail<T>(error: unknown): ApiResult<T> {
  return { ok: false, error: toApiError(error) };
}

/** `GET /api/endpoints` -- every registered endpoint, needed to populate the source/target
 * pickers. Same bare-array shape as `endpointsApi.ts`'s own `listEndpoints` (verified against
 * `openapi.json`, not assumed -- see that file and `../../test/apiFixtures.ts`). */
export async function listEndpoints(): Promise<ApiResult<EndpointOut[]>> {
  try {
    const { data, error } = await apiClient.GET("/api/endpoints");
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `POST /api/endpoints/{name}/healthcheck` -- real I/O against the tenant. Never fired
 * automatically for every endpoint in the picker (that would hammer every tenant on every
 * pair-form open); only in response to an operator clicking "Check health" for the endpoint
 * they are currently considering. See `PairFormSheet.tsx`'s doc comment for the full
 * trade-off this screen makes for "only enabled, healthy endpoints can be chosen". */
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

/** `GET /api/pairs` -- every configured sync pair. */
export async function listPairs(): Promise<ApiResult<SyncPairOut[]>> {
  try {
    const { data, error } = await apiClient.GET("/api/pairs");
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `POST /api/pairs` -- create a sync pair naming two already-registered endpoints. */
export async function createPair(payload: SyncPairCreateRequest): Promise<ApiResult<SyncPairOut>> {
  try {
    const { data, error } = await apiClient.POST("/api/pairs", { body: payload });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `PATCH /api/pairs/{pair_id}` -- edit any subset of a pair's fields, including toggling
 * `enabled` (pause/resume the schedule -- NOT a delete, see `PairsScreen.tsx`) and
 * `activation_opt_in` (D7). */
export async function updatePair(
  pairId: string,
  payload: SyncPairUpdateRequest,
): Promise<ApiResult<SyncPairOut>> {
  try {
    const { data, error } = await apiClient.PATCH("/api/pairs/{pair_id}", {
      params: { path: { pair_id: pairId } },
      body: payload,
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `DELETE /api/pairs/{pair_id}` -- deletes the pair and cascades its selection rules and
 * overrides (`pairs.py`'s own summary). Distinct from disabling it -- see `PairsScreen.tsx`. */
export async function deletePair(pairId: string): Promise<ApiResult<void>> {
  try {
    const { error } = await apiClient.DELETE("/api/pairs/{pair_id}", {
      params: { path: { pair_id: pairId } },
    });
    if (!error) return ok(undefined);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}
