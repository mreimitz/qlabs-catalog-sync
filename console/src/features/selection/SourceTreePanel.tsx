/* The source tree: browse the pair's live source, with every node already carrying the engine's
 * own verdict and the reason for it.
 *
 * What this panel shows, precisely
 * --------------------------------
 *
 * `GET /pairs/{id}/source-tree` always evaluates the pair's **stored, saved** rules and
 * overrides -- "what the next real sync would see right now". It is not, and cannot be, a view
 * of the unsaved draft; that is what the preview is for. While a draft is dirty the two are
 * therefore describing different rule sets on purpose, and this panel says so in a banner
 * rather than letting the operator assume the tree moved when they dragged a rule.
 *
 * Every decision is the server's
 * ------------------------------
 *
 * `included`, the decision source, the deciding rule id, the undetermined list and the one-line
 * explanation all arrive on `SelectionResultOut` / `DatasetSelectionOut`. Nothing here
 * re-evaluates a rule against a node, and the explanation rendered is the engine's own
 * `explain` string verbatim -- the same sentence a run report carries for the same object --
 * rather than a second sentence assembled from the parts, which could drift from it.
 *
 * A dataset's answer has two halves, and both are shown
 * -----------------------------------------------------
 *
 * C5: a table is only a member of a data product if its parent schema was included AND its own
 * dataset-scope rules did not exclude it. `DatasetSelectionOut` carries `parent` and `dataset`
 * as two complete results, deliberately, so a console never has to guess which one decided.
 * The detail below the tree shows both in full rather than collapsing them into one
 * locally-computed "deciding rule", which is exactly the second implementation C4 forbids.
 *
 * Pinning is immediate, and is pinned by qualified name
 * -----------------------------------------------------
 *
 * Overrides are not draftable -- `POST /pairs/{id}/preview` always joins the pair's STORED
 * overrides, whether it is previewing stored rules or a draft -- so a pin takes effect the
 * moment it is saved, and this panel says so next to the control. `overridePinTarget` decides
 * what a node can be pinned by, and the answer is always its `catalog.schema` /
 * `catalog.schema.table` qualified name; a node the source gave no qualified name for cannot be
 * pinned at all and the control is disabled with that reason, never quietly substituting the
 * connector's opaque `object_id` (see `overrides.ts` for the divergence that prevents).
 */
import { useMemo, type ReactNode } from "react";
import {
  Alert,
  AlertDescription,
  Badge,
  Button,
  Separator,
  StatePanel,
  Text,
  Tree,
  type TreeNode,
} from "@elabs-ai/components-ui";
import { Database, Pin, PinOff, Table2 } from "lucide-react";

import { DecisionBadge, SelectionResultDetail, UndeterminedBadge } from "./DecisionParts";
import { SCOPE_LABEL } from "./labels";
import { overridePinTarget } from "./overrides";
import { groupDatasets, schemaNames, type StoredRulePosition } from "./sourceTree";
import type {
  DatasetNodeOut,
  SchemaNodeOut,
  SelectionDecision,
  SelectionOverrideOut,
  RuleScope,
} from "./selectionApi";

/** Above this many flattened rows the tree switches to windowed rendering, so a catalog with
 * thousands of tables mounts a screenful of DOM rather than all of it. Matches the threshold
 * `Tree`'s own `virtualize` prop documents (">50 visible rows"). */
const VIRTUALIZE_ABOVE_ROWS = 60;

export interface SourceTreeState {
  status: "idle" | "loading" | "error" | "loaded";
  message: string | null;
  schemas: SchemaNodeOut[];
  schemaHasMore: boolean;
  schemaLoading: boolean;
  datasets: DatasetNodeOut[];
  datasetHasMore: boolean;
  datasetLoading: boolean;
  datasetsRequested: boolean;
}

export const EMPTY_TREE_STATE: SourceTreeState = {
  status: "idle",
  message: null,
  schemas: [],
  schemaHasMore: false,
  schemaLoading: false,
  datasets: [],
  datasetHasMore: true,
  datasetLoading: false,
  datasetsRequested: false,
};

export type TreeNodeData =
  | { kind: "schema"; node: SchemaNodeOut }
  | { kind: "dataset"; node: DatasetNodeOut }
  | { kind: "note" };

export interface SourceTreePanelProps {
  state: SourceTreeState;
  rulePositions: ReadonlyMap<string, StoredRulePosition>;
  overrides: Readonly<Record<RuleScope, Map<string, SelectionOverrideOut>>>;
  expandedIds: string[];
  onExpandedChange: (ids: string[]) => void;
  selectedId: string | null;
  onSelectedChange: (id: string | null) => void;
  onLoadMoreSchemas: () => void;
  onLoadMoreDatasets: () => void;
  onPin: (node: SchemaNodeOut | DatasetNodeOut, decision: SelectionDecision) => void;
  onUnpin: (override: SelectionOverrideOut) => void;
  onShowDecidingRule: (ruleId: string) => void;
  pinBusy: boolean;
  draftDirty: boolean;
  resolvingTags: boolean;
  resolvingOwners: boolean;
  onRetry: () => void;
}

export function schemaNodeId(node: SchemaNodeOut): string {
  return `object:${node.object_id}`;
}

export function datasetNodeId(node: DatasetNodeOut): string {
  return `dataset:${node.object_id}`;
}

/** A short chip naming the saved rule the engine said decided this schema. A pure id lookup
 * against the saved rule order -- the decision itself is never recomputed. */
function RuleChip({
  ruleId,
  rulePositions,
}: {
  ruleId: string | null;
  rulePositions: ReadonlyMap<string, StoredRulePosition>;
}) {
  if (ruleId === null) return null;
  const found = rulePositions.get(ruleId);
  return (
    <Badge variant="secondary">
      {found ? `rule ${found.position} of ${found.total}` : "a saved rule"}
    </Badge>
  );
}

function nodeLabel({
  name,
  icon,
  included,
  undeterminedCount,
  explain,
  pinned,
  trailing,
}: {
  name: string;
  icon: ReactNode;
  included: boolean;
  undeterminedCount: number;
  explain: string;
  pinned: boolean;
  trailing?: ReactNode;
}): ReactNode {
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      {icon}
      <span className="truncate font-medium">{name}</span>
      <DecisionBadge included={included} />
      {undeterminedCount > 0 ? <UndeterminedBadge count={undeterminedCount} /> : null}
      {pinned ? <Badge variant="info">pinned</Badge> : null}
      {trailing}
      <span className="truncate text-caption text-muted-foreground" title={explain}>
        {explain}
      </span>
    </span>
  );
}

export function SourceTreePanel({
  state,
  rulePositions,
  overrides,
  expandedIds,
  onExpandedChange,
  selectedId,
  onSelectedChange,
  onLoadMoreSchemas,
  onLoadMoreDatasets,
  onPin,
  onUnpin,
  onShowDecidingRule,
  pinBusy,
  draftDirty,
  resolvingTags,
  resolvingOwners,
  onRetry,
}: SourceTreePanelProps) {
  const knownSchemas = useMemo(() => schemaNames(state.schemas), [state.schemas]);
  const grouped = useMemo(
    () => groupDatasets(state.datasets, knownSchemas),
    [state.datasets, knownSchemas],
  );

  const expanded = useMemo(() => new Set(expandedIds), [expandedIds]);

  const nodes = useMemo<TreeNode<TreeNodeData>[]>(() => {
    function datasetNode(dataset: DatasetNodeOut): TreeNode<TreeNodeData> {
      const pin = overridePinTarget(dataset);
      const override = pin.pinnable ? overrides.dataset.get(pin.objectId) : undefined;
      const undeterminedCount =
        dataset.selection.dataset.undetermined.length + dataset.selection.parent.undetermined.length;
      return {
        id: datasetNodeId(dataset),
        label: nodeLabel({
          name: dataset.qualified_name ?? dataset.display_name ?? dataset.object_id,
          icon: <Table2 aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />,
          included: dataset.selection.included,
          undeterminedCount,
          explain: dataset.selection.explain,
          pinned: override !== undefined,
        }),
        data: { kind: "dataset", node: dataset },
      };
    }

    const roots: TreeNode<TreeNodeData>[] = state.schemas.map((schema) => {
      const id = schemaNodeId(schema);
      const pin = overridePinTarget(schema);
      const override = pin.pinnable ? overrides.object.get(pin.objectId) : undefined;
      const children = schema.qualified_name ? (grouped.byParent.get(schema.qualified_name) ?? []) : [];
      const isExpanded = expanded.has(id);

      const childNodes: TreeNode<TreeNodeData>[] = isExpanded
        ? [
            ...children.map(datasetNode),
            // While the source's table stream has not been read to its end, a schema's child
            // list is "what has been read", never "what exists". Saying so is the only honest
            // rendering — see `sourceTree.ts`'s module comment.
            ...(state.datasetHasMore
              ? [
                  {
                    id: `pending:${id}`,
                    disabled: true,
                    label: `${children.length} table(s) read so far — more may exist under this schema; the source's table stream has not been read to the end.`,
                    data: { kind: "note" } as TreeNodeData,
                  },
                ]
              : []),
          ]
        : [];

      return {
        id,
        label: nodeLabel({
          name: schema.qualified_name ?? schema.display_name ?? schema.object_id,
          icon: <Database aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />,
          included: schema.result.included,
          undeterminedCount: schema.result.undetermined.length,
          explain: schema.result.explain,
          pinned: override !== undefined,
          trailing:
            schema.result.source === "rule" ? (
              <RuleChip ruleId={schema.result.rule_id} rulePositions={rulePositions} />
            ) : null,
        }),
        hasChildren: children.length > 0 || state.datasetHasMore,
        children: isExpanded ? childNodes : undefined,
        data: { kind: "schema", node: schema },
      };
    });

    if (grouped.unparented.length > 0) {
      const id = "unparented";
      roots.push({
        id,
        label: `${grouped.unparented.length} table(s) whose schema is not among the schemas read so far`,
        hasChildren: true,
        children: expanded.has(id) ? grouped.unparented.map(datasetNode) : undefined,
        data: { kind: "note" },
      });
    }

    return roots;
  }, [state.schemas, state.datasetHasMore, grouped, overrides, expanded, rulePositions]);

  const visibleRowCount = useMemo(() => {
    let count = 0;
    const walk = (list: TreeNode<TreeNodeData>[]) => {
      for (const node of list) {
        count += 1;
        if (expanded.has(node.id) && node.children) walk(node.children);
      }
    };
    walk(nodes);
    return count;
  }, [nodes, expanded]);

  const selected = useMemo(() => {
    if (selectedId === null) return null;
    const schema = state.schemas.find((node) => schemaNodeId(node) === selectedId);
    if (schema) return { kind: "schema" as const, node: schema };
    const dataset = state.datasets.find((node) => datasetNodeId(node) === selectedId);
    if (dataset) return { kind: "dataset" as const, node: dataset };
    return null;
  }, [selectedId, state.schemas, state.datasets]);

  return (
    <div className="flex flex-col gap-4">
      <Text variant="caption" tone="muted">
        The saved rules and overrides, evaluated against the live source — what the next real run
        would see right now. Schemas are read one page at a time; the source&apos;s tables are read
        as one stream and filed under their schema as they arrive, because the API has no
        &quot;tables of this schema&quot; query.
        {resolvingTags || resolvingOwners
          ? ` Tags and owners are being read from the source for every node (${[resolvingTags ? "tags" : null, resolvingOwners ? "owners" : null].filter(Boolean).join(" and ")}), because a saved rule matches on them — one extra source read per node.`
          : " Tags and owners are not being read (no saved rule matches on them), so any rule that needs them would come back undetermined."}
      </Text>

      {draftDirty ? (
        <Alert variant="warning">
          <AlertDescription>
            The rule editor has an <strong>unsaved draft</strong>. This tree still shows the{" "}
            <strong>saved</strong> rules, because the browse route only ever evaluates what is
            stored. Save the draft to see it here; the preview above is already evaluating it.
          </AlertDescription>
        </Alert>
      ) : null}

      {state.status === "error" ? (
        <StatePanel
          kind="error"
          title="Could not browse this source"
          description={state.message ?? "The source did not answer."}
          actions={<Button onClick={onRetry}>Retry</Button>}
        />
      ) : null}

      {state.status === "loading" && state.schemas.length === 0 ? (
        <StatePanel kind="loading" title="Reading the source…" />
      ) : null}

      {state.status === "loaded" && state.schemas.length === 0 ? (
        <StatePanel
          kind="empty"
          title="This source reported no schemas"
          description="Nothing can be selected until the source lists at least one catalog.schema."
        />
      ) : null}

      {state.schemas.length > 0 ? (
        <Tree<TreeNodeData>
          nodes={nodes}
          expandedIds={expandedIds}
          onExpandedChange={onExpandedChange}
          selectionMode="single"
          selectedIds={selectedId === null ? [] : [selectedId]}
          onSelectionChange={(ids) => onSelectedChange(ids[0] ?? null)}
          virtualize={visibleRowCount > VIRTUALIZE_ABOVE_ROWS}
          maxHeight="28rem"
          aria-label="Source tree"
        />
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="outline"
          onClick={onLoadMoreSchemas}
          disabled={!state.schemaHasMore || state.schemaLoading}
        >
          {state.schemaLoading ? "Reading schemas…" : "Read more schemas"}
        </Button>
        <Button
          variant="outline"
          onClick={onLoadMoreDatasets}
          disabled={!state.datasetHasMore || state.datasetLoading}
        >
          {state.datasetLoading ? "Reading tables…" : "Read more tables & views"}
        </Button>
        <Text variant="caption" tone="muted">
          {state.schemas.length} schema(s) read{state.schemaHasMore ? ", more available" : " (all of them)"};{" "}
          {state.datasets.length} table(s) read
          {state.datasetsRequested
            ? state.datasetHasMore
              ? ", more available"
              : " (all of them)"
            : " — expand a schema to start reading them"}
          .
        </Text>
      </div>

      <Separator />

      {selected === null ? (
        <Text variant="caption" tone="muted">
          Select a node above to see its full decision, every rule that could not be evaluated
          against it, and the per-object override controls.
        </Text>
      ) : selected.kind === "schema" ? (
        <SchemaDetail
          node={selected.node}
          override={
            overridePinTarget(selected.node).pinnable
              ? overrides.object.get(selected.node.qualified_name ?? "")
              : undefined
          }
          rulePositions={rulePositions}
          onPin={onPin}
          onUnpin={onUnpin}
          onShowDecidingRule={onShowDecidingRule}
          pinBusy={pinBusy}
        />
      ) : (
        <DatasetDetail
          node={selected.node}
          override={
            overridePinTarget(selected.node).pinnable
              ? overrides.dataset.get(selected.node.qualified_name ?? "")
              : undefined
          }
          onPin={onPin}
          onUnpin={onUnpin}
          onShowDecidingRule={onShowDecidingRule}
          pinBusy={pinBusy}
        />
      )}
    </div>
  );
}

function PinControls({
  node,
  override,
  onPin,
  onUnpin,
  pinBusy,
}: {
  node: SchemaNodeOut | DatasetNodeOut;
  override: SelectionOverrideOut | undefined;
  onPin: (node: SchemaNodeOut | DatasetNodeOut, decision: SelectionDecision) => void;
  onUnpin: (override: SelectionOverrideOut) => void;
  pinBusy: boolean;
}) {
  const target = overridePinTarget(node);
  const name = node.qualified_name ?? node.object_id;

  if (!target.pinnable) {
    return (
      <Alert variant="warning">
        <AlertDescription>
          This object cannot be pinned. {target.reason}
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <Text variant="caption" tone="muted">
        A per-object override beats every rule outright — no rule is consulted at all for a pinned
        object. Overrides are saved immediately (they are never part of the draft) and are pinned
        by qualified name: <code>{target.objectId}</code>.
      </Text>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant={override?.decision === "include" ? "default" : "outline"}
          disabled={pinBusy}
          onClick={() => onPin(node, "include")}
        >
          <Pin aria-hidden className="mr-1.5 size-4" />
          Always include &quot;{name}&quot;
        </Button>
        <Button
          variant={override?.decision === "exclude" ? "default" : "outline"}
          disabled={pinBusy}
          onClick={() => onPin(node, "exclude")}
        >
          <Pin aria-hidden className="mr-1.5 size-4" />
          Always exclude &quot;{name}&quot;
        </Button>
        {override ? (
          <Button variant="outline" disabled={pinBusy} onClick={() => onUnpin(override)}>
            <PinOff aria-hidden className="mr-1.5 size-4" />
            Remove pin
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function DecidingRuleLink({
  ruleId,
  rulePositions,
  onShowDecidingRule,
  label,
}: {
  ruleId: string | null;
  rulePositions?: ReadonlyMap<string, StoredRulePosition>;
  onShowDecidingRule: (ruleId: string) => void;
  label: string;
}) {
  if (ruleId === null) return null;
  const found = rulePositions?.get(ruleId);
  return (
    <Button variant="outline" onClick={() => onShowDecidingRule(ruleId)}>
      {label}
      {found ? ` (evaluation position ${found.position} of ${found.total})` : ""}
    </Button>
  );
}

function SchemaDetail({
  node,
  override,
  rulePositions,
  onPin,
  onUnpin,
  onShowDecidingRule,
  pinBusy,
}: {
  node: SchemaNodeOut;
  override: SelectionOverrideOut | undefined;
  rulePositions: ReadonlyMap<string, StoredRulePosition>;
  onPin: (node: SchemaNodeOut | DatasetNodeOut, decision: SelectionDecision) => void;
  onUnpin: (override: SelectionOverrideOut) => void;
  onShowDecidingRule: (ruleId: string) => void;
  pinBusy: boolean;
}) {
  return (
    <section aria-label="Selected object" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-title font-medium">
          {node.qualified_name ?? node.display_name ?? node.object_id}
        </span>
        <Badge variant="outline">{SCOPE_LABEL.object}</Badge>
      </div>
      <SelectionResultDetail result={node.result} />
      <div className="flex flex-wrap gap-2">
        <DecidingRuleLink
          ruleId={node.result.rule_id}
          rulePositions={rulePositions}
          onShowDecidingRule={onShowDecidingRule}
          label="Show the deciding rule"
        />
      </div>
      <PinControls
        node={node}
        override={override}
        onPin={onPin}
        onUnpin={onUnpin}
        pinBusy={pinBusy}
      />
    </section>
  );
}

function DatasetDetail({
  node,
  override,
  onPin,
  onUnpin,
  onShowDecidingRule,
  pinBusy,
}: {
  node: DatasetNodeOut;
  override: SelectionOverrideOut | undefined;
  onPin: (node: SchemaNodeOut | DatasetNodeOut, decision: SelectionDecision) => void;
  onUnpin: (override: SelectionOverrideOut) => void;
  onShowDecidingRule: (ruleId: string) => void;
  pinBusy: boolean;
}) {
  return (
    <section aria-label="Selected object" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-title font-medium">
          {node.qualified_name ?? node.display_name ?? node.object_id}
        </span>
        <Badge variant="outline">{SCOPE_LABEL.dataset}</Badge>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <DecisionBadge included={node.selection.included} />
      </div>
      <Text variant="body">{node.selection.explain}</Text>
      <Text variant="caption" tone="muted">
        A table is a member of a data product only if its parent schema was included AND its own
        dataset-scope rules did not exclude it. Both halves are shown below exactly as the engine
        reported them.
      </Text>

      <SelectionResultDetail result={node.selection.parent} heading="Parent schema" />
      <SelectionResultDetail
        result={node.selection.dataset}
        heading="This table's own dataset-scope rules, in isolation"
      />
      <Text variant="caption" tone="muted">
        Read the second block as &quot;what the dataset-scope rules alone concluded&quot;, not as
        the answer: inside an included schema, &quot;no rule matched&quot; means this table
        inherits the schema&apos;s selection rather than being excluded. The line at the top is
        the answer the run will act on.
      </Text>

      <div className="flex flex-wrap gap-2">
        <DecidingRuleLink
          ruleId={node.selection.parent.rule_id}
          onShowDecidingRule={onShowDecidingRule}
          label="Show the parent schema's rule"
        />
        <DecidingRuleLink
          ruleId={node.selection.dataset.rule_id}
          onShowDecidingRule={onShowDecidingRule}
          label="Show this table's rule"
        />
      </div>

      <PinControls
        node={node}
        override={override}
        onPin={onPin}
        onUnpin={onUnpin}
        pinBusy={pinBusy}
      />
    </section>
  );
}
