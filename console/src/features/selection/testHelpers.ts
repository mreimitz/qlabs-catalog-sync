// Shared fixtures + this feature's request router. Not a `*.test.*` file itself, so vitest never
// collects it as a suite. Mirrors `../pairs/testHelpers.ts` exactly, and for the same reasons:
//
//  * every fixture is built from the REAL shapes in `../../api/generated/schema.ts`, so a fixture
//    that drifts from the API is a compile error rather than a silent divergence -- this feature
//    in particular has three response shapes (`SelectionResultOut`, `DatasetSelectionOut`,
//    `SourceTreePageOut`) whose fields are the whole point of the screen;
//  * every test drives the REAL `apiClient` through a stubbed `globalThis.fetch`, never a mock of
//    `apiClient` or of this feature's own modules, so what a test asserts is what would go over
//    the wire;
//  * the router answers by `"METHOD pathname"` rather than by call order, because this screen
//    issues five requests in one `Promise.all` on pair selection and more as the tree pages --
//    call-order queueing would silently hand one component another's response.
//
// One deviation worth stating: `../pairs/testHelpers.ts`'s router keys on pathname only, which
// cannot tell `GET /rules?scope=object` from `GET /rules?scope=dataset` -- and this feature
// fetches both. So this router lets a route be keyed by `"METHOD pathname?query"` as well, and
// records the full `method pathname?query` of every request in `calls`, which is also what the
// "reorder sends the complete ordered id list" and "the tree is not read until a schema is
// expanded" assertions need.
import { jsonResponse } from "../../test/apiFixtures";
import type { components } from "../../api/generated/schema";

export type SyncPairOut = components["schemas"]["SyncPairOut"];
export type SelectionRuleOut = components["schemas"]["SelectionRuleOut"];
export type SelectionOverrideOut = components["schemas"]["SelectionOverrideOut"];
export type SelectionResultOut = components["schemas"]["SelectionResultOut"];
export type UndeterminedRuleOut = components["schemas"]["UndeterminedRuleOut"];
export type SchemaNodeOut = components["schemas"]["SchemaNodeOut"];
export type DatasetNodeOut = components["schemas"]["DatasetNodeOut"];
export type DatasetSelectionOut = components["schemas"]["DatasetSelectionOut"];
export type SourceTreePageOut = components["schemas"]["SourceTreePageOut"];
export type PreviewOut = components["schemas"]["PreviewOut"];
export type ScopeCountsOut = components["schemas"]["ScopeCountsOut"];
export type PreviewSampleItemOut = components["schemas"]["PreviewSampleItemOut"];
export type EndpointManifestOut = components["schemas"]["EndpointManifestOut"];
export type EntityCapabilityOut = components["schemas"]["EntityCapabilityOut"];
export type FieldCapabilityOut = components["schemas"]["FieldCapabilityOut"];
export type ErrorModel = components["schemas"]["ErrorModel"];

export const PAIR_ID = "11111111-1111-1111-1111-111111111111";

export function syncPairOutFixture(overrides: Partial<SyncPairOut> = {}): SyncPairOut {
  return {
    id: PAIR_ID,
    name: "prod_databricks_to_qlik",
    source: "databricks_prod",
    target: "qlik_prod",
    target_space: "analytics/finance",
    entity_types: ["data_product", "dataset"],
    cadence_seconds: 900,
    jitter_seconds: null,
    manual_edit_policy: { default: "source_wins" },
    activation_opt_in: false,
    enabled: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

export function ruleFixture(overrides: Partial<SelectionRuleOut> = {}): SelectionRuleOut {
  return {
    id: "aaaaaaaa-0000-0000-0000-000000000001",
    pair_id: PAIR_ID,
    ordinal: 0,
    scope: "object",
    decision: "include",
    matcher_kind: "glob",
    pattern: "analytics.*",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

export function overrideFixture(
  overrides: Partial<SelectionOverrideOut> = {},
): SelectionOverrideOut {
  return {
    id: "bbbbbbbb-0000-0000-0000-000000000001",
    pair_id: PAIR_ID,
    scope: "object",
    object_id: "analytics.prod_staging",
    decision: "include",
    reason: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

export function undeterminedFixture(
  overrides: Partial<UndeterminedRuleOut> = {},
): UndeterminedRuleOut {
  return {
    matcher_kind: "tag",
    pattern: "pii",
    decision: "exclude",
    missing: "tags",
    explain: "rule #1 exclude tag 'pii' could not be evaluated: tags unknown",
    ...overrides,
  };
}

export function resultFixture(overrides: Partial<SelectionResultOut> = {}): SelectionResultOut {
  return {
    decision: "include",
    included: true,
    source: "rule",
    rule_id: "aaaaaaaa-0000-0000-0000-000000000001",
    explain: "included by rule #0 include glob 'analytics.*'",
    undetermined: [],
    ...overrides,
  };
}

export function schemaNodeFixture(overrides: Partial<SchemaNodeOut> = {}): SchemaNodeOut {
  return {
    scope: "object",
    object_id: "schema-stable-id-1",
    qualified_name: "analytics.sales",
    display_name: "sales",
    result: resultFixture(),
    ...overrides,
  };
}

export function datasetSelectionFixture(
  overrides: Partial<DatasetSelectionOut> = {},
): DatasetSelectionOut {
  return {
    included: true,
    explain: "included by rule #0 include glob 'analytics.sales.*'",
    parent: resultFixture(),
    dataset: resultFixture({
      rule_id: "cccccccc-0000-0000-0000-000000000001",
      explain: "included by rule #0 include glob 'analytics.sales.*'",
    }),
    ...overrides,
  };
}

export function datasetNodeFixture(overrides: Partial<DatasetNodeOut> = {}): DatasetNodeOut {
  return {
    scope: "dataset",
    object_id: "table-stable-id-1",
    qualified_name: "analytics.sales.orders",
    display_name: "orders",
    selection: datasetSelectionFixture(),
    ...overrides,
  };
}

export function sourceTreePageFixture(
  overrides: Partial<SourceTreePageOut> = {},
): SourceTreePageOut {
  return {
    nodes: [],
    offset: 0,
    limit: 200,
    has_more: false,
    next_offset: null,
    ...overrides,
  };
}

export function countsFixture(overrides: Partial<ScopeCountsOut> = {}): ScopeCountsOut {
  return { total: 0, included: 0, excluded: 0, undetermined: 0, ...overrides };
}

export function previewFixture(overrides: Partial<PreviewOut> = {}): PreviewOut {
  return {
    rule_set_source: "stored",
    counts: { object: countsFixture(), dataset: countsFixture() },
    sample: [],
    candidates_examined: 0,
    truncated: false,
    ...overrides,
  };
}

export function sampleItemFixture(
  overrides: Partial<PreviewSampleItemOut> = {},
): PreviewSampleItemOut {
  return {
    scope: "object",
    object_id: "schema-stable-id-1",
    qualified_name: "analytics.sales",
    display_name: "sales",
    included: true,
    explain: "included by rule #0 include glob 'analytics.*'",
    rule_id: "aaaaaaaa-0000-0000-0000-000000000001",
    has_undetermined: false,
    ...overrides,
  };
}

export function fieldCapabilityFixture(
  overrides: Partial<FieldCapabilityOut> = {},
): FieldCapabilityOut {
  return {
    mode: "ro",
    normalized_by_target: false,
    partial_update: false,
    writable_via: null,
    ...overrides,
  };
}

export function entityCapabilityFixture(
  overrides: Partial<EntityCapabilityOut> = {},
): EntityCapabilityOut {
  return {
    supported: true,
    supports_events: false,
    identity_keys: ["native_key"],
    allowed_update_paths: null,
    max_update_operations: null,
    fields: {},
    ...overrides,
  };
}

/** A manifest whose `tags`/`owners` modes are stated per entity type -- exactly the two facts
 * `selection/source_tree.py`'s `tags_offered`/`owners_offered` read. `"absent"` omits the field
 * entirely, which is a different reason for the same answer and the UI says which. */
export function manifestFixture({
  endpoint = "databricks_prod",
  tags = "ro",
  owners = "ro",
  supported = true,
}: {
  endpoint?: string;
  tags?: FieldCapabilityOut["mode"] | "absent";
  owners?: FieldCapabilityOut["mode"] | "absent";
  supported?: boolean;
} = {}): EndpointManifestOut {
  const fields: Record<string, FieldCapabilityOut> = {};
  if (tags !== "absent") fields.tags = fieldCapabilityFixture({ mode: tags });
  if (owners !== "absent") fields.owners = fieldCapabilityFixture({ mode: owners });
  const entity = entityCapabilityFixture({ supported, fields });
  return {
    endpoint,
    manifest: {
      concurrency: "etag",
      entities: { data_product: entity, dataset: entity },
    },
    unavailable_reason: null,
  };
}

export function unreadableManifestFixture(
  endpoint = "databricks_prod",
  reason = "the workspace refused the request",
): EndpointManifestOut {
  return { endpoint, manifest: null, unavailable_reason: reason };
}

export function errorModelFixture(overrides: Partial<ErrorModel> = {}): ErrorModel {
  return {
    code: "selection_rule_pattern_invalid",
    message: "'analytics' must contain exactly 1 '.' separating 2 glob segments",
    field: "pattern",
    entity: null,
    correlation_id: null,
    ...overrides,
  };
}

export type RouteResponder = (request: Request) => Response | Promise<Response>;
export type Routes = Record<string, RouteResponder | Response>;

/** Routes a stubbed `globalThis.fetch` by `"METHOD pathname"`, falling back from the more
 * specific `"METHOD pathname?query"` key first so `?scope=object` and `?scope=dataset` can be
 * answered differently. Records the full keyed request in `calls`, including the query string,
 * so a test can assert that a route was -- or was NOT -- called, and with what.
 *
 * An unrouted request throws by name rather than returning `undefined`, so a missing fixture
 * fails loudly at the call site instead of as a mystery rejection somewhere else. */
export function installApiRouter(
  fetchMock: ReturnType<typeof import("vitest").vi.fn>,
  routes: Routes,
): { calls: string[]; requests: Request[] } {
  const calls: string[] = [];
  const requests: Request[] = [];
  fetchMock.mockImplementation(async (request: Request) => {
    const url = new URL(request.url);
    const withQuery = `${request.method} ${url.pathname}${url.search}`;
    const bare = `${request.method} ${url.pathname}`;
    calls.push(withQuery);
    requests.push(request);
    const route = routes[withQuery] ?? routes[bare];
    if (route === undefined) {
      throw new Error(`Unrouted request in test: ${withQuery}. Add it to installApiRouter.`);
    }
    // A `Response` body reads once -- clone a static fixture so one route can answer repeats.
    return typeof route === "function" ? route(request) : route.clone();
  });
  return { calls, requests };
}

/** The JSON body a recorded request would actually have sent. */
export async function bodyOf(request: Request): Promise<unknown> {
  return JSON.parse(await request.clone().text()) as unknown;
}

/** The last recorded request matching `"METHOD pathname"`, ignoring its query string. */
export function lastRequestTo(requests: readonly Request[], key: string): Request {
  for (let index = requests.length - 1; index >= 0; index -= 1) {
    const request = requests[index];
    if (request === undefined) continue;
    const url = new URL(request.url);
    if (`${request.method} ${url.pathname}` === key) return request;
  }
  throw new Error(`No request matching ${key}. Saw: ${requests.map((r) => r.url).join(", ")}`);
}

export { jsonResponse };
