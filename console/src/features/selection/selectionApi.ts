// Thin wrappers over the real `apiClient` (`../../api/client`) for the routes the selection
// screen needs. Mirrors `../pairs/pairsApi.ts` and `../endpoints/endpointsApi.ts` exactly: no
// component in this feature calls `apiClient` directly, this is the one place that does, and
// every call is wrapped in `try`/`catch` (never a bare `await`) because `apiClient` REJECTS --
// rather than returning `{error}` -- for a failure below the HTTP layer, and this screen
// fetches on mount, so an uncaught rejection here would be an unhandled promise rejection
// nobody catches.
//
// Two pagination contracts meet on this screen and they are NOT the same shape. The source
// tree is **offset**-paginated (`{nodes, offset, limit, has_more, next_offset}`); `/api/runs`
// -- which this feature never calls -- is **keyset**-paginated with an opaque `next_cursor`.
// Both were read off `openapi.json`, not assumed from each other.
//
// `listPairs` duplicates `../pairs/pairsApi.ts`'s own function rather than importing it, for
// the reason that file's own doc comment gives: each feature owns its one-file API surface and
// does not reach into a sibling feature's internals for a thin wrapper it can restate.
import { apiClient, toApiError, type ApiError } from "../../api/client";
import type { components } from "../../api/generated/schema";

export type ApiError_ = ApiError;

export type RuleScope = components["schemas"]["RuleScope"];
export type SelectionDecision = components["schemas"]["SelectionDecision"];
export type MatcherKind = components["schemas"]["MatcherKind"];
export type DecisionSource = components["schemas"]["DecisionSource"];
export type CandidateFact = components["schemas"]["CandidateFact"];
export type EntityType = components["schemas"]["EntityType"];
export type FieldCapabilityMode = components["schemas"]["FieldCapabilityMode"];

export type SyncPairOut = components["schemas"]["SyncPairOut"];
export type EndpointManifestOut = components["schemas"]["EndpointManifestOut"];
export type SelectionRuleOut = components["schemas"]["SelectionRuleOut"];
export type SelectionRuleCreateRequest = components["schemas"]["SelectionRuleCreateRequest"];
export type SelectionRuleUpdateRequest = components["schemas"]["SelectionRuleUpdateRequest"];
export type SelectionOverrideOut = components["schemas"]["SelectionOverrideOut"];
export type SelectionOverrideCreateRequest =
  components["schemas"]["SelectionOverrideCreateRequest"];
export type SourceTreePageOut = components["schemas"]["SourceTreePageOut"];
export type SchemaNodeOut = components["schemas"]["SchemaNodeOut"];
export type DatasetNodeOut = components["schemas"]["DatasetNodeOut"];
export type SelectionResultOut = components["schemas"]["SelectionResultOut"];
export type UndeterminedRuleOut = components["schemas"]["UndeterminedRuleOut"];
export type PreviewRequest = components["schemas"]["PreviewRequest"];
export type PreviewOut = components["schemas"]["PreviewOut"];
export type ScopeCountsOut = components["schemas"]["ScopeCountsOut"];
export type PreviewSampleItemOut = components["schemas"]["PreviewSampleItemOut"];
export type DraftSelectionRuleIn = components["schemas"]["DraftSelectionRuleIn"];

export type ApiResult<T> = { ok: true; data: T } | { ok: false; error: ApiError };

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function fail<T>(error: unknown): ApiResult<T> {
  return { ok: false, error: toApiError(error) };
}

/** `GET /api/pairs` -- every configured sync pair; the screen has no meaning until one is
 * chosen (a selection rule's whole scope is one pair). */
export async function listPairs(): Promise<ApiResult<SyncPairOut[]>> {
  try {
    const { data, error } = await apiClient.GET("/api/pairs");
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `GET /api/endpoints/{name}/manifest` -- what the pair's SOURCE endpoint's connector can
 * actually report, which is what decides whether a tag or owner matcher can ever reach a
 * verdict against it (`selection/source_tree.py`'s `tags_offered`/`owners_offered`). Real I/O
 * against the tenant, exactly like a healthcheck -- fired once when a pair is selected, never
 * per rule row and never per keystroke. See `matcherSupport.ts` for what is done with it. */
export async function readSourceManifest(name: string): Promise<ApiResult<EndpointManifestOut>> {
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

/** `GET /api/pairs/{pair_id}/rules?scope=` -- one scope's rules. The server returns them
 * ordinal-ascending, and `SelectionRuleOut.ordinal` carries the evaluation position
 * explicitly; this screen sorts on `ordinal` rather than trusting array position, because
 * `ordinal` is the contract (`routes/selection.py`'s module docstring) and array order is a
 * convenience that agrees with it. */
export async function listRules(
  pairId: string,
  scope: RuleScope,
): Promise<ApiResult<SelectionRuleOut[]>> {
  try {
    const { data, error } = await apiClient.GET("/api/pairs/{pair_id}/rules", {
      params: { path: { pair_id: pairId }, query: { scope } },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `POST /api/pairs/{pair_id}/rules` -- create one rule. `ordinal` is deliberately omitted:
 * the server appends, and `reorderRules` immediately afterwards is what fixes the final
 * evaluation order (see `draft.ts`'s `planSave`). */
export async function createRule(
  pairId: string,
  payload: SelectionRuleCreateRequest,
): Promise<ApiResult<SelectionRuleOut>> {
  try {
    const { data, error } = await apiClient.POST("/api/pairs/{pair_id}/rules", {
      params: { path: { pair_id: pairId } },
      body: payload,
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `PATCH /api/pairs/{pair_id}/rules/{rule_id}` -- decision / matcher kind / pattern only.
 * Position is never an implicit side effect of a field edit; that is `reorderRules`'s job. */
export async function updateRule(
  pairId: string,
  ruleId: string,
  payload: SelectionRuleUpdateRequest,
): Promise<ApiResult<SelectionRuleOut>> {
  try {
    const { data, error } = await apiClient.PATCH("/api/pairs/{pair_id}/rules/{rule_id}", {
      params: { path: { pair_id: pairId, rule_id: ruleId } },
      body: payload,
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `DELETE /api/pairs/{pair_id}/rules/{rule_id}` -- 204, no body. */
export async function deleteRule(pairId: string, ruleId: string): Promise<ApiResult<void>> {
  try {
    const { error } = await apiClient.DELETE("/api/pairs/{pair_id}/rules/{rule_id}", {
      params: { path: { pair_id: pairId, rule_id: ruleId } },
    });
    if (!error) return ok(undefined);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `POST /api/pairs/{pair_id}/rules/reorder` -- the COMPLETE ordered list of one
 * `(pair, scope)`'s rule ids, never a "move rule X to index N" delta.
 *
 * `routes/selection.py`'s module docstring is explicit about why: the service's own contract
 * is "give me the complete new order or nothing changes", and `ordered_rule_ids` must name
 * exactly the existing ids for that `(pair, scope)`, each once, or the whole call is refused
 * with a 422. A client that sent a delta would be asking the route to do a read-then-write
 * splice it deliberately does not do. */
export async function reorderRules(
  pairId: string,
  scope: RuleScope,
  ruleIds: string[],
): Promise<ApiResult<SelectionRuleOut[]>> {
  try {
    const { data, error } = await apiClient.POST("/api/pairs/{pair_id}/rules/reorder", {
      params: { path: { pair_id: pairId } },
      body: { scope, rule_ids: ruleIds },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `GET /api/pairs/{pair_id}/overrides?scope=` -- one scope's per-object pins. */
export async function listOverrides(
  pairId: string,
  scope: RuleScope,
): Promise<ApiResult<SelectionOverrideOut[]>> {
  try {
    const { data, error } = await apiClient.GET("/api/pairs/{pair_id}/overrides", {
      params: { path: { pair_id: pairId }, query: { scope } },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `POST /api/pairs/{pair_id}/overrides` -- pin one object's decision.
 *
 * `object_id` MUST be the object's literal qualified name (`catalog.schema` for object scope,
 * `catalog.schema.table` for dataset scope), never a connector's opaque stable id. This is not
 * a stylistic preference: `tests/selection/test_preview_sync_agreement.py` holds the exact
 * divergence it prevents -- the sync loop derives a dataset's parent schema purely from the
 * dataset's own qualified name (a lone dataset change carries no reference to its schema's
 * stable id), so a pin on a schema's opaque id is honoured by the console's preview and
 * silently invisible to the run, which is the console promising a sync that never happens.
 * The API refuses the wrong form with a 422; this screen never offers it (see
 * `overridePinTarget` in `overrides.ts`). */
export async function createOverride(
  pairId: string,
  payload: SelectionOverrideCreateRequest,
): Promise<ApiResult<SelectionOverrideOut>> {
  try {
    const { data, error } = await apiClient.POST("/api/pairs/{pair_id}/overrides", {
      params: { path: { pair_id: pairId } },
      body: payload,
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `PATCH /api/pairs/{pair_id}/overrides/{override_id}` -- flip an existing pin's decision.
 * `scope`/`object_id` are the pin's identity and are not editable; moving a pin to another
 * object is a delete-and-recreate. */
export async function updateOverride(
  pairId: string,
  overrideId: string,
  decision: SelectionDecision,
): Promise<ApiResult<SelectionOverrideOut>> {
  try {
    const { data, error } = await apiClient.PATCH("/api/pairs/{pair_id}/overrides/{override_id}", {
      params: { path: { pair_id: pairId, override_id: overrideId } },
      body: { decision },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `DELETE /api/pairs/{pair_id}/overrides/{override_id}` -- unpin. 204, no body. */
export async function deleteOverride(
  pairId: string,
  overrideId: string,
): Promise<ApiResult<void>> {
  try {
    const { error } = await apiClient.DELETE("/api/pairs/{pair_id}/overrides/{override_id}", {
      params: { path: { pair_id: pairId, override_id: overrideId } },
    });
    if (!error) return ok(undefined);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

export interface SourceTreeQuery {
  scope: RuleScope;
  offset: number;
  limit: number;
  resolveTags: boolean;
  resolveOwners: boolean;
}

/** `GET /api/pairs/{pair_id}/source-tree` -- ONE page of the pair's live source, each node
 * already carrying its decision and the deciding rule, evaluated against the pair's **stored**
 * rules and overrides (a draft is never browsable; that is the preview route's job).
 *
 * OFFSET pagination -- `{nodes, offset, limit, has_more, next_offset}` -- read off
 * `openapi.json`. `next_offset` is `null` exactly when there is nothing more. */
export async function browseSourceTree(
  pairId: string,
  query: SourceTreeQuery,
): Promise<ApiResult<SourceTreePageOut>> {
  try {
    const { data, error } = await apiClient.GET("/api/pairs/{pair_id}/source-tree", {
      params: {
        path: { pair_id: pairId },
        query: {
          scope: query.scope,
          offset: query.offset,
          limit: query.limit,
          resolve_tags: query.resolveTags,
          resolve_owners: query.resolveOwners,
        },
      },
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}

/** `POST /api/pairs/{pair_id}/preview` -- counts plus a bounded sample.
 *
 * `body.rules` left OUT previews the pair's stored, saved rules; an explicit array previews
 * that unsaved draft instead, and `PreviewOut.rule_set_source` echoes which one was evaluated
 * so the console can never present a draft's numbers as the saved configuration's. Overrides
 * are never draftable -- they always come from storage either way. */
export async function runPreview(
  pairId: string,
  body: PreviewRequest,
): Promise<ApiResult<PreviewOut>> {
  try {
    const { data, error } = await apiClient.POST("/api/pairs/{pair_id}/preview", {
      params: { path: { pair_id: pairId } },
      body,
    });
    if (data) return ok(data);
    return fail(error);
  } catch (caught) {
    return fail(caught);
  }
}
