---
type: "Research Output"
title: "Reference Implementations — Data-Product Sync & Catalog Metadata Automation"
description: "Annotated shortlist of focused GitHub projects implementing data-product sync, automated catalog metadata creation, or metadata maintenance, with patterns to borrow."
tags: ["research", "RS-07", "reference-projects", "github", "data-products"]
timestamp: "2026-08-06T10:00:00Z"
status: "draft"
---

# Reference Implementations — Data-Product Sync & Catalog Metadata Automation

This memo captures focused, non-monolithic GitHub projects that already do one of the three
things QLabs Catalog Sync needs to do: (a) sync data-product / catalog metadata between systems,
(b) automatically create/populate catalog metadata via API, or (c) maintain metadata
programmatically (upsert/reconcile descriptions, tags, glossary terms, owners). Every repository
below was confirmed to exist via the GitHub API or by fetching its page. Where a star count was
fetched directly it is given as an exact number; where it was not fetched, the count is described
qualitatively and flagged. "Last activity" reflects the most recent push observed on 2026-08-06.

A recurring caveat applies to the whole list: several of the closest-fit repositories are recent,
single-author proofs-of-concept with few stars and no tests or license. They are valuable as
*pattern* references, not as dependencies.

## Data contract / spec tooling

These treat a machine-readable contract/descriptor as the metadata carrier — useful as the
canonical intermediate representation QLabs would translate to and from each catalog.

- **datacontract/datacontract-cli** — https://github.com/datacontract/datacontract-cli — Python —
  ~926 stars — very active (pushed 2026-08-05).
  What it does: a CLI/library to lint, test and, importantly, **import from and export to** many
  systems (Databricks/Unity Catalog, dbt, Snowflake, BigQuery, Avro/JSON schema, SQL, and more)
  around a YAML data-contract document. Exemplifies (b) and (c): it can read a live schema and
  reconcile it against a contract, and generate artifacts for target systems.
  What to borrow: the **import/export "adapter" architecture** (one small module per source/target
  format), the contract-as-single-source model, and the "read live schema, diff against desired
  state" test loop. This is the closest thing to a reusable translation core.
  Caveats: heavier dependency surface; contract-testing focus, not two-way live catalog sync.

- **bitol-io/open-data-contract-standard (ODCS)** — https://github.com/bitol-io/open-data-contract-standard —
  spec (docs/schema, Shell/Ruby tooling) — ~851 stars — Apache-2.0 — very active, LF AI & Data
  project, v3.1.0 (Dec 2025).
  What it does: defines the ODCS schema for data contracts (schema, quality, SLA, team, roles,
  servers/infrastructure). Not code to run, but the standard the tooling above targets.
  What to borrow: adopt (or map onto) the ODCS object model as QLabs' **canonical metadata model**
  so that "Databricks fields" and "Qlik fields" both normalize to one vocabulary.
  Caveats: it is a spec; you still build the sync engine.

- **Accenture/odps-python** — https://github.com/Accenture/odps-python — Python — ~15 stars —
  Apache-2.0 — moderate activity (pushed 2025-11), published on PyPI.
  What it does: a library to create, validate and serialize Open Data Product Specification (ODPS)
  v4.x documents, with ISO/RFC field validation, JSON/YAML round-trip, and caching.
  What to borrow: the **model + validate + serialize** pattern (typed models, pluggable validators,
  JSON/YAML I/O) for QLabs' internal data-product object. A clean, small template for the metadata
  layer even if ODPS is not the chosen carrier.
  Caveats: spec-modeling only; no catalog connectivity.

- **NextLab-SRL/dc43** — https://github.com/NextLab-SRL/dc43 — Python — small/recent (created
  2025-09, pushed 2026-07), low star count (not individually fetched).
  What it does: a library to apply Bitol ODCS contracts inside data pipelines (Databricks, Spark),
  i.e. enforce a contract at read/write time.
  What to borrow: concrete example of wiring an ODCS contract into a **Databricks/Spark** runtime —
  useful for the Databricks endpoint's "validate then apply" step.
  Caveats: young project; narrow to the pipeline-enforcement use case.

- **datamesh-architecture/dataproduct-specification** — https://github.com/datamesh-architecture/dataproduct-specification —
  spec + examples — modest stars — last pushed 2025-01.
  What it does: a lightweight YAML data-product descriptor (id, owner, output ports, etc.).
  What to borrow: a minimal descriptor shape if ODCS/ODPS feel too heavy for a first cut.
  Caveats: light governance semantics; slower cadence.

## Vendor API automation

Single-vendor drivers and scripts — reference material for each concrete endpoint QLabs must speak.

- **qlik-oss/qlik-cli** — https://github.com/qlik-oss/qlik-cli — Go — ~41 stars — MIT — active
  (pushed 2026-07), official Qlik OSS.
  What it does: the official `qlik` CLI exposing all Qlik Cloud public APIs (contexts, auth via
  API keys, and access to data assets / glossary / catalog endpoints).
  What to borrow: use it as the **Qlik endpoint driver** (shell out or mirror its API calls), and
  study its context/credential model for multi-tenant auth. Exemplifies (b).
  Caveats: the repo is a release mirror/placeholder; the API surface is documented at qlik.dev.

- **agile-lab-dev/witboost-collibra-python-data-catalog-plugin** —
  https://github.com/agile-lab-dev/witboost-collibra-python-data-catalog-plugin — Python — low stars —
  pushed 2026-03.
  What it does: a Collibra "data catalog plugin" that provisions/updates data-product metadata into
  Collibra as part of the Witboost platform. Exemplifies (b) and (c).
  What to borrow: the **provision / update / unprovision** plugin interface — an explicit,
  idempotent contract for pushing a data product's metadata into a catalog and reconciling it. This
  interface shape maps directly onto a QLabs "endpoint" abstraction.
  Caveats: designed to plug into Witboost; extract the interface idea, not the whole runtime.

- **akagrv/collibra-metadata-ingestion** — https://github.com/akagrv/collibra-metadata-ingestion —
  Python — low stars — stale (pushed 2020).
  What it does: scripts ingesting metadata from Redshift/Aurora/Oracle into Collibra via the
  Collibra REST API. Exemplifies (b).
  What to borrow: concrete **Collibra REST upsert** call patterns (assets, attributes, relations).
  Caveats: old (2020); API may have drifted — treat as illustrative only.

- **jmbenedetto/qlikcloud_automation** — https://github.com/jmbenedetto/qlikcloud_automation —
  Python — very low stars — pushed 2024-02.
  What it does: exploratory scripts against Qlik Cloud APIs and the CLI.
  What to borrow: concrete **Qlik Cloud API auth + request** snippets to accelerate the Qlik driver.
  Caveats: explicitly experimental; not a library.

## Catalog-to-catalog sync

The most directly on-topic group: projects that move metadata between two systems. Note that none
of them do Databricks <-> Qlik, and most are one-directional or single-author.

- **jincejames/UnitySync** — https://github.com/jincejames/UnitySync — Python — 0 stars — single
  commit (2025-11), no license.
  What it does: four scripts performing **full and delta metadata sync in both directions** between
  AWS Glue and Databricks Unity Catalog (`full_metadata_sync_glue_unity`,
  `delta_metadata_sync_glue_unity`, and the two reverse-direction variants). Exemplifies (a).
  What to borrow: the **explicit matrix of {full | delta} x {direction}** as separate, legible code
  paths, and the bidirectional-sync skeleton. This is the closest structural analog to QLabs'
  two-way bridge, at toy scale.
  Caveats: proof-of-concept, one commit, no tests/license — a pattern, not a base to fork.

- **awslabs/aws-glue-catalog-sync-agent-for-hive** —
  https://github.com/awslabs/aws-glue-catalog-sync-agent-for-hive — Java — ~35 stars — Apache-2.0 —
  mature (pushed 2023-12), AWS Labs.
  What it does: a Hive Metastore event listener that captures create/drop table/partition events
  onto a durable in-memory queue and a separate thread drains it to the AWS Glue Data Catalog
  (via Athena JDBC), tolerating disconnects. Exemplifies (a) one-way.
  What to borrow: the **event-listener + durable queue + drain/replay** design for resilient sync,
  plus idempotent "create-if-missing / drop-if-exists" handling and a suppress-drops safety flag.
  Directly relevant to QLabs' change-capture and retry story.
  Caveats: Java/Hive-specific; one-directional; DDL-level (not descriptions/tags/glossary).

- **brock-acryl/databricks-datahub-file-sync** —
  https://github.com/brock-acryl/databricks-datahub-file-sync — Python — very low stars (new,
  pushed 2026-08).
  What it does: ingests Databricks Unity Catalog metadata into DataHub Cloud by running the DataHub
  Databricks source with a **file sink** inside the workspace, retrieving the file via the
  Databricks Files API, then replaying it into DataHub from an egress-capable host. Exemplifies (a).
  What to borrow: the **"extract to a portable file, transport, then replay to target"** decoupling
  — a clean answer for restricted-network environments and for making sync steps independently
  retryable/auditable.
  Caveats: brand new; example-grade.

- **cwgdata/uc-iceberg-rest-sync** — https://github.com/cwgdata/uc-iceberg-rest-sync — Python — very
  low stars — pushed 2026-05.
  What it does: a Databricks App that replicates Iceberg tables from an external REST catalog
  (Polaris / Snowflake Polaris / Tabular) into Unity Catalog using the metadata-file-location
  preview. Exemplifies (a).
  What to borrow: pattern for **reading an external catalog's metadata and materializing it into
  UC** — relevant when Snowflake/Polaris become QLabs endpoints.
  Caveats: preview-feature dependent; single-author; narrow.

- **alexane-rose/data-governance-toolkit** — https://github.com/alexane-rose/data-governance-toolkit —
  Python — very low stars — new (pushed 2025-11).
  What it does: a toolkit to sync catalogs and manage metadata across Unity Catalog, Alation and
  Databricks. Exemplifies (a) and (c).
  What to borrow: the idea of a **single toolkit spanning multiple governance catalogs** with shared
  metadata operations — a structural sketch for QLabs' multi-endpoint scope.
  Caveats: brand-new and unproven; verify the code does what the description claims before relying on it.

- **Bhushan-Khachane/sourcedesc-ai** — https://github.com/Bhushan-Khachane/sourcedesc-ai — Python —
  very low stars — new (pushed 2026-07).
  What it does: an LLM-driven pipeline that auto-generates business descriptions, tags and PII
  classifications and **syncs them to Microsoft Purview and Databricks Unity Catalog**.
  Exemplifies (b) and (c).
  What to borrow: the **enrich-then-upsert** flow (generate/augment metadata, then push and reconcile
  into one or more catalogs) — useful if QLabs adds AI-assisted description/tag maintenance.
  Caveats: new; AI-enrichment focus may exceed QLabs' initial scope.

## Connector patterns (platforms, reference-only)

Individual connectors/plugins from larger platforms — included only to illustrate the
ingest/upsert pattern, not to adopt the whole platform.

- **agile-lab-dev/witboost-openmetadata-data-catalog-plugin** —
  https://github.com/agile-lab-dev/witboost-openmetadata-data-catalog-plugin — low stars — pushed
  2025-06.
  Pattern to study: a self-contained plugin that provisions data-product metadata into OpenMetadata
  through the same provision/update/unprovision contract as the Collibra plugin above — evidence
  that a single catalog-plugin interface can be reused across catalogs.

- **datacontract/open-data-contract-standard-python** —
  https://github.com/datacontract/open-data-contract-standard-python — Python — low stars — pushed
  2025-12.
  Pattern to study: a minimal, well-scoped library to read/write ODCS YAML — a drop-in
  (de)serialization reference if ODCS is chosen as the carrier, complementing odps-python.

## Data-product frameworks

Broader frameworks kept brief; useful mainly for architecture, not wholesale reuse.

- **agile-lab-dev/witboost-starter-kit** — https://github.com/agile-lab-dev/witboost-starter-kit —
  multi-repo kit — active (pushed 2026-07).
  What to borrow: the **"tech adapter" + "data catalog plugin" separation** — provisioning to a
  compute/storage system is one adapter type; publishing metadata to a catalog (Collibra,
  OpenMetadata) is another, behind a common lifecycle (provision/validate/unprovision). The org
  ships adapters for Databricks, AWS Glue, BigQuery, Snowflake and more, so it is a rich catalogue
  of endpoint-integration patterns that mirror QLabs' endpoint concept almost one-to-one.
  Caveats: tied to the Witboost platform; mine it for structure, not as a dependency.

- **opendatamesh-initiative/odm-specification-dpdescriptor** (+ `-parser`) —
  https://github.com/opendatamesh-initiative/odm-specification-dpdescriptor and
  https://github.com/opendatamesh-initiative/odm-specification-dpdescriptor-parser — spec + Java
  parser — moderate activity.
  What to borrow: a richer Data Product Descriptor (DPDS) model and a reference parser, if the ODCS
  model proves too data-contract-centric for full data-product semantics (ports, promises, infra).

- **agile-lab-dev/Data-Product-Specification** —
  https://github.com/agile-lab-dev/Data-Product-Specification — spec — modest stars — pushed 2025-09.
  Reference only: an early open data-mesh data-product spec; useful for vocabulary comparison.

## Top picks to study first

1. **datacontract/datacontract-cli** — the import/export adapter architecture and contract-as-source
   model are the most directly reusable ideas for QLabs' translation core.
2. **agile-lab-dev/witboost-starter-kit** (with its Collibra and OpenMetadata catalog plugins) — the
   adapter/plugin lifecycle is the closest structural analog to QLabs' multi-catalog endpoints;
   study the provision/update/unprovision contract.
3. **jincejames/UnitySync** — the smallest concrete example of two-way, full-and-delta metadata sync;
   read it for the direction/mode matrix, then build something sturdier.
4. **awslabs/aws-glue-catalog-sync-agent-for-hive** — the event-listener + durable queue + replay
   pattern is the reference for resilient, idempotent change propagation.
5. **bitol-io/open-data-contract-standard + Accenture/odps-python** — the canonical metadata carrier
   plus a clean model/validate/serialize implementation to base QLabs' internal object on.

## What is genuinely missing in the ecosystem

There is no off-the-shelf, focused project that does **two-way, data-product-level metadata sync
between Databricks and Qlik** (nor between Databricks and Snowflake/Collibra as a symmetric bridge).
The prior art clusters into three shapes, none of which is the QLabs target: (1) *specs/standards*
(ODCS, ODPS, DPDS) that define a carrier but ship no engine; (2) *one-directional ingestion
connectors* that pull metadata into a governance platform (DataHub, OpenMetadata, Collibra,
Purview); and (3) *single-vendor automation* (qlik-cli, Unity Catalog scripts, the AWS Hive->Glue
agent). The handful of "catalog-to-catalog sync" repositories that exist are recent, single-author
proofs-of-concept, almost always one-directional, and centered on Glue/Unity Catalog rather than a
BI-plus-lakehouse pair. Bidirectional sync with conflict detection/resolution at the data-product
level is effectively greenfield — QLabs should expect to assemble it from the *patterns* above
(canonical model + per-endpoint adapters + change-capture/queue/replay + idempotent upsert) rather
than fork an existing bridge.

# Citations

- https://github.com/datacontract/datacontract-cli — Data Contract CLI; import/export adapters and contract testing (Python, ~926 stars).
- https://github.com/bitol-io/open-data-contract-standard — Open Data Contract Standard (ODCS) spec, LF AI & Data (~851 stars, Apache-2.0).
- https://github.com/Accenture/odps-python — Python library for the Open Data Product Specification (ODPS) v4.x (~15 stars, Apache-2.0).
- https://github.com/NextLab-SRL/dc43 — Library applying ODCS contracts in Databricks/Spark pipelines (Python).
- https://github.com/datamesh-architecture/dataproduct-specification — Lightweight YAML data-product descriptor spec.
- https://github.com/qlik-oss/qlik-cli — Official Qlik Cloud CLI exposing all public APIs (Go, ~41 stars, MIT).
- https://github.com/agile-lab-dev/witboost-collibra-python-data-catalog-plugin — Collibra data-catalog provisioning plugin (Python).
- https://github.com/akagrv/collibra-metadata-ingestion — Collibra REST API metadata ingestion scripts (Python, stale 2020).
- https://github.com/jmbenedetto/qlikcloud_automation — Exploratory Qlik Cloud API/CLI automation scripts (Python).
- https://github.com/jincejames/UnitySync — Bidirectional full/delta metadata sync between AWS Glue and Databricks Unity Catalog (Python, 0 stars, single commit).
- https://github.com/awslabs/aws-glue-catalog-sync-agent-for-hive — Hive Metastore -> AWS Glue Data Catalog event sync agent (Java, ~35 stars, Apache-2.0).
- https://github.com/brock-acryl/databricks-datahub-file-sync — Unity Catalog metadata into DataHub via file sink + Files API (Python, new).
- https://github.com/cwgdata/uc-iceberg-rest-sync — Replicate Iceberg tables from external REST catalog into Unity Catalog (Python).
- https://github.com/alexane-rose/data-governance-toolkit — Toolkit to sync/manage metadata across Unity Catalog, Alation, Databricks (Python, new).
- https://github.com/Bhushan-Khachane/sourcedesc-ai — LLM metadata enrichment synced to Microsoft Purview and Databricks Unity Catalog (Python, new).
- https://github.com/agile-lab-dev/witboost-openmetadata-data-catalog-plugin — OpenMetadata data-catalog provisioning plugin (reference for the catalog-plugin interface).
- https://github.com/datacontract/open-data-contract-standard-python — Python library to read/write ODCS YAML.
- https://github.com/agile-lab-dev/witboost-starter-kit — Witboost tech-adapter + data-catalog-plugin architecture across many endpoints.
- https://github.com/opendatamesh-initiative/odm-specification-dpdescriptor — Data Product Descriptor Specification (DPDS).
- https://github.com/opendatamesh-initiative/odm-specification-dpdescriptor-parser — Reference parser for DPDS.
- https://github.com/agile-lab-dev/Data-Product-Specification — Early open data-mesh data-product specification.
- https://qlik.dev/toolkits/qlik-cli/ — Qlik CLI documentation (Qlik Cloud public API surface).
- https://opendataproducts.org/ — Open Data Product Specification (ODPS) home, referenced by odps-python.
