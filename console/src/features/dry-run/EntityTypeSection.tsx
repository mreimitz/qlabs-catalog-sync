// Renders one `SyncRunReportOut` -- one entity type's slice of the plan. Grouped into the
// DoD's three primary buckets (creates / updates / no-ops) plus one more ("other") so a
// skipped, filtered, orphaned or failed record is never simply absent from the screen --
// `planGrouping.ts`'s `groupRecords` decides the bucket, this file only renders it.
import {
  Badge,
  Heading,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Text,
} from "@elabs-ai/components-ui";
import { AlertTriangle } from "lucide-react";

import type { ErrorReportOut, RecordReportOut, SyncRunReportOut } from "./dryRunApi";
import { D2_FIELD, D3_FIELD, groupRecords } from "./planGrouping";
import {
  COUNT_FIELD_LABEL,
  COUNT_FIELD_ORDER,
  ENTITY_TYPE_LABEL,
  OUTCOME_LABEL,
  OUTCOME_VARIANT,
  RUN_STATUS_LABEL,
  RUN_STATUS_VARIANT,
} from "./labels";

function CountsStrip({ report }: { report: SyncRunReportOut }) {
  return (
    <div
      role="region"
      aria-label={`${ENTITY_TYPE_LABEL[report.entity_type]} counts`}
      className="flex flex-wrap gap-4"
    >
      {COUNT_FIELD_ORDER.map((field) => (
        <div key={field} className="flex flex-col">
          <Text variant="caption" tone="muted">
            {COUNT_FIELD_LABEL[field]}
          </Text>
          <Text variant="body" className="font-medium tabular-nums">
            {report.counts[field]}
          </Text>
        </div>
      ))}
    </div>
  );
}

function FieldList({ label, fields }: { label: string; fields: readonly string[] }) {
  if (fields.length === 0) return null;
  return (
    <div className="flex flex-col gap-0.5">
      <Text variant="caption" className="font-medium">
        {label}
      </Text>
      <Text variant="caption" tone="muted">
        {fields.join(", ")}
      </Text>
    </div>
  );
}

/** One create or update record, with every field-level fact rendered as its OWN list --
 * `dropped`, `withheld` and `target_skipped_fields` are three different facts (the target
 * cannot carry the field at all; the engine chose not to send it; the target attempted the
 * write and reported it did not fully land) and this task's DoD is explicit that collapsing
 * them into one "skipped" bucket misrepresents the plan. None of the three are computed here --
 * each is the record's own field, rendered verbatim. */
function RecordCard({ record }: { record: RecordReportOut }) {
  const flagsDatasetMember = record.target_skipped_fields.includes(D2_FIELD);
  const flagsOwner = record.target_skipped_fields.includes(D3_FIELD);
  return (
    <li
      className="flex flex-col gap-2 rounded-md border border-border p-3"
      data-record-key={record.native_key}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-foreground">
          {record.display_name ?? record.native_key}
        </span>
        {record.display_name ? (
          <span className="text-caption text-muted-foreground">{record.native_key}</span>
        ) : null}
        <Badge variant={OUTCOME_VARIANT[record.outcome]}>{OUTCOME_LABEL[record.outcome]}</Badge>
        {flagsDatasetMember ? (
          <Badge variant="warning">Unresolved dataset member (D2)</Badge>
        ) : null}
        {flagsOwner ? <Badge variant="warning">Unresolvable owner (D3)</Badge> : null}
      </div>
      {record.detail ? <Text variant="caption">{record.detail}</Text> : null}
      <FieldList label="Fields changed at the source" fields={record.changed_fields} />
      <FieldList label="Fields this write includes" fields={record.written_fields} />
      {record.dropped.length > 0 ? (
        <div className="flex flex-col gap-0.5">
          <Text variant="caption" className="font-medium">
            Dropped — the target cannot carry these
          </Text>
          <ul className="flex flex-col gap-0.5">
            {record.dropped.map((item) => (
              <li key={item.field}>
                <Text variant="caption" tone="muted">
                  {item.field}: {item.reason}
                  {item.capability_mode ? ` (mode: ${item.capability_mode})` : ""}
                </Text>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {record.withheld.length > 0 ? (
        <div className="flex flex-col gap-0.5">
          <Text variant="caption" className="font-medium">
            Withheld by engine policy
          </Text>
          <ul className="flex flex-col gap-0.5">
            {record.withheld.map((item) => (
              <li key={item.field}>
                <Text variant="caption" tone="muted">
                  {item.field}: {item.reason}
                </Text>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {record.target_skipped_fields.length > 0 ? (
        <div className="flex flex-col gap-0.5">
          <Text variant="caption" className="font-medium">
            Target reported not written
          </Text>
          <Text variant="caption" tone="muted">
            {record.target_skipped_fields.join(", ")}
          </Text>
        </div>
      ) : null}
    </li>
  );
}

function RecordCardGroup({
  title,
  records,
}: {
  title: string;
  records: readonly RecordReportOut[];
}) {
  return (
    <div className="flex flex-col gap-2">
      <Text className="font-medium">
        {title} ({records.length})
      </Text>
      {records.length === 0 ? (
        <Text variant="caption" tone="muted">
          None.
        </Text>
      ) : (
        <ul className="flex flex-col gap-2">
          {records.map((record) => (
            <RecordCard key={record.native_key} record={record} />
          ))}
        </ul>
      )}
    </div>
  );
}

/** No-ops and "other" outcomes (skipped / filtered / orphaned / failed) -- a plain table,
 * because there is no field-level plan to show for a record that would write nothing. The
 * "Detail" column echoes the server's own text verbatim -- for an orphan this is
 * `sync/loop.py`'s own "gone at the source; recorded as an orphan and never deleted at the
 * target", the same wording `../runs/RunIssuesPanel.tsx` already established for the identical
 * fact on the Runs screen, reused here rather than re-invented. */
function RecordOutcomeTable({
  title,
  records,
}: {
  title: string;
  records: readonly RecordReportOut[];
}) {
  return (
    <div className="flex flex-col gap-2">
      <Text className="font-medium">
        {title} ({records.length})
      </Text>
      {records.length === 0 ? (
        <Text variant="caption" tone="muted">
          None.
        </Text>
      ) : (
        <Table>
          <TableCaption className="sr-only">{title}</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead>Object</TableHead>
              <TableHead>Outcome</TableHead>
              <TableHead>Detail</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {records.map((record) => (
              <TableRow key={record.native_key} data-record-key={record.native_key}>
                <TableCell>
                  <div className="flex flex-col">
                    <span className="font-medium text-foreground">
                      {record.display_name ?? record.native_key}
                    </span>
                    {record.display_name ? (
                      <span className="text-caption text-muted-foreground">
                        {record.native_key}
                      </span>
                    ) : null}
                  </div>
                </TableCell>
                <TableCell>
                  <Badge variant={OUTCOME_VARIANT[record.outcome]}>
                    {OUTCOME_LABEL[record.outcome]}
                  </Badge>
                  {record.outcome === "orphaned" ? (
                    <Badge variant="outline" className="ml-1.5">
                      reported, not deleted
                    </Badge>
                  ) : null}
                </TableCell>
                <TableCell className="max-w-md">
                  <Text variant="caption">{record.detail ?? record.reason ?? "—"}</Text>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

function ErrorsList({ errors }: { errors: readonly ErrorReportOut[] }) {
  if (errors.length === 0) return null;
  return (
    <ul className="flex flex-col gap-2">
      {errors.map((error, index) => (
        <li key={`${error.kind}-${index}`} className="rounded-md border border-destructive/40 p-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="destructive">{error.kind}</Badge>
            {error.endpoint ? (
              <span className="text-caption text-muted-foreground">endpoint: {error.endpoint}</span>
            ) : null}
            {error.operation ? (
              <span className="text-caption text-muted-foreground">op: {error.operation}</span>
            ) : null}
          </div>
          <Text variant="caption">{error.message}</Text>
        </li>
      ))}
    </ul>
  );
}

export function EntityTypeSection({ report }: { report: SyncRunReportOut }) {
  const grouped = groupRecords(report.records);
  const failed = report.status === "failed";

  return (
    <section
      aria-label={`${ENTITY_TYPE_LABEL[report.entity_type]} plan`}
      className="flex flex-col gap-4 rounded-md border border-border p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Heading level={3}>{ENTITY_TYPE_LABEL[report.entity_type]}</Heading>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={RUN_STATUS_VARIANT[report.status]}>{RUN_STATUS_LABEL[report.status]}</Badge>
          <Badge variant="secondary">No changes applied — dry run</Badge>
        </div>
      </div>

      <Text variant="caption" tone="muted">
        {report.source_endpoint} → {report.target_endpoint} · {report.duration_seconds.toFixed(1)}s
      </Text>

      <CountsStrip report={report} />

      {failed ? (
        <div
          role="alert"
          className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3"
        >
          <div className="flex items-center gap-2">
            <AlertTriangle aria-hidden className="size-4 text-destructive" />
            <Text className="font-medium">
              Could not produce a plan for {ENTITY_TYPE_LABEL[report.entity_type].toLowerCase()}
            </Text>
          </div>
          <Text variant="caption" tone="muted">
            This is a failed cycle, not an empty one -- the plan below (if any records made it
            through before the failure) is incomplete.
          </Text>
          <ErrorsList errors={report.errors} />
        </div>
      ) : null}

      {report.records.length === 0 && !failed ? (
        <Text variant="body" tone="muted">
          No candidate objects were evaluated for{" "}
          {ENTITY_TYPE_LABEL[report.entity_type].toLowerCase()} in this plan.
        </Text>
      ) : (
        <>
          <RecordCardGroup title="Creates" records={grouped.creates} />
          <RecordCardGroup title="Updates" records={grouped.updates} />
          <RecordOutcomeTable title="No-ops" records={grouped.noOps} />
          <RecordOutcomeTable title="Other outcomes" records={grouped.other} />
        </>
      )}
    </section>
  );
}
