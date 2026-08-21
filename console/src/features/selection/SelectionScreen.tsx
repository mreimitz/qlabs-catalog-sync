/* Selection (T13.5) -- the screen the console exists for: decide exactly which source objects a
 * pair syncs into Qlik, and see what that decision costs before anything is written.
 *
 * The one rule this screen is built around
 * ----------------------------------------
 *
 * C4: the sync loop and the preview call the same evaluator, so a preview that disagrees with
 * the run it predicts is impossible ON THE SERVER. This screen's job is to not reintroduce the
 * divergence on the client. So: no inclusion is computed here, no count is computed here, and no
 * "decided by rule X" is computed here. Every one of those arrives on a `SelectionResultOut`,
 * `DatasetSelectionOut` or `PreviewOut` and is rendered as given. The only client-side
 * derivations in this feature are (a) grouping datasets under their parent by the qualified NAME
 * the server put on them, because the API has no per-schema dataset query, and (b) reading the
 * source's capability manifest to decide which matcher kinds a rule may use -- neither of which
 * is a decision about any object. Both are called out where they happen.
 *
 * Three rule sets, and never confusing them
 * -----------------------------------------
 *
 *  * The **saved** rules -- what the next run will use. The source tree always shows these; the
 *    browse route has no draft mode.
 *  * The **draft** -- what the operator is editing. Previewable (`PreviewRequest.rules`),
 *    discardable, and not visible in the tree until it is saved. `PreviewOut.rule_set_source`
 *    labels every set of numbers as one or the other.
 *  * **Overrides** -- never drafted at all. The preview route always joins the pair's stored
 *    overrides, so pinning an object takes effect immediately and both the tree and the preview
 *    are refreshed the moment one changes.
 *
 * Staying responsive over a large catalog
 * ---------------------------------------
 *
 *  * The tree is paged, not loaded: one bounded page of schemas at a time, and the source's table
 *    stream is not touched at all until a schema is actually expanded.
 *  * Exactly one page request per stream can be in flight; expanding five schemas in a row
 *    cannot become five requests.
 *  * The preview is debounced, and every response carries a sequence number so a slow earlier
 *    response can never overwrite a newer one. While a newer draft is pending, the figures on
 *    screen stay visible but are explicitly labelled as being recalculated -- never silently
 *    presented as current.
 *  * `resolve_tags`/`resolve_owners` cost one extra source `read()` per node, so they are sent
 *    only when the rule set being evaluated actually contains a tag or owner rule -- and the
 *    tree is sent the flags derived from the SAVED rules while the preview is sent the flags
 *    derived from the set it is evaluating, so each one resolves exactly what it needs.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  AlertDescription,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FieldRow,
  SectionHeader,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  StatePanel,
  Text,
  TooltipProvider,
  toast,
} from "@elabs-ai/components-ui";

import {
  browseSourceTree,
  createOverride,
  deleteOverride,
  listOverrides,
  listPairs,
  listRules,
  readSourceManifest,
  runPreview,
  updateOverride,
  type DraftSelectionRuleIn,
  type EndpointManifestOut,
  type PreviewOut,
  type PreviewRequest,
  type PreviewSampleItemOut,
  type RuleScope,
  type SchemaNodeOut,
  type DatasetNodeOut,
  type SelectionDecision,
  type SelectionOverrideOut,
  type SyncPairOut,
} from "./selectionApi";
import {
  EMPTY_DRAFT,
  EMPTY_STORED,
  draftFromStored,
  executeSave,
  isDirty,
  planSave,
  toPreviewRules,
  type DraftRule,
  type DraftRules,
  type StoredRules,
} from "./draft";
import { RULE_SCOPES, SCOPE_LABEL } from "./labels";
import { matcherSupportFor } from "./matcherSupport";
import { overrideIndex, overridePinTarget } from "./overrides";
import { storedRulePositions } from "./sourceTree";
import { locateStoredRuleId } from "./ruleRefs";
import { RuleEditor } from "./RuleEditor";
import { PreviewPanel, type PreviewState } from "./PreviewPanel";
import {
  EMPTY_TREE_STATE,
  SourceTreePanel,
  datasetNodeId,
  schemaNodeId,
  type SourceTreeState,
} from "./SourceTreePanel";

/** One bounded page of either stream. The server's own default, and its ceiling is 1000. */
const PAGE_SIZE = 200;
/** How much of a stream a refresh re-reads in one request, at most. */
const MAX_REFRESH_LIMIT = 1000;
/** How long a rule edit has to settle before the preview is asked again. Long enough that
 * dragging a rule through four positions is one request, short enough to feel live. */
const PREVIEW_DEBOUNCE_MS = 400;
/** Enough example rows to be useful, small enough that the response stays small. */
const PREVIEW_SAMPLE_LIMIT = 25;
/** The route's own default ceiling on candidates examined before it reports `truncated`. */
const PREVIEW_MAX_CANDIDATES = 20_000;

type OverridesByScope = Record<RuleScope, SelectionOverrideOut[]>;

const EMPTY_OVERRIDES: OverridesByScope = { object: [], dataset: [] };

/** Whether the rule set about to be evaluated needs the source's tags / owners read.
 *
 * `resolve_tags`/`resolve_owners` default to false and cost one extra `read()` per node when
 * true (`routes/preview.py`), so they are never sent speculatively. But WITHOUT them a tag rule
 * is undetermined for every candidate, which is a preview full of "cannot tell" that says
 * nothing -- so they are sent exactly when the set being evaluated contains such a rule. */
function resolveFlagsFor(matchers: readonly string[]): { tags: boolean; owners: boolean } {
  return { tags: matchers.includes("tag"), owners: matchers.includes("owner") };
}

export function SelectionScreen() {
  const [pairsLoad, setPairsLoad] = useState<
    { status: "loading" } | { status: "error"; message: string } | { status: "loaded" }
  >({ status: "loading" });
  const [pairs, setPairs] = useState<SyncPairOut[]>([]);
  const [pairId, setPairId] = useState<string | null>(null);

  const [scope, setScope] = useState<RuleScope>("object");
  const [stored, setStored] = useState<StoredRules>(EMPTY_STORED);
  const [draft, setDraft] = useState<DraftRules>(EMPTY_DRAFT);
  const [overrides, setOverrides] = useState<OverridesByScope>(EMPTY_OVERRIDES);
  const [manifest, setManifest] = useState<EndpointManifestOut | null>(null);
  const [configLoad, setConfigLoad] = useState<
    { status: "idle" } | { status: "loading" } | { status: "error"; message: string } | { status: "loaded" }
  >({ status: "idle" });

  const [tree, setTree] = useState<SourceTreeState>(EMPTY_TREE_STATE);
  const [expandedIds, setExpandedIds] = useState<string[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  const [previewState, setPreviewState] = useState<PreviewState>({ status: "idle" });
  const [lastPreview, setLastPreview] = useState<PreviewOut | null>(null);
  const [lastPreviewKey, setLastPreviewKey] = useState<string | null>(null);
  const [previewNonce, setPreviewNonce] = useState(0);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [patternErrors, setPatternErrors] = useState<Record<string, string>>({});
  const [highlightKey, setHighlightKey] = useState<string | null>(null);
  const [pinBusy, setPinBusy] = useState(false);

  // Guards state updates against firing after unmount -- same reason
  // `../pairs/PairsScreen.tsx` keeps one: this screen fetches automatically.
  const mountedRef = useRef(true);
  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  const previewSeqRef = useRef(0);
  const schemaLoadingRef = useRef(false);
  const datasetLoadingRef = useRef(false);
  // Mirrors `tree` so a refresh triggered from an event handler can read how much of each
  // stream is currently loaded without performing a side effect inside a state updater (React
  // may invoke an updater more than once).
  const treeRef = useRef<SourceTreeState>(EMPTY_TREE_STATE);
  useEffect(() => {
    treeRef.current = tree;
  }, [tree]);

  const pair = useMemo(() => pairs.find((row) => row.id === pairId) ?? null, [pairs, pairId]);
  const dirty = useMemo(() => isDirty(draft, stored), [draft, stored]);
  const rulePositions = useMemo(() => storedRulePositions(stored), [stored]);
  const overridesIndexed = useMemo(
    () => ({ object: overrideIndex(overrides.object), dataset: overrideIndex(overrides.dataset) }),
    [overrides],
  );
  const support = useMemo(
    () => matcherSupportFor(manifest, scope, pair?.source ?? ""),
    [manifest, scope, pair],
  );

  const storedResolveFlags = useMemo(
    () => resolveFlagsFor(RULE_SCOPES.flatMap((s) => stored[s].map((rule) => rule.matcher_kind))),
    [stored],
  );

  const draftPreviewRules = useMemo<DraftSelectionRuleIn[]>(() => toPreviewRules(draft), [draft]);
  const incompleteRuleCount = useMemo(
    () => draftPreviewRules.filter((rule) => rule.pattern.trim() === "").length,
    [draftPreviewRules],
  );
  /** Identifies the exact rule set the preview should describe, so a landed preview can be told
   * apart from the edit that has since replaced it. */
  const previewKey = useMemo(
    () => JSON.stringify({ dirty, rules: dirty ? draftPreviewRules : "stored", previewNonce }),
    [dirty, draftPreviewRules, previewNonce],
  );

  // ---- initial load -------------------------------------------------------------------

  const fetchPairs = useCallback(async () => {
    setPairsLoad({ status: "loading" });
    const result = await listPairs();
    if (!mountedRef.current) return;
    if (!result.ok) {
      setPairsLoad({ status: "error", message: result.error.message });
      return;
    }
    setPairs(result.data);
    setPairsLoad({ status: "loaded" });
  }, []);

  useEffect(() => {
    void fetchPairs();
  }, [fetchPairs]);

  // ---- per-pair configuration ---------------------------------------------------------

  const loadSchemaPage = useCallback(
    async (id: string, offset: number, limit: number, resolve: { tags: boolean; owners: boolean }) => {
      if (schemaLoadingRef.current) return;
      schemaLoadingRef.current = true;
      setTree((prev) => ({ ...prev, schemaLoading: true, status: prev.status === "idle" ? "loading" : prev.status }));
      const result = await browseSourceTree(id, {
        scope: "object",
        offset,
        limit,
        resolveTags: resolve.tags,
        resolveOwners: resolve.owners,
      });
      schemaLoadingRef.current = false;
      if (!mountedRef.current) return;
      if (!result.ok) {
        setTree((prev) => ({ ...prev, schemaLoading: false, status: "error", message: result.error.message }));
        return;
      }
      const page = result.data;
      setTree((prev) => ({
        ...prev,
        status: "loaded",
        message: null,
        schemaLoading: false,
        schemas:
          offset === 0
            ? page.nodes.filter((node): node is SchemaNodeOut => node.scope === "object")
            : [
                ...prev.schemas,
                ...page.nodes.filter((node): node is SchemaNodeOut => node.scope === "object"),
              ],
        schemaHasMore: page.has_more,
      }));
    },
    [],
  );

  const loadDatasetPage = useCallback(
    async (id: string, offset: number, limit: number, resolve: { tags: boolean; owners: boolean }) => {
      if (datasetLoadingRef.current) return;
      datasetLoadingRef.current = true;
      setTree((prev) => ({ ...prev, datasetLoading: true, datasetsRequested: true }));
      const result = await browseSourceTree(id, {
        scope: "dataset",
        offset,
        limit,
        resolveTags: resolve.tags,
        resolveOwners: resolve.owners,
      });
      datasetLoadingRef.current = false;
      if (!mountedRef.current) return;
      if (!result.ok) {
        setTree((prev) => ({ ...prev, datasetLoading: false, status: "error", message: result.error.message }));
        return;
      }
      const page = result.data;
      setTree((prev) => ({
        ...prev,
        datasetLoading: false,
        datasets:
          offset === 0
            ? page.nodes.filter((node): node is DatasetNodeOut => node.scope === "dataset")
            : [
                ...prev.datasets,
                ...page.nodes.filter((node): node is DatasetNodeOut => node.scope === "dataset"),
              ],
        datasetHasMore: page.has_more,
      }));
    },
    [],
  );

  const loadConfiguration = useCallback(
    async (id: string, sourceEndpoint: string) => {
      setConfigLoad({ status: "loading" });
      const [objectRules, datasetRules, objectOverrides, datasetOverrides, manifestResult] =
        await Promise.all([
          listRules(id, "object"),
          listRules(id, "dataset"),
          listOverrides(id, "object"),
          listOverrides(id, "dataset"),
          readSourceManifest(sourceEndpoint),
        ]);
      if (!mountedRef.current) return;

      for (const result of [objectRules, datasetRules, objectOverrides, datasetOverrides]) {
        if (!result.ok) {
          setConfigLoad({ status: "error", message: result.error.message });
          return;
        }
      }
      if (!objectRules.ok || !datasetRules.ok || !objectOverrides.ok || !datasetOverrides.ok) return;

      const nextStored: StoredRules = { object: objectRules.data, dataset: datasetRules.data };
      setStored(nextStored);
      setDraft(draftFromStored(nextStored));
      setOverrides({ object: objectOverrides.data, dataset: datasetOverrides.data });
      // A manifest this endpoint would not produce is not a load failure -- the rest of the
      // screen works fine without it, and `matcherSupport.ts` renders that as "unknown" rather
      // than as "the source cannot do this".
      setManifest(manifestResult.ok ? manifestResult.data : null);
      setConfigLoad({ status: "loaded" });

      const resolve = resolveFlagsFor(
        [...objectRules.data, ...datasetRules.data].map((rule) => rule.matcher_kind),
      );
      void loadSchemaPage(id, 0, PAGE_SIZE, resolve);
    },
    [loadSchemaPage],
  );

  function selectPair(nextId: string) {
    const next = pairs.find((row) => row.id === nextId);
    if (next === undefined) return;
    setPairId(nextId);
    setScope("object");
    setStored(EMPTY_STORED);
    setDraft(EMPTY_DRAFT);
    setOverrides(EMPTY_OVERRIDES);
    setManifest(null);
    setTree(EMPTY_TREE_STATE);
    setExpandedIds([]);
    setSelectedNodeId(null);
    setPreviewState({ status: "idle" });
    setLastPreview(null);
    setLastPreviewKey(null);
    setSaveError(null);
    setPatternErrors({});
    setHighlightKey(null);
    schemaLoadingRef.current = false;
    datasetLoadingRef.current = false;
    void loadConfiguration(nextId, next.source);
  }

  // ---- preview ------------------------------------------------------------------------

  const requestPreview = useCallback(async () => {
    if (pairId === null || configLoad.status !== "loaded") return;
    if (incompleteRuleCount > 0) {
      setPreviewState({
        status: "paused",
        reason: `Preview paused: ${incompleteRuleCount} rule(s) still need a pattern. The numbers below describe the last rule set that could be evaluated, not the one in the editor.`,
      });
      return;
    }

    const seq = previewSeqRef.current + 1;
    previewSeqRef.current = seq;
    const key = previewKey;
    setPreviewState({ status: "loading" });

    const rules = dirty ? draftPreviewRules : null;
    // Derived from the set actually being evaluated -- the draft when one is being previewed,
    // the saved rules otherwise -- so the preview resolves exactly the facts its own rules need.
    const evaluated: readonly { matcher_kind: string }[] =
      rules ?? RULE_SCOPES.flatMap((s) => stored[s]);
    const resolve = resolveFlagsFor(evaluated.map((rule) => rule.matcher_kind));
    // `rules` is OMITTED, not sent as null, when previewing the stored set: that is the
    // documented way to ask for the saved configuration, and it keeps the request honest about
    // which of the two it is asking for.
    const body: PreviewRequest = {
      resolve_tags: resolve.tags,
      resolve_owners: resolve.owners,
      sample_limit: PREVIEW_SAMPLE_LIMIT,
      max_candidates: PREVIEW_MAX_CANDIDATES,
      ...(rules === null ? {} : { rules }),
    };

    const result = await runPreview(pairId, body);
    if (!mountedRef.current) return;
    // A slower earlier request must never overwrite a newer answer.
    if (previewSeqRef.current !== seq) return;
    if (!result.ok) {
      setPreviewState({ status: "error", message: result.error.message });
      return;
    }
    setPreviewState({ status: "loaded", preview: result.data });
    setLastPreview(result.data);
    setLastPreviewKey(key);
  }, [pairId, configLoad.status, incompleteRuleCount, previewKey, dirty, draftPreviewRules, stored]);

  useEffect(() => {
    if (pairId === null || configLoad.status !== "loaded") return;
    const timer = setTimeout(() => {
      void requestPreview();
    }, PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [pairId, configLoad.status, previewKey, requestPreview]);

  // ---- tree paging --------------------------------------------------------------------

  function handleExpandedChange(ids: string[]) {
    setExpandedIds(ids);
    // The source's table stream is not read at all until a schema is actually expanded -- and
    // only one page request can be in flight, so expanding several schemas in a row is still one
    // request. See `sourceTree.ts` for why a per-schema fetch does not exist.
    if (
      pairId !== null &&
      ids.length > 0 &&
      !tree.datasetsRequested &&
      !datasetLoadingRef.current
    ) {
      void loadDatasetPage(pairId, 0, PAGE_SIZE, storedResolveFlags);
    }
  }

  function handleLoadMoreSchemas() {
    if (pairId === null) return;
    void loadSchemaPage(pairId, tree.schemas.length, PAGE_SIZE, storedResolveFlags);
  }

  function handleLoadMoreDatasets() {
    if (pairId === null) return;
    void loadDatasetPage(pairId, tree.datasets.length, PAGE_SIZE, storedResolveFlags);
  }

  const refreshTree = useCallback(
    (id: string, resolve: { tags: boolean; owners: boolean }, current: SourceTreeState) => {
      const schemaLimit = Math.min(MAX_REFRESH_LIMIT, Math.max(PAGE_SIZE, current.schemas.length));
      void loadSchemaPage(id, 0, schemaLimit, resolve);
      if (current.datasetsRequested) {
        const datasetLimit = Math.min(
          MAX_REFRESH_LIMIT,
          Math.max(PAGE_SIZE, current.datasets.length),
        );
        void loadDatasetPage(id, 0, datasetLimit, resolve);
      }
    },
    [loadSchemaPage, loadDatasetPage],
  );

  // ---- saving -------------------------------------------------------------------------

  async function handleSave() {
    if (pairId === null) return;
    setSaving(true);
    setSaveError(null);
    setPatternErrors({});

    const outcome = await executeSave(pairId, planSave(draft, stored));
    const [objectRules, datasetRules] = await Promise.all([
      listRules(pairId, "object"),
      listRules(pairId, "dataset"),
    ]);
    if (!mountedRef.current) return;

    const refreshed: StoredRules = {
      object: objectRules.ok ? objectRules.data : stored.object,
      dataset: datasetRules.ok ? datasetRules.data : stored.dataset,
    };
    setStored(refreshed);

    if (outcome.ok) {
      // Re-seeding the draft is what gives newly created rules their real ids.
      setDraft(draftFromStored(refreshed));
      setHighlightKey(null);
      toast.success("Selection rules saved. The source tree and the preview now show them.");
    } else {
      // The draft is deliberately NOT reset on failure: the operator's edit is the thing they
      // still need. `stored` is updated to the server's truth so the next save plan is computed
      // against reality rather than against what this screen hoped had happened.
      setSaveError(
        `${outcome.error.message} (while ${outcome.step})${outcome.partiallyApplied ? ". Some earlier changes in this save were already written -- the saved rules shown here have been re-read from the server." : ""}`,
      );
      if (outcome.ruleKey !== null && outcome.error.field === "pattern") {
        setPatternErrors({ [outcome.ruleKey]: outcome.error.message });
      }
    }

    const resolve = resolveFlagsFor(
      RULE_SCOPES.flatMap((s) => refreshed[s].map((rule) => rule.matcher_kind)),
    );
    refreshTree(pairId, resolve, treeRef.current);
    setPreviewNonce((value) => value + 1);
    setSaving(false);
  }

  function handleDiscard() {
    setDraft(draftFromStored(stored));
    setSaveError(null);
    setPatternErrors({});
    setHighlightKey(null);
  }

  // ---- overrides ----------------------------------------------------------------------

  async function reloadOverrides(id: string, forScope: RuleScope) {
    const result = await listOverrides(id, forScope);
    if (!mountedRef.current || !result.ok) return;
    setOverrides((prev) => ({ ...prev, [forScope]: result.data }));
  }

  async function handlePin(
    node: SchemaNodeOut | DatasetNodeOut,
    decision: SelectionDecision,
  ) {
    if (pairId === null) return;
    const target = overridePinTarget(node);
    // Unreachable through the UI (the control is disabled), and refused by the API anyway --
    // kept so this function can never be the place an opaque id leaks into an override.
    if (!target.pinnable) return;

    setPinBusy(true);
    const existing = overrides[target.scope].find((row) => row.object_id === target.objectId);
    const result = existing
      ? await updateOverride(pairId, existing.id, decision)
      : await createOverride(pairId, {
          scope: target.scope,
          object_id: target.objectId,
          decision,
        });
    if (!mountedRef.current) return;
    setPinBusy(false);

    if (!result.ok) {
      toast.error(`Could not pin "${target.objectId}": ${result.error.message}`);
      return;
    }
    toast.success(
      `"${target.objectId}" is pinned to ${decision}. An override beats every rule, and it is saved immediately.`,
    );
    await reloadOverrides(pairId, target.scope);
    refreshTree(pairId, storedResolveFlags, treeRef.current);
    setPreviewNonce((value) => value + 1);
  }

  async function handleUnpin(override: SelectionOverrideOut) {
    if (pairId === null) return;
    setPinBusy(true);
    const result = await deleteOverride(pairId, override.id);
    if (!mountedRef.current) return;
    setPinBusy(false);
    if (!result.ok) {
      toast.error(`Could not remove the pin on "${override.object_id}": ${result.error.message}`);
      return;
    }
    toast.success(`The pin on "${override.object_id}" is removed. Rules decide it again.`);
    await reloadOverrides(pairId, override.scope);
    refreshTree(pairId, storedResolveFlags, treeRef.current);
    setPreviewNonce((value) => value + 1);
  }

  // ---- tracing a node back to its rule -------------------------------------------------

  function handleShowDecidingRule(ruleId: string) {
    const located = locateStoredRuleId(draft, ruleId);
    if (located === null) {
      toast.error(
        "That rule decided this object in the SAVED configuration, but the unsaved draft no longer contains it. Discard the draft to see it.",
      );
      return;
    }
    setScope(located.scope);
    setHighlightKey(located.key);
  }

  function handleSelectSample(item: PreviewSampleItemOut) {
    // The sample names an object by its own identifiers; select the matching tree node when it
    // has already been read, so the operator lands on the full decision detail.
    if (item.scope === "object") {
      const found = tree.schemas.find((node) => node.object_id === item.object_id);
      if (found !== undefined) {
        setSelectedNodeId(schemaNodeId(found));
        return;
      }
    } else {
      const found = tree.datasets.find((node) => node.object_id === item.object_id);
      if (found !== undefined) {
        setSelectedNodeId(datasetNodeId(found));
        return;
      }
    }
    toast.error(
      `"${item.qualified_name ?? item.object_id}" has not been read into the tree yet. Read more of the source to inspect it.`,
    );
  }

  // ---- render -------------------------------------------------------------------------

  const stale = lastPreviewKey !== null && lastPreviewKey !== previewKey;

  return (
    <TooltipProvider>
      <div className="flex flex-col gap-6">
        <SectionHeader
          title="Selection"
          description="Which source objects a pair syncs into Qlik. Rules are evaluated top to bottom and the last matching rule decides; a per-object override beats every rule. Nothing here writes anything to Qlik."
        />

        {pairsLoad.status === "error" ? (
          <StatePanel
            kind="error"
            title="Could not load sync pairs"
            description={pairsLoad.message}
            actions={<Button onClick={() => void fetchPairs()}>Retry</Button>}
          />
        ) : null}

        <Select value={pairId ?? undefined} onValueChange={selectPair}>
          <FieldRow
            label="Sync pair"
            description="Choosing a pair reads its source endpoint — the same live catalog a run would read. Nothing is written."
          >
            <SelectTrigger disabled={pairsLoad.status !== "loaded" || pairs.length === 0}>
              <SelectValue
                placeholder={pairs.length === 0 ? "No sync pairs configured yet" : "Select a sync pair"}
              />
            </SelectTrigger>
          </FieldRow>
          <SelectContent>
            {pairs.map((row) => (
              <SelectItem key={row.id} value={row.id}>
                {row.name} ({row.source} → {row.target})
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {pairsLoad.status === "loaded" && pairs.length === 0 ? (
          <StatePanel
            kind="empty"
            title="No sync pairs configured yet"
            description="Selection rules belong to one sync pair. Create a pair on the Sync pairs screen first."
          />
        ) : null}

        {pair === null ? null : configLoad.status === "error" ? (
          <StatePanel
            kind="error"
            title="Could not load this pair's selection configuration"
            description={configLoad.message}
            actions={<Button onClick={() => void loadConfiguration(pair.id, pair.source)}>Retry</Button>}
          />
        ) : configLoad.status === "loading" ? (
          <StatePanel kind="loading" title="Loading this pair's rules, overrides and source…" />
        ) : configLoad.status === "loaded" ? (
          <>
            <Card>
              <CardHeader>
                <CardTitle>What these rules would select</CardTitle>
                <CardDescription>
                  Evaluated by the engine&apos;s own evaluator against {pair.source} — the same code
                  path the real run uses, so a preview that disagrees with its run is not possible.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <PreviewPanel
                  state={previewState}
                  lastPreview={lastPreview}
                  stale={stale}
                  onRetry={() => void requestPreview()}
                  onSelectSample={handleSelectSample}
                />
              </CardContent>
            </Card>

            <div className="grid gap-6 xl:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle>Rules</CardTitle>
                  <CardDescription>
                    {SCOPE_LABEL[scope]} — shown in evaluation order, top to bottom.
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  {saveError ? (
                    <Alert variant="destructive" className="mb-4">
                      <AlertDescription>{saveError}</AlertDescription>
                    </Alert>
                  ) : null}
                  <RuleEditor
                    scope={scope}
                    onScopeChange={setScope}
                    rules={draft[scope]}
                    onRulesChange={(next: DraftRule[]) =>
                      setDraft((prev) => ({ ...prev, [scope]: next }))
                    }
                    support={support}
                    dirty={dirty}
                    saving={saving}
                    onSave={() => void handleSave()}
                    onDiscard={handleDiscard}
                    highlightKey={highlightKey}
                    patternErrors={patternErrors}
                  />
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Source tree</CardTitle>
                  <CardDescription>
                    Every node carries the decision the saved rules produce for it, and the reason.
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <SourceTreePanel
                    state={tree}
                    rulePositions={rulePositions}
                    overrides={overridesIndexed}
                    expandedIds={expandedIds}
                    onExpandedChange={handleExpandedChange}
                    selectedId={selectedNodeId}
                    onSelectedChange={setSelectedNodeId}
                    onLoadMoreSchemas={handleLoadMoreSchemas}
                    onLoadMoreDatasets={handleLoadMoreDatasets}
                    onPin={(node, decision) => void handlePin(node, decision)}
                    onUnpin={(override) => void handleUnpin(override)}
                    onShowDecidingRule={handleShowDecidingRule}
                    pinBusy={pinBusy}
                    draftDirty={dirty}
                    resolvingTags={storedResolveFlags.tags}
                    resolvingOwners={storedResolveFlags.owners}
                    onRetry={() =>
                      void loadSchemaPage(pair.id, 0, PAGE_SIZE, storedResolveFlags)
                    }
                  />
                </CardContent>
              </Card>
            </div>
          </>
        ) : (
          <Text variant="caption" tone="muted">
            Select a sync pair above to edit its selection rules.
          </Text>
        )}
      </div>
    </TooltipProvider>
  );
}

export default SelectionScreen;
