// Renders a `CapabilityManifestOut` (per entity type: `supported`, `identity_keys`, and each
// field's `mode`/`writable_via`/`partial_update`/`normalized_by_target`) -- shared by the
// per-endpoint manifest sheet (`EndpointManifestSheet.tsx`, real data from a configured
// endpoint) and the register form's connector preview (`EndpointFormSheet.tsx`, the connector
// CLASS's own manifest, already in hand from the one `GET /connectors` call this screen makes
// per load -- never a second fetch).
import {
  Badge,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@elabs-ai/components-ui";

import type { CapabilityManifestOut } from "./endpointsApi";

const FIELD_MODE_LABEL: Record<string, string> = {
  rw: "Read/write",
  ro: "Read-only",
  na: "Not available",
};

/** `ro` and `na` both render as "never written", but the DoD is explicit that the UI must say
 * WHY -- `ro` means the endpoint can express the field but only ever returns it; `na` means
 * either no native equivalent exists at all, or this endpoint instance cannot currently even
 * read it (RS-08 section 4.2 / decision D6, `FieldCapabilityMode`'s own docstring). */
const FIELD_MODE_HINT: Record<string, string> = {
  rw: "readable and writable",
  ro: "readable, but the endpoint only ever returns this field -- it is never written",
  na: "no native equivalent, or this endpoint instance cannot currently read it",
};

const FIELD_MODE_VARIANT: Record<string, "success" | "secondary" | "outline"> = {
  rw: "success",
  ro: "secondary",
  na: "outline",
};

export function ManifestPanel({ manifest }: { manifest: CapabilityManifestOut }) {
  const entityTypes = Object.keys(manifest.entities).sort();
  return (
    <div className="flex flex-col gap-6">
      <p className="text-caption text-muted-foreground">
        Concurrency: <span className="font-medium text-foreground">{manifest.concurrency}</span>
      </p>
      {entityTypes.length === 0 ? (
        <p className="text-caption text-muted-foreground">
          This connector reports no supported entity types.
        </p>
      ) : null}
      {entityTypes.map((entityType) => {
        const entity = manifest.entities[entityType];
        if (!entity) return null;
        const fieldNames = Object.keys(entity.fields).sort();
        return (
          <section key={entityType} className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <h3 className="text-body font-medium">{entityType}</h3>
              <Badge variant={entity.supported ? "success" : "outline"}>
                {entity.supported ? "Supported" : "Not supported"}
              </Badge>
            </div>
            {entity.supported ? (
              <>
                <p className="text-caption text-muted-foreground">
                  Identity keys:{" "}
                  {entity.identity_keys.length > 0 ? entity.identity_keys.join(", ") : "none"}
                </p>
                {fieldNames.length > 0 ? (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Field</TableHead>
                        <TableHead>Mode</TableHead>
                        <TableHead>Writable via</TableHead>
                        <TableHead>Partial update</TableHead>
                        <TableHead>Normalized by target</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {fieldNames.map((fieldName) => {
                        const field = entity.fields[fieldName];
                        if (!field) return null;
                        return (
                          <TableRow key={fieldName}>
                            <TableCell>{fieldName}</TableCell>
                            <TableCell>
                              <Badge variant={FIELD_MODE_VARIANT[field.mode] ?? "outline"}>
                                {FIELD_MODE_LABEL[field.mode] ?? field.mode}
                              </Badge>
                              <span className="sr-only">
                                {FIELD_MODE_HINT[field.mode] ?? ""}
                              </span>
                            </TableCell>
                            <TableCell>{field.writable_via ?? "—"}</TableCell>
                            <TableCell>{field.partial_update ? "Yes" : "No"}</TableCell>
                            <TableCell>{field.normalized_by_target ? "Yes" : "No"}</TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                ) : null}
              </>
            ) : null}
          </section>
        );
      })}
    </div>
  );
}
