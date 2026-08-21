// Responses recorded VERBATIM from a running engine, so this feature's tests can be driven by
// what the service actually sends rather than only by what a fixture author believed it sends.
//
// How they were produced: `qlabs-catalog-sync serve` against a real SQLite state store, with a
// real `databricks` connector pointed at a throwaway HTTPS stand-in for a Unity Catalog
// workspace (catalogs -> schemas -> tables). Nothing about the engine was stubbed -- the real
// selection evaluator, the real source-tree walk, the real routes. Only the tenant was fake.
// The rule set is C3's own worked example plus the two cases that matter most here: a
// per-object override that beats every rule, and a `tag` rule against a source whose manifest
// declares `tags` as `na` (RM-01 D6: no SQL warehouse configured), which is what produces a
// real, non-zero `undetermined`.
//
// Every constant is annotated with its generated type, NOT cast to it -- so the day the API
// changes shape, this file stops compiling instead of quietly disagreeing with the running
// service at exactly the point under test. `SelectionScreen.recorded.test.tsx` replays these
// through the real `apiClient`.
//
// Only two edits were made to the captured bytes, both subtractive and neither touching a
// field: the schema/dataset node lists were filtered to two entries each, and the preview
// sample to its first two, so the file stays readable.
import type {
  EndpointManifestOut,
  PreviewOut,
  SelectionOverrideOut,
  SelectionRuleOut,
  SourceTreePageOut,
} from "./selectionApi";

export const RECORDED_SOURCE_TREE_OBJECT: SourceTreePageOut = {
  "nodes": [
    {
      "scope": "object",
      "object_id": "schema-id-analytics-sales",
      "qualified_name": "analytics.sales",
      "display_name": "analytics.sales",
      "result": {
        "decision": "include",
        "included": true,
        "source": "rule",
        "rule_id": "2516d236-02a1-4bb4-ab56-feb3c9c6eff5",
        "explain": "included by rule #0 include glob 'analytics.*'; 1 rule(s) undetermined: rule #4 exclude tag 'pii' could not be evaluated: tags unknown",
        "undetermined": [
          {
            "matcher_kind": "tag",
            "pattern": "pii",
            "decision": "exclude",
            "missing": "tags",
            "explain": "rule #4 exclude tag 'pii' could not be evaluated: tags unknown"
          }
        ]
      }
    },
    {
      "scope": "object",
      "object_id": "schema-id-finance-reporting",
      "qualified_name": "finance.reporting",
      "display_name": "finance.reporting",
      "result": {
        "decision": "include",
        "included": true,
        "source": "override",
        "rule_id": null,
        "explain": "included by override include 'finance.reporting' (pinned from the source tree)",
        "undetermined": []
      }
    }
  ],
  "offset": 0,
  "limit": 200,
  "has_more": false,
  "next_offset": null
};

export const RECORDED_SOURCE_TREE_DATASET: SourceTreePageOut = {
  "nodes": [
    {
      "scope": "dataset",
      "object_id": "table-id-analytics-sales-orders",
      "qualified_name": "analytics.sales.orders",
      "display_name": "analytics.sales.orders",
      "selection": {
        "included": true,
        "explain": "included by rule #0 include glob 'analytics.sales.*'",
        "parent": {
          "decision": "include",
          "included": true,
          "source": "rule",
          "rule_id": "2516d236-02a1-4bb4-ab56-feb3c9c6eff5",
          "explain": "included by rule #0 include glob 'analytics.*'; 1 rule(s) undetermined: rule #4 exclude tag 'pii' could not be evaluated: tags unknown",
          "undetermined": [
            {
              "matcher_kind": "tag",
              "pattern": "pii",
              "decision": "exclude",
              "missing": "tags",
              "explain": "rule #4 exclude tag 'pii' could not be evaluated: tags unknown"
            }
          ]
        },
        "dataset": {
          "decision": "include",
          "included": true,
          "source": "rule",
          "rule_id": "5baec133-8acf-4616-957b-a6fa35ca6505",
          "explain": "included by rule #0 include glob 'analytics.sales.*'",
          "undetermined": []
        }
      }
    },
    {
      "scope": "dataset",
      "object_id": "table-id-finance-reporting-ledger",
      "qualified_name": "finance.reporting.ledger",
      "display_name": "finance.reporting.ledger",
      "selection": {
        "included": true,
        "explain": "included: inherits parent schema's selection (included by override include 'finance.reporting' (pinned from the source tree))",
        "parent": {
          "decision": "include",
          "included": true,
          "source": "override",
          "rule_id": null,
          "explain": "included by override include 'finance.reporting' (pinned from the source tree)",
          "undetermined": []
        },
        "dataset": {
          "decision": "exclude",
          "included": false,
          "source": "default",
          "rule_id": null,
          "explain": "excluded by default (no rule matched)",
          "undetermined": []
        }
      }
    }
  ],
  "offset": 0,
  "limit": 200,
  "has_more": false,
  "next_offset": null
};

export const RECORDED_PREVIEW: PreviewOut = {
  "rule_set_source": "stored",
  "counts": {
    "object": {
      "total": 4,
      "included": 3,
      "excluded": 1,
      "undetermined": 3
    },
    "dataset": {
      "total": 5,
      "included": 4,
      "excluded": 1,
      "undetermined": 4
    }
  },
  "sample": [
    {
      "scope": "object",
      "object_id": "schema-id-analytics-prod_staging",
      "qualified_name": "analytics.prod_staging",
      "display_name": "analytics.prod_staging",
      "included": true,
      "explain": "included by rule #3 include glob 'analytics.prod_staging'; 1 rule(s) undetermined: rule #4 exclude tag 'pii' could not be evaluated: tags unknown",
      "rule_id": "c8b348e7-0044-4220-9f78-9996ce8daf70",
      "has_undetermined": true
    },
    {
      "scope": "object",
      "object_id": "schema-id-analytics-sales",
      "qualified_name": "analytics.sales",
      "display_name": "analytics.sales",
      "included": true,
      "explain": "included by rule #0 include glob 'analytics.*'; 1 rule(s) undetermined: rule #4 exclude tag 'pii' could not be evaluated: tags unknown",
      "rule_id": "2516d236-02a1-4bb4-ab56-feb3c9c6eff5",
      "has_undetermined": true
    }
  ],
  "candidates_examined": 9,
  "truncated": false
};

export const RECORDED_RULES_OBJECT: SelectionRuleOut[] = [
  {
    "id": "2516d236-02a1-4bb4-ab56-feb3c9c6eff5",
    "pair_id": "e5fd6597-7b6b-4c27-98a1-621da2b6c291",
    "ordinal": 0,
    "scope": "object",
    "decision": "include",
    "matcher_kind": "glob",
    "pattern": "analytics.*",
    "created_at": "2026-08-21T06:57:09.772507Z",
    "updated_at": "2026-08-21T06:57:09.772507Z"
  },
  {
    "id": "373ad38b-7061-4a03-8bc6-c41703e4eff7",
    "pair_id": "e5fd6597-7b6b-4c27-98a1-621da2b6c291",
    "ordinal": 1,
    "scope": "object",
    "decision": "exclude",
    "matcher_kind": "glob",
    "pattern": "analytics.staging",
    "created_at": "2026-08-21T06:57:09.792967Z",
    "updated_at": "2026-08-21T06:57:09.792967Z"
  },
  {
    "id": "ab6f7d7d-540c-4f37-af7d-9ce4f5274b5e",
    "pair_id": "e5fd6597-7b6b-4c27-98a1-621da2b6c291",
    "ordinal": 2,
    "scope": "object",
    "decision": "exclude",
    "matcher_kind": "glob",
    "pattern": "analytics.prod_staging",
    "created_at": "2026-08-21T06:58:14.620914Z",
    "updated_at": "2026-08-21T06:58:14.620914Z"
  },
  {
    "id": "c8b348e7-0044-4220-9f78-9996ce8daf70",
    "pair_id": "e5fd6597-7b6b-4c27-98a1-621da2b6c291",
    "ordinal": 3,
    "scope": "object",
    "decision": "include",
    "matcher_kind": "glob",
    "pattern": "analytics.prod_staging",
    "created_at": "2026-08-21T06:57:09.819969Z",
    "updated_at": "2026-08-21T06:57:09.819969Z"
  },
  {
    "id": "4984f092-e7cc-4b79-8b20-a753a66794e3",
    "pair_id": "e5fd6597-7b6b-4c27-98a1-621da2b6c291",
    "ordinal": 4,
    "scope": "object",
    "decision": "exclude",
    "matcher_kind": "tag",
    "pattern": "pii",
    "created_at": "2026-08-21T06:58:59.319591Z",
    "updated_at": "2026-08-21T06:58:59.319591Z"
  }
];

export const RECORDED_RULES_DATASET: SelectionRuleOut[] = [
  {
    "id": "5baec133-8acf-4616-957b-a6fa35ca6505",
    "pair_id": "e5fd6597-7b6b-4c27-98a1-621da2b6c291",
    "ordinal": 0,
    "scope": "dataset",
    "decision": "include",
    "matcher_kind": "glob",
    "pattern": "analytics.sales.*",
    "created_at": "2026-08-21T06:57:09.844784Z",
    "updated_at": "2026-08-21T06:57:09.844784Z"
  }
];

export const RECORDED_OVERRIDES_OBJECT: SelectionOverrideOut[] = [
  {
    "id": "0bd31ec2-443f-4613-a656-1c3f465c1ace",
    "pair_id": "e5fd6597-7b6b-4c27-98a1-621da2b6c291",
    "scope": "object",
    "object_id": "finance.reporting",
    "decision": "include",
    "reason": "pinned from the source tree",
    "created_at": "2026-08-21T06:59:16.156830Z",
    "updated_at": "2026-08-21T06:59:16.156830Z"
  }
];

export const RECORDED_MANIFEST: EndpointManifestOut = {
  "endpoint": "databricks_probe",
  "manifest": {
    "concurrency": "none",
    "entities": {
      "data_product": {
        "supported": true,
        "identity_keys": [
          "full_name",
          "object_id"
        ],
        "supports_events": false,
        "allowed_update_paths": null,
        "max_update_operations": null,
        "fields": {
          "dataset_refs": {
            "mode": "ro",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "description": {
            "mode": "ro",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "documentation": {
            "mode": "na",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "glossary_term_refs": {
            "mode": "na",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "name": {
            "mode": "ro",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "owners": {
            "mode": "ro",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "placement": {
            "mode": "na",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "status": {
            "mode": "na",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "tags": {
            "mode": "na",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          }
        }
      },
      "dataset": {
        "supported": true,
        "identity_keys": [
          "full_name",
          "object_id"
        ],
        "supports_events": false,
        "allowed_update_paths": null,
        "max_update_operations": null,
        "fields": {
          "asset_type": {
            "mode": "ro",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "classifications": {
            "mode": "na",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "description": {
            "mode": "ro",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "glossary_term_refs": {
            "mode": "na",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "name": {
            "mode": "ro",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "owners": {
            "mode": "ro",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "physical_ref": {
            "mode": "ro",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          },
          "tags": {
            "mode": "na",
            "writable_via": null,
            "partial_update": true,
            "normalized_by_target": false
          }
        }
      },
      "glossary_term": {
        "supported": false,
        "identity_keys": [],
        "supports_events": false,
        "allowed_update_paths": null,
        "max_update_operations": null,
        "fields": {}
      },
      "category": {
        "supported": false,
        "identity_keys": [],
        "supports_events": false,
        "allowed_update_paths": null,
        "max_update_operations": null,
        "fields": {}
      }
    }
  },
  "unavailable_reason": null
};

