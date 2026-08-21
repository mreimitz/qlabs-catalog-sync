// One response captured VERBATIM from a running engine -- not a full plan (see below for
// exactly why), but real, over-the-wire proof that this feature's error path matches what the
// real server actually sends, not only a hand-typed fixture that happens to compile.
//
// What was captured, and how
// ---------------------------
// `qlabs-catalog-sync serve` against a real SQLite state store (a throwaway one, this session
// only), with the real API: signed in as the real admin session, registered a real `databricks`
// SOURCE endpoint and a real `qlik` TARGET endpoint through `POST /api/endpoints` (both accepted
// with no live connectivity check -- registration only validates each connector's own
// `ConfigModel` shape), created a real pair naming both through `POST /api/pairs`, then called
// the real route this screen calls: `POST /api/pairs/{id}/dry-run`. The response below is that
// call's real body, byte for byte (only the `entity` value was left as recorded; nothing was
// added, removed or reworded).
//
// Why this is not a full recorded PLAN, unlike `../selection/recordedPayloads.ts`
// -----------------------------------------------------------------------------------
// Selection's own recorded fixtures needed only a fake SOURCE (a throwaway HTTPS stand-in for
// Unity Catalog) -- selection never touches the target at all. A dry-run PLAN needs both
// connectors to construct successfully, and reading both connectors' `setup()` shows each does
// real OAuth2 client-credentials authentication eagerly, against its own token endpoint, before
// anything else runs: `qlabs-connector-databricks/__init__.py`'s `Connector.setup` -- "Fetches
// the initial access token... a bad client id/secret surfaces here as AuthError" -- and
// `qlabs-connector-qlik`'s own `setup` does the same against Qlik Cloud's `/oauth/token`. So a
// full plan needs TWO working HTTPS stand-ins (one Unity-Catalog-shaped, one
// Qlik-Cloud-shaped), each terminating real TLS and answering a real OAuth2 token exchange
// correctly, before either connector's own data endpoints are ever reached -- a materially larger
// build than T13.5's read-only, source-only stub. That was out of scope for the time available
// on this task; what got captured instead is the real, complete failure path an operator sees
// today the moment an endpoint's credentials do not resolve -- which is exactly what a
// misconfigured (or, as here, deliberately unreachable) endpoint produces, over the SAME route
// this screen calls, through the SAME error shape (`ErrorModel`, `api/errors.py`'s
// `API_ERROR_RESPONSES`) every route in `openapi.json` promises.
//
// Annotated with the generated type, not cast -- so an API shape change breaks the build.
import type { components } from "../../api/generated/schema";

export type ErrorModel = components["schemas"]["ErrorModel"];

export const RECORDED_ENDPOINT_SETUP_FAILED_ERROR: ErrorModel = {
  code: "endpoint_setup_failed",
  message:
    "endpoint 'databricks_probe' could not be set up: secret backend 'environment' has no " +
    "value for endpoint 'DATABRICKS_PROBE_CLIENT_SECRET', key 'client_secret' (expected " +
    "environment variable DATABRICKS_PROBE_CLIENT_SECRET__CLIENT_SECRET)",
  field: null,
  entity: "databricks_probe",
  correlation_id: null,
};
