// Pure unit tests for the field-attribution logic `EndpointFormSheet.tsx` relies on to satisfy
// the DoD ("validation errors are shown against the offending field") and mutation check #5
// ("attach a field validation error to the wrong field, or show it only as a banner"). Every
// fixture mirrors a REAL response `errors.py` produces -- see each test's comment for the
// exact handler it reproduces.
import { describe, expect, it } from "vitest";

import { classifyEndpointError } from "./errorMapping";
import { errorModelFixture } from "./testHelpers";

describe("classifyEndpointError", () => {
  it("attaches connector_not_registered to the connector field (errors.py sets field explicitly)", () => {
    const result = classifyEndpointError(
      errorModelFixture({
        code: "connector_not_registered",
        message: "connector 'nope' is not registered",
        field: "connector",
        entity: "nope",
      }),
    );
    expect(result).toEqual({
      kind: "field",
      field: "connector",
      message: "connector 'nope' is not registered",
    });
  });

  it("attaches secret_ref_invalid to the secret_ref field", () => {
    const result = classifyEndpointError(
      errorModelFixture({
        code: "secret_ref_invalid",
        message: "malformed secret reference",
        field: "secret_ref",
      }),
    );
    expect(result).toEqual({
      kind: "field",
      field: "secret_ref",
      message: "malformed secret reference",
    });
  });

  it("strips the 'body.' prefix a RequestValidationError puts on a top-level field", () => {
    const result = classifyEndpointError(
      errorModelFixture({
        code: "request_validation_error",
        message: "request validation failed",
        field: "body.name",
      }),
    );
    expect(result).toEqual({ kind: "field", field: "name", message: "request validation failed" });
  });

  it("attaches endpoint_already_exists to the name field via `entity`, not `field`", () => {
    const result = classifyEndpointError(
      errorModelFixture({
        code: "endpoint_already_exists",
        message: "an endpoint named 'dup' already exists",
        field: null,
        entity: "dup",
      }),
    );
    expect(result).toEqual({
      kind: "field",
      field: "name",
      message: "an endpoint named 'dup' already exists",
    });
  });

  it("splits inline_secret_rejected's comma-joined field into individual settings keys", () => {
    const result = classifyEndpointError(
      errorModelFixture({
        code: "inline_secret_rejected",
        message: "connector 'secrety' settings must not carry secret-typed field(s) 'api_key', 'token' inline",
        field: "api_key, token",
        entity: "secrety",
      }),
    );
    expect(result).toEqual({
      kind: "settings-fields",
      keys: ["api_key", "token"],
      message: "connector 'secrety' settings must not carry secret-typed field(s) 'api_key', 'token' inline",
    });
  });

  it("routes endpoint_settings_invalid to the settings banner -- the server names no field for it", () => {
    const result = classifyEndpointError(
      errorModelFixture({
        code: "endpoint_settings_invalid",
        message: "settings for connector 'fake' are invalid: nonexistent_field: extra fields not permitted",
        field: null,
        entity: "fake",
      }),
    );
    expect(result).toEqual({
      kind: "settings-banner",
      message: "settings for connector 'fake' are invalid: nonexistent_field: extra fields not permitted",
    });
  });

  it("falls back to the generic form banner for a field this form doesn't own (e.g. a nested settings path)", () => {
    const result = classifyEndpointError(
      errorModelFixture({
        code: "request_validation_error",
        message: "request validation failed",
        field: "body.settings.host",
      }),
    );
    expect(result).toEqual({ kind: "form", message: "request validation failed" });
  });

  it("falls back to the generic form banner when no field/entity is given at all", () => {
    const result = classifyEndpointError(
      errorModelFixture({ code: "config_service_error", message: "unexpected", field: null, entity: null }),
    );
    expect(result).toEqual({ kind: "form", message: "unexpected" });
  });
});
