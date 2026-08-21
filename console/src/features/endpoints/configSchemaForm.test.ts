// Pure unit tests for `configSchemaForm.ts` -- no React, no fetch. The `databricks`/`qlik`
// cases below parse the REAL recorded `config_schema` payloads (`recordedConnectors.ts`); the
// synthetic ones (clearly marked) exercise shapes neither real connector happens to declare
// (boolean/number/enum properties, and shapes this form does not support at all) so the
// "unsupported kinds still render, never drop a property" contract has unit coverage
// independent of what today's two connectors ship. `SchemaSettingsForm.test.tsx` proves the same
// synthetic "unsupported" case renders as an editable control, not nothing (mutation check #2).
import { describe, expect, it } from "vitest";

import {
  describeConfigSchema,
  effectiveValue,
  missingRequiredFields,
  unknownSettingNames,
  type ConfigFieldDescriptor,
} from "./configSchemaForm";
import { RECORDED_DATABRICKS_CONNECTOR, RECORDED_QLIK_CONNECTOR } from "./recordedConnectors";

describe("describeConfigSchema -- real recorded schemas", () => {
  it("parses the real databricks config_schema: two required strings, one nullable string, one string array", () => {
    const descriptors = describeConfigSchema(RECORDED_DATABRICKS_CONNECTOR.config_schema);
    expect(descriptors.map((d) => d.name)).toEqual([
      "host",
      "client_id",
      "sql_warehouse_id",
      "catalog_schema_patterns",
    ]);

    const host = descriptors.find((d) => d.name === "host")!;
    expect(host).toMatchObject({ title: "Host", required: true, nullable: false, hasDefault: false });
    expect(host.field).toEqual({ kind: "string" });
    expect(host.description).toContain("workspace host");

    const clientId = descriptors.find((d) => d.name === "client_id")!;
    expect(clientId.required).toBe(true);
    expect(clientId.field).toEqual({ kind: "string" });

    const sqlWarehouse = descriptors.find((d) => d.name === "sql_warehouse_id")!;
    expect(sqlWarehouse).toMatchObject({
      title: "Sql Warehouse Id",
      required: false,
      nullable: true,
      hasDefault: true,
      default: null,
    });
    expect(sqlWarehouse.field).toEqual({ kind: "string" });

    const patterns = descriptors.find((d) => d.name === "catalog_schema_patterns")!;
    expect(patterns.required).toBe(false);
    expect(patterns.field).toEqual({ kind: "string-array" });
  });

  it("parses the real qlik config_schema: three required strings, one nullable string with a non-null default", () => {
    const descriptors = describeConfigSchema(RECORDED_QLIK_CONNECTOR.config_schema);
    expect(descriptors.map((d) => d.name)).toEqual(["base_url", "client_id", "scope", "space_id"]);

    for (const name of ["base_url", "client_id", "space_id"]) {
      const descriptor = descriptors.find((d) => d.name === name)!;
      expect(descriptor.required).toBe(true);
      expect(descriptor.field).toEqual({ kind: "string" });
    }

    const scope = descriptors.find((d) => d.name === "scope")!;
    expect(scope).toMatchObject({
      title: "Scope",
      required: false,
      nullable: true,
      hasDefault: true,
      default: "user_default",
    });
    expect(scope.field).toEqual({ kind: "string" });
    // qlik's `scope` property carries no `description` in the real schema (verified against the
    // live service) -- must not be invented.
    expect(scope.description).toBeUndefined();
  });

  it("client_secret never appears -- the server already stripped it before this form ever sees the schema", () => {
    const databricksNames = describeConfigSchema(RECORDED_DATABRICKS_CONNECTOR.config_schema).map((d) => d.name);
    const qlikNames = describeConfigSchema(RECORDED_QLIK_CONNECTOR.config_schema).map((d) => d.name);
    expect(databricksNames).not.toContain("client_secret");
    expect(qlikNames).not.toContain("client_secret");
  });
});

describe("describeConfigSchema -- degenerate input", () => {
  it("returns [] for a null schema (an unavailable connector, or one whose ConfigModel could not produce one)", () => {
    expect(describeConfigSchema(null)).toEqual([]);
  });

  it("returns [] for a schema object with no properties, rather than throwing", () => {
    expect(describeConfigSchema({ type: "object" })).toEqual([]);
  });

  it("returns [] for a non-object schema, rather than throwing", () => {
    expect(describeConfigSchema("not a schema")).toEqual([]);
    expect(describeConfigSchema(42)).toEqual([]);
  });
});

describe("describeConfigSchema -- synthetic shapes no real connector happens to declare today", () => {
  it("MUTATION #2 -- a property whose shape this form does not support still appears, classified 'unsupported', never dropped", () => {
    const descriptors = describeConfigSchema({
      properties: {
        // A nested object -- none of the five supported kinds.
        advanced: { type: "object", title: "Advanced" },
        // A real union of two non-null types -- deliberately not the one `anyOf`-with-null
        // shape this form understands.
        either: { anyOf: [{ type: "string" }, { type: "integer" }], title: "Either" },
        // An array of a type other than string.
        weights: { type: "array", items: { type: "number" }, title: "Weights" },
      },
      required: ["advanced"],
    });
    expect(descriptors).toHaveLength(3);
    expect(descriptors.map((d) => d.field.kind)).toEqual(["unsupported", "unsupported", "unsupported"]);
    // Required-ness is still tracked correctly even for an unsupported kind.
    expect(descriptors.find((d) => d.name === "advanced")!.required).toBe(true);
  });

  it("classifies a boolean property", () => {
    const [descriptor] = describeConfigSchema({
      properties: { verbose: { type: "boolean", title: "Verbose", default: false } },
    });
    expect(descriptor!.field).toEqual({ kind: "boolean" });
    expect(descriptor!.hasDefault).toBe(true);
    expect(descriptor!.default).toBe(false);
  });

  it("classifies a number/integer property", () => {
    const [descriptor] = describeConfigSchema({
      properties: { timeout_seconds: { type: "integer", title: "Timeout Seconds" } },
    });
    expect(descriptor!.field).toEqual({ kind: "number" });
  });

  it("classifies a string enum property", () => {
    const [descriptor] = describeConfigSchema({
      properties: { mode: { type: "string", enum: ["fast", "safe"], title: "Mode" } },
    });
    expect(descriptor!.field).toEqual({ kind: "enum", values: ["fast", "safe"] });
  });

  it("a non-string enum falls back to unsupported rather than rendering the wrong control", () => {
    const [descriptor] = describeConfigSchema({
      properties: { priority: { enum: [1, 2, 3], title: "Priority" } },
    });
    expect(descriptor!.field).toEqual({ kind: "unsupported" });
  });
});

describe("effectiveValue", () => {
  const [sqlWarehouse] = describeConfigSchema(RECORDED_DATABRICKS_CONNECTOR.config_schema).filter(
    (d) => d.name === "sql_warehouse_id",
  );
  const [scope] = describeConfigSchema(RECORDED_QLIK_CONNECTOR.config_schema).filter((d) => d.name === "scope");

  it("falls back to the schema's own default when the operator has not touched the field", () => {
    expect(effectiveValue(sqlWarehouse!, {})).toBeNull();
    expect(effectiveValue(scope!, {})).toBe("user_default");
  });

  it("prefers whatever is already in `settings` over the schema default", () => {
    expect(effectiveValue(sqlWarehouse!, { sql_warehouse_id: "wh-123" })).toBe("wh-123");
    expect(effectiveValue(scope!, { scope: "custom_scope" })).toBe("custom_scope");
  });
});

describe("missingRequiredFields", () => {
  const descriptors = describeConfigSchema(RECORDED_DATABRICKS_CONNECTOR.config_schema);

  it("MUTATION #4 -- names every required property that is not yet present", () => {
    expect(missingRequiredFields(descriptors, {})).toEqual(["host", "client_id"]);
    expect(missingRequiredFields(descriptors, { host: "https://x" })).toEqual(["client_id"]);
  });

  it("is satisfied once every required property has a non-empty value, regardless of optional ones", () => {
    expect(missingRequiredFields(descriptors, { host: "https://x", client_id: "abc" })).toEqual([]);
  });

  it("treats a blank string as still missing, not merely 'present'", () => {
    expect(missingRequiredFields(descriptors, { host: "   ", client_id: "abc" })).toEqual(["host"]);
  });
});

describe("unknownSettingNames", () => {
  it("MUTATION #3 -- names a stored key the current schema does not describe", () => {
    const descriptors = describeConfigSchema(RECORDED_QLIK_CONNECTOR.config_schema);
    const names = unknownSettingNames(descriptors, RECORDED_QLIK_CONNECTOR.config_secret_fields, {
      base_url: "https://acme.eu.qlikcloud.com",
      client_id: "abc",
      space_id: "space-1",
      legacy_flag: true,
    });
    expect(names).toEqual(["legacy_flag"]);
  });

  it("excludes a stored key that matches a secret-typed field name, even though the schema doesn't describe it either", () => {
    const descriptors = describeConfigSchema(RECORDED_QLIK_CONNECTOR.config_schema);
    const names = unknownSettingNames(descriptors, RECORDED_QLIK_CONNECTOR.config_secret_fields, {
      base_url: "https://acme.eu.qlikcloud.com",
      client_secret: "leaked-value",
    });
    expect(names).toEqual([]);
  });

  it("returns [] once every stored key is either schema-described or secret-typed", () => {
    const descriptors = describeConfigSchema(RECORDED_QLIK_CONNECTOR.config_schema);
    const names = unknownSettingNames(descriptors, RECORDED_QLIK_CONNECTOR.config_secret_fields, {
      base_url: "https://acme.eu.qlikcloud.com",
      client_id: "abc",
      space_id: "space-1",
    });
    expect(names).toEqual([]);
  });
});

// Typechecks `ConfigFieldDescriptor`'s discriminated `field` union is actually narrowable the
// way `SchemaSettingsForm.tsx`'s `switch` relies on -- a compile-time check more than a runtime
// one; if this stops compiling, so does the component.
function _typeNarrowingSanityCheck(descriptor: ConfigFieldDescriptor): string {
  switch (descriptor.field.kind) {
    case "enum":
      return descriptor.field.values.join(",");
    default:
      return descriptor.field.kind;
  }
}
void _typeNarrowingSanityCheck;
