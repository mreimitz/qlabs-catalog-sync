// `GET /api/connectors` RECORDED VERBATIM from a running engine, so this feature's schema-driven
// settings form is tested against what the service actually sends -- not against a hand-written
// belief about what a connector's `config_schema` looks like. (The task brief is explicit about
// why: a fixture built on an assumption about a response shape, rather than a recorded real one,
// has already shipped a broken screen once on this build.)
//
// How this was produced: `qlabs-catalog-sync serve` against a real throwaway SQLite state store
// (`endpoints: {}`, `pairs: []`), with the real entry-point-discovered `databricks` and `qlik`
// connectors -- nothing stubbed, no fake registry. Signed in as the console admin, then
// `curl -b cookies.txt http://127.0.0.1:8129/api/connectors`.
//
// Every field below -- including `config_schema`'s exact JSON Schema shape, which properties
// `qlik`/`databricks` declare `nullable` via `anyOf`, which carry a `default`, and exactly which
// property `config_secret_fields` names -- is copied byte-for-byte from that response. Typed as
// `ConnectorInfo[]` (annotated, not cast): a real shape change stops this file compiling instead
// of silently disagreeing with the service. Nothing was added, reordered or reworded; the only
// change from the raw response is trimming this file down to the two connectors that carry a
// `config_schema` (`databricks`, `qlik`) -- `collibra` and `snowflake` are both broken
// (`qlabs_catalog_sync_sdk.contract.Connector` subclass mismatches unrelated to this feature) and
// carry no schema, so they add nothing a hand-written `connectorInfoFixture({available: false})`
// doesn't already cover in `testHelpers.ts`.
import type { ConnectorInfo } from "./endpointsApi";

export const RECORDED_CONNECTORS: ConnectorInfo[] = [
  {
    name: "databricks",
    available: true,
    manifest: null,
    config_schema: {
      additionalProperties: false,
      description:
        "Workspace host, OAuth M2M service-principal credentials, and an optional\nSQL warehouse.\n\nLoaded via :meth:`~qlabs_catalog_sync_sdk.config.ConnectorConfig.for_endpoint`, so a\ndeployment configures this through ``<ENDPOINT>__HOST``, ``<ENDPOINT>__CLIENT_ID``,\n``<ENDPOINT>__CLIENT_SECRET`` and, optionally, ``<ENDPOINT>__SQL_WAREHOUSE_ID`` —\nnever read from ``os.environ`` directly by connector code.",
      properties: {
        host: {
          description:
            "The workspace host, e.g. 'https://adb-1234567890123456.7.azuredatabricks.net'. Do not append '/api' — REST paths are joined onto this as-is.",
          title: "Host",
          type: "string",
        },
        client_id: {
          description: "The OAuth M2M service principal's application (client) id. Not a secret.",
          minLength: 1,
          title: "Client Id",
          type: "string",
        },
        sql_warehouse_id: {
          anyOf: [{ type: "string" }, { type: "null" }],
          default: null,
          description:
            "A SQL warehouse id used to read Unity Catalog tags via INFORMATION_SCHEMA.*_TAGS over the Statement Execution API (decision D6). Omit to leave tag reads declared 'na' in the capability manifest.",
          title: "Sql Warehouse Id",
        },
        catalog_schema_patterns: {
          description:
            "Endpoint-level allow-list of `catalog.schema` glob patterns this connector may read at all. Defaults to everything the service principal can see; the per-pair selector (SyncPairConfig.catalog_schema_patterns, decision D1) is applied by the engine on top of this and is the one an operator normally edits. Present here because Connector.read(ref) receives only a ref.",
          items: { type: "string" },
          title: "Catalog Schema Patterns",
          type: "array",
        },
      },
      required: ["host", "client_id"],
      title: "DatabricksConfig",
      type: "object",
    },
    config_secret_fields: ["client_secret"],
    manifest_unavailable_reason:
      "this connector reports what it supports only once an endpoint using it has been configured, because its capabilities depend on that configuration. Register an endpoint to see its manifest. (capabilities() needs the resolved config: call setup(ctx) first)",
    distribution: null,
    broken_stage: null,
    broken_reason: null,
  },
  {
    name: "qlik",
    available: true,
    manifest: {
      concurrency: "etag",
      entities: {
        data_product: {
          supported: true,
          identity_keys: ["id", "qri"],
          supports_events: false,
          allowed_update_paths: [
            "/name",
            "/description",
            "/datasetIds",
            "/glossaryIds",
            "/readMe",
            "/keyContacts",
            "/tags",
            "/apiConsumableDatasetIds",
          ],
          max_update_operations: 8,
          fields: {
            dataset_refs: { mode: "rw", writable_via: "rest-patch", partial_update: false, normalized_by_target: false },
            description: { mode: "rw", writable_via: "rest-patch", partial_update: true, normalized_by_target: false },
            documentation: { mode: "rw", writable_via: "rest-patch", partial_update: true, normalized_by_target: true },
            glossary_term_refs: { mode: "na", writable_via: null, partial_update: true, normalized_by_target: false },
            name: { mode: "rw", writable_via: "rest-patch", partial_update: true, normalized_by_target: false },
            owners: { mode: "rw", writable_via: "rest-patch", partial_update: false, normalized_by_target: true },
            placement: { mode: "ro", writable_via: null, partial_update: true, normalized_by_target: false },
            status: { mode: "ro", writable_via: null, partial_update: true, normalized_by_target: false },
            tags: { mode: "rw", writable_via: "rest-patch", partial_update: false, normalized_by_target: true },
          },
        },
        dataset: {
          supported: true,
          identity_keys: ["secure_qri", "id", "resource_id"],
          supports_events: false,
          allowed_update_paths: null,
          max_update_operations: null,
          fields: {
            asset_type: { mode: "ro", writable_via: null, partial_update: true, normalized_by_target: false },
            classifications: { mode: "ro", writable_via: null, partial_update: true, normalized_by_target: false },
            description: { mode: "ro", writable_via: null, partial_update: true, normalized_by_target: false },
            glossary_term_refs: { mode: "na", writable_via: null, partial_update: true, normalized_by_target: false },
            name: { mode: "ro", writable_via: null, partial_update: true, normalized_by_target: false },
            owners: { mode: "ro", writable_via: null, partial_update: true, normalized_by_target: false },
            physical_ref: { mode: "ro", writable_via: null, partial_update: true, normalized_by_target: false },
            tags: { mode: "ro", writable_via: null, partial_update: true, normalized_by_target: false },
          },
        },
        glossary_term: { supported: false, identity_keys: [], supports_events: false, allowed_update_paths: null, max_update_operations: null, fields: {} },
        category: { supported: false, identity_keys: [], supports_events: false, allowed_update_paths: null, max_update_operations: null, fields: {} },
      },
    },
    config_schema: {
      additionalProperties: false,
      description:
        "Configuration for one Qlik Cloud tenant endpoint.\n\n``base_url`` is the tenant's own base, e.g. ``https://acme.eu.qlikcloud.com`` —\nnever\ninclude ``/api/v1``, ``/api/data-governance`` or ``/oauth/token``; the connector\nappends those itself. OAuth2 client-credentials (machine-to-machine) is the only auth\nmethod T3.1 wires up: RS-02 section 3.2 recommends it over long-lived API keys for\nbackend integrations, and it is what lets the connector run unattended.",
      properties: {
        base_url: { minLength: 1, title: "Base Url", type: "string" },
        client_id: { minLength: 1, title: "Client Id", type: "string" },
        scope: {
          anyOf: [{ type: "string" }, { type: "null" }],
          default: "user_default",
          title: "Scope",
        },
        space_id: { minLength: 1, title: "Space Id", type: "string" },
      },
      required: ["base_url", "client_id", "space_id"],
      title: "QlikConfig",
      type: "object",
    },
    config_secret_fields: ["client_secret"],
    manifest_unavailable_reason: null,
    distribution: null,
    broken_stage: null,
    broken_reason: null,
  },
];

/** The recorded `databricks` entry alone -- most tests only need one connector on screen. */
export const RECORDED_DATABRICKS_CONNECTOR: ConnectorInfo = RECORDED_CONNECTORS[0]!;

/** The recorded `qlik` entry alone. */
export const RECORDED_QLIK_CONNECTOR: ConnectorInfo = RECORDED_CONNECTORS[1]!;
