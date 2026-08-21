// Turning the API's two flat, offset-paginated node streams into the hierarchy the operator
// thinks in — and being honest about the join, because the join is where a tree can start
// implying things the API never said.
//
// What the API actually offers
// ----------------------------
//
// `GET /pairs/{id}/source-tree` is ONE walk, exposed as a flat, OFFSET-paginated page:
// `{nodes, offset, limit, has_more, next_offset}`. `scope=object` walks every schema;
// `scope=dataset` walks every table and view in the source. There is **no** "the datasets of
// schema X" query and no per-schema cursor: `walk_source_tree` yields every schema, then every
// dataset, and a dataset's own page position has nothing to do with its parent.
//
// So a schema's children cannot be fetched. They can only be RECOGNISED, out of the dataset
// stream, by the `catalog.schema` prefix of the `qualified_name` the server put on each dataset
// node. That is a name lookup, not a decision — every included/excluded/undetermined answer and
// every deciding rule still arrives on the node itself, evaluated once by the engine.
//
// The consequence this module refuses to hide
// -------------------------------------------
//
// Until the dataset stream has been read to its end (`has_more === false`), "the tables under
// this schema" is only ever "the tables under this schema THAT HAVE BEEN READ SO FAR". A tree
// that rendered a partially-read stream as a complete child list would tell an operator a
// schema has three tables when it has three hundred. `groupDatasets` therefore reports
// `complete` alongside the children, and the tree renders an explicit "more may exist" row for
// every schema while the stream is unfinished.
//
// Nothing is silently dropped either: a dataset whose parent schema is not among the schema
// pages read so far (or whose `qualified_name` the source did not report at all) is collected
// into `unparented` and rendered under its own root, rather than vanishing because no bucket
// claimed it.
import type { DatasetNodeOut, SchemaNodeOut, SelectionRuleOut, RuleScope } from "./selectionApi";
import { sortByOrdinal } from "./draft";
import { RULE_SCOPES } from "./labels";

/** The `catalog.schema` a `catalog.schema.table` belongs to, or `null` when the source did not
 * report a usable qualified name for it. Pure string work on a server-provided name. */
export function parentSchemaName(dataset: DatasetNodeOut): string | null {
  const name = dataset.qualified_name;
  if (name == null) return null;
  const segments = name.split(".");
  if (segments.length !== 3 || segments.some((segment) => segment.trim() === "")) return null;
  return `${segments[0]}.${segments[1]}`;
}

export interface GroupedDatasets {
  byParent: Map<string, DatasetNodeOut[]>;
  /** Datasets whose parent schema has not been read yet, or which have no usable name. */
  unparented: DatasetNodeOut[];
}

export function groupDatasets(
  datasets: readonly DatasetNodeOut[],
  knownSchemaNames: ReadonlySet<string>,
): GroupedDatasets {
  const byParent = new Map<string, DatasetNodeOut[]>();
  const unparented: DatasetNodeOut[] = [];

  for (const dataset of datasets) {
    const parent = parentSchemaName(dataset);
    if (parent === null || !knownSchemaNames.has(parent)) {
      unparented.push(dataset);
      continue;
    }
    const bucket = byParent.get(parent);
    if (bucket === undefined) byParent.set(parent, [dataset]);
    else bucket.push(dataset);
  }

  return { byParent, unparented };
}

export function schemaNames(schemas: readonly SchemaNodeOut[]): Set<string> {
  const names = new Set<string>();
  for (const schema of schemas) {
    if (schema.qualified_name != null) names.add(schema.qualified_name);
  }
  return names;
}

export interface StoredRulePosition {
  scope: RuleScope;
  /** 1-based evaluation position, for display. */
  position: number;
  total: number;
  rule: SelectionRuleOut;
}

/** Where each SAVED rule sits in its scope's evaluation order, keyed by the id the engine
 * reports on `SelectionResultOut.rule_id`.
 *
 * The source tree always evaluates the pair's STORED rules (a draft is not browsable), so a
 * `rule_id` off a tree node names a saved rule and is resolved against the saved order here --
 * not against the draft, whose order the operator may already have changed. Ordinal-sorted for
 * the same reason the editor is: `ordinal` is the contract, array position is a convenience. */
export function storedRulePositions(
  stored: Record<RuleScope, SelectionRuleOut[]>,
): Map<string, StoredRulePosition> {
  const index = new Map<string, StoredRulePosition>();
  for (const scope of RULE_SCOPES) {
    const ordered = sortByOrdinal(stored[scope]);
    ordered.forEach((rule, at) => {
      index.set(rule.id, { scope, position: at + 1, total: ordered.length, rule });
    });
  }
  return index;
}
