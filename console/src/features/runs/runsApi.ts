// Thin wrappers over the real `apiClient` (`../../api/client`) for every route this feature
// needs: run history (`GET /api/runs`, `GET /api/runs/{run_id}`,
// `GET /api/runs/{run_id}/issues`), the configuration change log (`GET /api/config-changes`),
// run control (`POST /api/pairs/{pair_id}/run-now|pause|resume`,
// `GET /api/pairs/{pair_id}/run-status`), and the one read of `GET /api/pairs` this screen
// needs to populate its pair picker. Mirrors `../pairs/pairsApi.ts` and
// `../endpoints/endpointsApi.ts` exactly: no screen in this feature calls `apiClient`
// directly, this is the one place that does, and every call is wrapped in `try`/`catch`
// (never a bare `await`) because `apiClient` REJECTS -- rather than returning `{error}` --
// for a failure below the HTTP layer, and every screen here fetches on mount.
//
// `listPairs` duplicates `../pairs/pairsApi.ts`'s own function almost verbatim rather than
// importing it -- deliberate, not an oversight, for the same reason `pairsApi.ts` itself
// gives for duplicating two of `../endpoints/endpointsApi.ts`'s functions: each feature owns
// its own one-file API surface, so this feature does not reach into a sibling feature's
// internals for ~10 duplicate lines.
//
// Two response shapes named `RunCountsOut` exist in the generated schema
// (`qlabs_catalog_sync__api__routes__history__RunCountsOut` and
// `qlabs_catalog_sync__api__routes__run_control__RunCountsOut`) because the server has two
// Pydantic models of that name in two different route modules. This feature only ever reads
// run counts through `RunSummaryOut`/`RunDetailOut` (the history shape, which additionally
// carries `write` and `error`) -- `run_control.py`'s own `RunCountsOut` lives on
// `SyncRunReportOut`, which is T13.6's dry-run screen's concern, not this one's.
import { apiClient, toApiError, type ApiError } from "../../api/client";
import type { components } from "../../api/generated/schema";

export type SyncPairOut = components["schemas"]["SyncPairOut"];
export type EntityType = components["schemas"]["EntityType"];
export type RunRecordStatus = components["schemas"]["RunRecordStatus"];
export type RunSummaryOut = components["schemas"]["RunSummaryOut"];
export type RunDetailOut = components["schemas"]["RunDetailOut"];
export type RunIssuesOut = components["schemas"]["RunIssuesOut"];
export type RunItemOut = components["schemas"]["RunItemOut"];
export type RunErrorOut = components["schemas"]["RunErrorOut"];
export type RunOrphanIssueOut = components["schemas"]["RunOrphanIssueOut"];
export type RunListPage = components["schemas"]["RunListPage"];
export type RunCountsOut = components["schemas"]["qlabs_catalog_sync__api__routes__history__RunCountsOut"];
export type RecordOutcome = components["schemas"]["RecordOutcome"];
export type RunControlStatusOut = components["schemas"]["RunControlStatusOut"];
export type RunNowRequest = components["schemas"]["RunNowRequest"];
export type RunReportsOut = components["schemas"]["RunReportsOut"];
export type ConfigChangeOut = components["schemas"]["ConfigChangeOut"];
export type ConfigChangeListPage = components["schemas"]["ConfigChangeListPage"];
export type ChangeEntityKind = components["schemas"]["ChangeEntityKind"];
export type ChangeAction = components["schemas"]["ChangeAction"];

export type ApiResult<T> = { ok: true; data: T } | { ok: false; error: ApiError };

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function fail<T>(error: unknown): ApiResult<T> {
  return { ok: false, error: toApiError(error) };
}

/** `GET /api/pairs` -- every configured sync pair, needed to populate the pair picker that
 * drives both the run-history filter (by `SyncPairOut.name`, `RunRow.pair`'s own type) and
 * the run-control panel (by `SyncPairOut.id`, the path param every run-control route takes).
 * Same bare-array shape as `../pairs/pairsApi.ts`'s own `listPairs`. */
export async function listPairs(): Promise<ApiResult<SyncPairOut[]>> {
  try {
    const { data, error } = await apiClient.GET("/api/pairs");
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

export interface ListRunsParams {
  pair?: string;
  entity_type?: EntityType;
  status?: RunRecordStatus;
  limit?: number;
  cursor?: string;
}

/** `GET /api/runs` -- keyset-paginated, newest first. `params.cursor` is the opaque
 * `next_cursor` from a previous page; omit it for the first page. Never build a page number
 * from this -- `has_more`/`next_cursor` are the only way to know there is more. */
export async function listRuns(params: ListRunsParams = {}): Promise<ApiResult<RunListPage>> {
  try {
    const { data, error } = await apiClient.GET("/api/runs", { params: { query: params } });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `GET /api/runs/{run_id}` -- counts, items and errors for one run. */
export async function getRun(runId: string): Promise<ApiResult<RunDetailOut>> {
  try {
    const { data, error } = await apiClient.GET("/api/runs/{run_id}", {
      params: { path: { run_id: runId } },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `GET /api/runs/{run_id}/issues` -- unresolved dataset members (D2), unresolvable owners
 * (D3), orphans (D4) and errors, as one answer. */
export async function getRunIssues(runId: string): Promise<ApiResult<RunIssuesOut>> {
  try {
    const { data, error } = await apiClient.GET("/api/runs/{run_id}/issues", {
      params: { path: { run_id: runId } },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

export interface ListConfigChangesParams {
  entity_kind?: ChangeEntityKind;
  entity_id?: string;
  action?: ChangeAction;
  field?: string;
  limit?: number;
  cursor?: string;
}

/** `GET /api/config-changes` -- also keyset-paginated, newest first. Same cursor contract as
 * `listRuns` above. */
export async function listConfigChanges(
  params: ListConfigChangesParams = {},
): Promise<ApiResult<ConfigChangeListPage>> {
  try {
    const { data, error } = await apiClient.GET("/api/config-changes", { params: { query: params } });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `POST /api/pairs/{pair_id}/run-now` -- run one real cycle now. Refused (409) if the pair
 * is paused (`sync_pair_paused`) or already has a cycle in flight through this API
 * (`sync_cycle_already_running`); can also 504 (`run_now_timed_out`) if the cycle does not
 * finish inside the server's bound. Every case surfaces as an ordinary `ApiResult` failure --
 * the caller decides how to present it, never a thrown exception. */
export async function runNow(
  pairId: string,
  payload: RunNowRequest = { create_missing: false },
): Promise<ApiResult<RunReportsOut>> {
  try {
    const { data, error } = await apiClient.POST("/api/pairs/{pair_id}/run-now", {
      params: { path: { pair_id: pairId } },
      body: payload,
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `POST /api/pairs/{pair_id}/pause` -- flips the pair's `enabled` flag off (real, persisted,
 * queryable -- see `run_control.py`'s own module docstring for exactly what this does and
 * does not control today). */
export async function pausePair(pairId: string): Promise<ApiResult<RunControlStatusOut>> {
  try {
    const { data, error } = await apiClient.POST("/api/pairs/{pair_id}/pause", {
      params: { path: { pair_id: pairId } },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `POST /api/pairs/{pair_id}/resume` -- flips the pair's `enabled` flag back on. */
export async function resumePair(pairId: string): Promise<ApiResult<RunControlStatusOut>> {
  try {
    const { data, error } = await apiClient.POST("/api/pairs/{pair_id}/resume", {
      params: { path: { pair_id: pairId } },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `GET /api/pairs/{pair_id}/run-status` -- whether this pair is paused and/or has a
 * run-now cycle in flight through this API process, read straight back from the database
 * (`enabled`/`paused`) plus this process's own in-flight tracking (`running`). The only
 * source of truth this screen trusts for "is run-now live right now" -- never inferred from
 * a button's own local pending state alone. */
export async function getRunStatus(pairId: string): Promise<ApiResult<RunControlStatusOut>> {
  try {
    const { data, error } = await apiClient.GET("/api/pairs/{pair_id}/run-status", {
      params: { path: { pair_id: pairId } },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}
