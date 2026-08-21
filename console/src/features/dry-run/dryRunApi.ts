// Thin wrappers over the real `apiClient` (`../../api/client`) for the two routes this screen
// needs: `GET /api/pairs` (populate the pair picker) and `POST /api/pairs/{pair_id}/dry-run`
// (the plan itself). Mirrors `../pairs/pairsApi.ts`, `../runs/runsApi.ts` and
// `../selection/selectionApi.ts` exactly: one file, one place in this feature that calls
// `apiClient`, and every call wrapped in `try`/`catch` (never a bare `await`) because
// `apiClient` REJECTS -- rather than returning `{error}` -- for a failure below the HTTP
// layer.
//
// `listPairs` duplicates the other three features' own copies almost verbatim rather than
// importing one -- deliberate, not an oversight; see `selectionApi.ts`'s own doc comment for
// why each feature owns its one-file API surface instead of reaching into a sibling's.
import { apiClient, toApiError, type ApiError } from "../../api/client";
import type { components } from "../../api/generated/schema";

export type SyncPairOut = components["schemas"]["SyncPairOut"];
export type EntityType = components["schemas"]["EntityType"];
export type RecordOutcome = components["schemas"]["RecordOutcome"];
export type SkipReason = components["schemas"]["SkipReason"];
export type RunStatus = components["schemas"]["RunStatus"];
export type DryRunRequest = components["schemas"]["DryRunRequest"];
export type RunReportsOut = components["schemas"]["RunReportsOut"];
export type SyncRunReportOut = components["schemas"]["SyncRunReportOut"];
export type RecordReportOut = components["schemas"]["RecordReportOut"];
export type DroppedFieldOut = components["schemas"]["DroppedFieldOut"];
export type WithheldFieldOut = components["schemas"]["WithheldFieldOut"];
export type OrphanReportOut = components["schemas"]["OrphanReportOut"];
export type ErrorReportOut = components["schemas"]["ErrorReportOut"];
export type WatermarkOut = components["schemas"]["WatermarkOut"];
export type RunCountsOut =
  components["schemas"]["qlabs_catalog_sync__api__routes__run_control__RunCountsOut"];

export type ApiResult<T> = { ok: true; data: T } | { ok: false; error: ApiError };

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function fail<T>(error: unknown): ApiResult<T> {
  return { ok: false, error: toApiError(error) };
}

/** `GET /api/pairs` -- every configured sync pair; a dry run has no meaning until one is
 * chosen (a plan is always scoped to exactly one pair). */
export async function listPairs(): Promise<ApiResult<SyncPairOut[]>> {
  try {
    const { data, error } = await apiClient.GET("/api/pairs");
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `POST /api/pairs/{pair_id}/dry-run` -- plan this pair's next cycle: the writes it would
 * make, zero mutations.
 *
 * Real read I/O against the source, run synchronously and bounded by the server's own
 * timeout (`run_control.py`'s module docstring: "a dry run is real I/O... potentially across
 * many pages", `DEFAULT_DRY_RUN_TIMEOUT_SECONDS` = 120s) -- this is why the screen calling
 * this function only ever does so from an explicit operator action, never from an effect that
 * fires on mount or on pair selection.
 *
 * Zero mutations, verified by reading `sync/loop.py`: `_apply_update`/`_create_or_skip` return
 * a synthetic `RecordReport` the moment `self._dry_run` is true, BEFORE calling
 * `target.update()`/`target.create()` at all, and `SyncRunReportOut.committed` is `false` on
 * every report a dry run returns. See `planGrouping.ts`'s own doc comment for the direct
 * consequence: because the target connector is never actually invoked, `target_skipped_fields`
 * -- the channel D2/D3 unresolved-reference reporting rides on -- is never populated by this
 * route with the engine as it stands today. */
export async function runDryRun(
  pairId: string,
  payload: DryRunRequest = { create_missing: false },
): Promise<ApiResult<RunReportsOut>> {
  try {
    const { data, error } = await apiClient.POST("/api/pairs/{pair_id}/dry-run", {
      params: { path: { pair_id: pairId } },
      body: payload,
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}
