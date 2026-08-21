// The D2/D3 callout -- rendered once, at the top of the plan, across every entity type's
// report. This task's own DoD names these two explicitly: "dataset members that do not exist
// in the target space (RM-01 D2) and owner emails with no matching Qlik user (D3) -- called
// out rather than buried." So this section is not conditional on anything being found: it
// always renders, always in the same place, so an operator never has to expand a record to
// learn whether one of these hit.
//
// The honest limit this panel is built around
// ---------------------------------------------
// `planGrouping.ts`'s own doc comment has the full trace: reading `sync/loop.py` end to end
// shows that a DRY run never calls the target connector's `create()`/`update()` at all, and
// D2/D3 resolution happens ONLY inside those calls (`qlabs-connector-qlik/write.py`). So with
// the engine as it stands today, this panel will read "None found" on every real dry run, not
// because references resolve cleanly, but because the question was never asked. Saying nothing
// about that would be exactly the kind of thing that makes an operator trust a screen more than
// it has earned -- so the caveat below is permanent, not conditional on the list being empty.
import { Badge, Heading, Text } from "@elabs-ai/components-ui";

import { unresolvedReferences, type UnresolvedReference } from "./planGrouping";
import type { SyncRunReportOut } from "./dryRunApi";
import { ENTITY_TYPE_LABEL } from "./labels";

function ReferenceRow({ item }: { item: UnresolvedReference }) {
  const record = item.record;
  return (
    <li
      className="flex flex-col gap-1 rounded-md border border-warning/40 bg-warning/5 p-2"
      data-unresolved-key={`${item.kind}-${record.native_key}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-foreground">
          {record.display_name ?? record.native_key}
        </span>
        <span className="text-caption text-muted-foreground">{record.native_key}</span>
        <Badge variant="secondary">{ENTITY_TYPE_LABEL[item.entityType]}</Badge>
      </div>
      {record.detail ? <Text variant="caption">{record.detail}</Text> : null}
    </li>
  );
}

function ReferenceList({ items }: { items: readonly UnresolvedReference[] }) {
  if (items.length === 0) {
    return (
      <Text variant="caption" tone="muted">
        None found in this plan.
      </Text>
    );
  }
  return (
    <ul className="flex flex-col gap-2">
      {items.map((item) => (
        <ReferenceRow key={`${item.kind}-${item.record.native_key}`} item={item} />
      ))}
    </ul>
  );
}

export function UnresolvedReferencesPanel({ runs }: { runs: readonly SyncRunReportOut[] }) {
  const references = unresolvedReferences(runs);
  const datasetMembers = references.filter((item) => item.kind === "dataset_member");
  const owners = references.filter((item) => item.kind === "owner");

  return (
    <section
      aria-label="Unresolved references"
      className="flex flex-col gap-4 rounded-md border border-border bg-surface-muted/40 p-4"
    >
      <div className="flex flex-col gap-1">
        <Heading level={3}>Unresolved references</Heading>
        <Text variant="caption" tone="muted">
          The two things an operator can act on directly: a dataset member the target space does
          not have, and an owner email with no matching Qlik user. A dry run does not call Qlik
          to resolve either one -- that check only happens while the engine is actually writing
          (a real run) -- so an empty list here does not guarantee every reference below would
          resolve on a real run. Run this pair for real, or check its run history, for the
          engine's actual answer.
        </Text>
      </div>

      <div className="flex flex-col gap-2">
        <Text className="font-medium">
          Unresolved dataset members (D2) — {datasetMembers.length}
        </Text>
        <ReferenceList items={datasetMembers} />
      </div>

      <div className="flex flex-col gap-2">
        <Text className="font-medium">Unresolvable owners (D3) — {owners.length}</Text>
        <ReferenceList items={owners} />
      </div>
    </section>
  );
}
