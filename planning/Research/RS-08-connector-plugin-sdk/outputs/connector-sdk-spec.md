---
type: "Research Output"
title: "Connector Plugin SDK — Design Specification (v1)"
description: "A Python SDK and entry-point plugin framework that makes each catalog an independently packaged, versioned, auto-discovered connector, so new endpoints ship without core changes."
tags: ["research", "RS-08", "connector-sdk", "plugin", "extensibility", "python"]
timestamp: "2026-08-06T11:00:00Z"
status: "draft"
---

# Connector Plugin SDK — Design Specification (v1)

This specifies how catalog endpoints (Databricks, Qlik, Snowflake, Collibra, and future systems)
are built as **plugins on a shared SDK**. Each connector is a separate, pip-installable Python
package that implements a stable, versioned contract and is auto-discovered by the core engine via
Python entry points. The goal is that adding a new catalog means writing a new package against the
SDK — never editing the engine. This formalizes the adapter contract sketched in the RS-07
architecture and binds to the RS-03 neutral model. It is the design basis for RM-03 (pluggable
endpoint framework).

Decisions taken (per project scoping): **in-process plugins discovered through Python entry points**,
and an SDK built **internal-first but public-ready** — a documented, semver-versioned contract that
can be opened to third parties later without breaking changes.

## 1. Goals and principles

1. **Plugin, not fork.** A connector is an external package; the engine discovers it at runtime. No
   engine change is required to add, remove, or upgrade a connector.
2. **One stable contract.** Connectors depend only on the SDK package, never on engine internals.
   The SDK is the public surface; the engine is private.
3. **Capability-driven.** A connector declares what it supports (entities, per-field read/write,
   partial-update, events, identity keys). The engine adapts to declared capabilities instead of
   assuming a uniform feature set.
4. **Batteries included.** The SDK ships the shared plumbing (HTTP client, retry/backoff, pagination,
   auth base, envelope/checksum helpers, config base, testing kit) so a connector is mostly mapping
   logic.
5. **Versioned and compatible.** The SDK carries an explicit contract version; the engine refuses to
   load connectors built against an incompatible major.
6. **Public-ready hygiene from day one.** Semver, deprecation policy, a conformance test kit, and a
   documented trust model — even while connectors are internal.

## 2. Package topology

Three package roles, each independently versioned and releasable:

```
qlabs-catalog-sync-sdk      # the contract + shared helpers + testing kit (public surface)
qlabs-catalog-sync          # the engine/core: discovery, sync loop, state store, scheduler
qlabs-connector-databricks  # a connector plugin package
qlabs-connector-qlik        # a connector plugin package
qlabs-connector-snowflake   # (later)
qlabs-connector-collibra    # (later)
```

- **SDK** depends on nothing from the engine. It re-exports the neutral model types (from RS-03) so a
  connector imports `DataProduct`, `GlossaryTerm`, `FieldEnvelope`, etc. from one place.
- **Engine** depends on the SDK and discovers connectors at runtime; it never imports a connector
  directly.
- **Connectors** depend only on the SDK (plus their own vendor libraries, e.g. `databricks-sdk`).

This lets a connector be built, tested, versioned, and shipped on its own cadence, and lets the SDK
evolve under semver without dragging the engine or every connector along.

## 3. Plugin discovery via entry points

Connectors advertise themselves under a single entry-point group. In a connector's `pyproject.toml`:

```
[project.entry-points."qlabs_catalog_sync.connectors"]
databricks = "qlabs_connector_databricks:DatabricksConnector"
```

The engine enumerates the group at startup using the standard library:

```
from importlib.metadata import entry_points

def discover_connectors() -> dict[str, type["Connector"]]:
    found = {}
    for ep in entry_points(group="qlabs_catalog_sync.connectors"):
        cls = ep.load()                     # imports the connector class
        check_contract_compatibility(cls)   # SDK major-version gate (section 7)
        found[ep.name] = cls
    return found
```

Properties:

- **Zero-config discovery.** Installing `pip install qlabs-connector-snowflake` makes the `snowflake`
  connector available; uninstalling removes it. No registry edit.
- **Name is the endpoint key.** The entry-point name (`databricks`, `qlik`) is the stable id used in
  config, the IdentityMap, and logs.
- **Lazy option.** `ep.load()` imports eagerly; for many installed-but-unused connectors the engine
  can defer `load()` until a config references the name.
- **Collision policy.** Two packages claiming the same name is a startup error listing both
  distributions.

## 4. The SDK surface

What a connector author imports and uses.

### 4.1 The Connector contract

An abstract base class (ABC) that mirrors the RS-07 interface, typed against the neutral model:

```
from abc import ABC, abstractmethod
from collections.abc import Iterable

class Connector(ABC):
    name: str                       # matches the entry-point name
    sdk_contract_version: int       # set by the SDK base; used for compat checks
    ConfigModel: type[ConnectorConfig]   # pydantic-settings model this connector needs

    @abstractmethod
    def capabilities(self) -> CapabilityManifest: ...

    @abstractmethod
    def setup(self, ctx: ConnectorContext) -> None: ...      # build clients, validate config

    @abstractmethod
    def healthcheck(self) -> HealthStatus: ...

    @abstractmethod
    def list_changed(self, entity_type: EntityType, since: Watermark) -> Iterable[ChangeRef]: ...

    @abstractmethod
    def read(self, ref: IdentityRef) -> NeutralEntity: ...   # returns entity with field envelopes

    @abstractmethod
    def create(self, entity: NeutralEntity) -> IdentityRef: ...

    @abstractmethod
    def update(self, ref: IdentityRef, diff: FieldDiff) -> WriteResult: ...

    @abstractmethod
    def delete(self, ref: IdentityRef) -> None: ...

    def close(self) -> None:         # optional: release clients/sessions
        ...
```

The engine only ever calls these methods. `list_changed` implements the poll model (RS-07); `read`
returns field envelopes (RS-03) so the engine can diff by checksum; `update` receives a minimal
`FieldDiff` and the connector translates it to the smallest native mutation.

### 4.2 The capability manifest

Declarative, machine-readable statement of what the connector supports. The engine reads it once and
plans accordingly.

```
class FieldCapability(BaseModel):
    mode: Literal["rw", "ro", "na"]
    writable_via: str | None = None          # e.g. "rest-patch", "sql-ddl"
    partial_update: bool = True              # False => read-modify-write full replace

class EntityCapability(BaseModel):
    supported: bool
    identity_keys: list[str]                 # e.g. ["secure_qri"], ["full_name","object_id"]
    fields: dict[str, FieldCapability]       # neutral field name -> capability
    supports_events: bool = False            # else engine polls

class CapabilityManifest(BaseModel):
    entities: dict[EntityType, EntityCapability]
    concurrency: Literal["etag", "revision", "none"] = "none"
```

This is where Qlik declares glossary terms fully RW with ETag concurrency and product arrays as
`partial_update=False`; where Databricks declares no native glossary (`na`) and SQL-DDL write paths;
and where a connector says `supports_events=False` so the engine polls. The engine never writes a
field a connector marks `ro`/`na`, and it uses `concurrency` to decide whether to send `if-match`.

### 4.3 Shared helpers (so connectors stay thin)

The SDK provides, and connectors are expected to reuse:

- `HttpEndpoint` — a configured `httpx` client wrapper with base URL, auth injection, timeouts,
  connection pooling.
- `retry()` / rate-limit handling — `tenacity`-based decorators honoring `429`/`Retry-After` and
  `5xx`, with jitter.
- `paginate()` — cursor/offset pagination helpers returning iterators.
- `AuthProvider` base — token acquisition/refresh (API key, OAuth2 M2M, JWT/key-pair), tokens held
  in memory, never persisted.
- Envelope + checksum utilities — canonical normalization so the same value hashes identically across
  connectors (stable diffing).
- `ConnectorConfig` (pydantic-settings base) — connectors subclass to declare their required config
  and secrets; the engine binds and injects.
- `ConnectorContext` — injected at `setup()`: resolved config, a structured logger bound with
  `endpoint`/`tenant`, a metrics handle, and a clock (for testability).
- Typed exceptions — `TransientError`, `AuthError`, `NotFound`, `ConflictError`, `CapabilityError` —
  so the engine reacts uniformly (retry vs skip vs fail).

## 5. Connector lifecycle

1. **Discover** — engine enumerates entry points, version-gates each class.
2. **Instantiate & configure** — for each endpoint in config, the engine builds the connector's
   `ConfigModel` (binding secrets), then constructs the connector.
3. **`setup(ctx)`** — connector builds vendor clients, validates config/credentials, resolves auth.
4. **`capabilities()`** — engine caches the manifest and plans field-level sync.
5. **`healthcheck()`** — pre-flight before each cycle (or on a schedule); a red status quarantines
   that endpoint without stopping others.
6. **Sync cycles** — engine calls `list_changed` -> `read` -> (diff) -> `update`/`create`/`delete`.
7. **`close()`** — on shutdown/reconfigure, connector releases sessions.

Connectors are stateless about *sync* — all cross-run state (IdentityMap, watermarks, envelopes)
lives in the engine's state store. A connector only holds live clients/sessions. This keeps
connectors simple and the engine authoritative and restart-safe.

## 6. Configuration and secrets

Each connector declares its config schema; the engine owns resolution and injection so connectors
never read the environment directly:

```
class QlikConfig(ConnectorConfig):
    tenant_url: str                     # https://<tenant>.<region>.qlikcloud.com
    auth: OAuth2M2M | ApiKey            # SDK-provided auth models
    poll_cadence_seconds: int = 600
```

The engine binds values (and secret references) from its config + secret backend (env, Vault, cloud
secret manager) and passes a validated instance in `ConnectorContext`. Secrets are redacted in logs
by the SDK logger and never written to the state store.

## 7. Versioning and compatibility

Public-ready means the contract is a managed API:

- The SDK exposes `SDK_CONTRACT_VERSION` (an integer major) plus a full semver package version.
- The `Connector` base stamps `sdk_contract_version` onto every connector class.
- At discovery, the engine rejects a connector whose contract major differs from what the engine
  supports, with a clear message (which package, which versions). This prevents a stale connector
  from silently misbehaving.
- **Semver policy:** additive, backward-compatible SDK changes bump minor; contract-breaking changes
  bump the contract major and are rare and announced. New optional capability fields default to
  "unsupported" so old connectors keep working.
- **Deprecation policy:** a deprecated SDK symbol warns for one minor cycle before removal; the
  conformance kit flags use of deprecated surface.

## 8. Capability negotiation in the engine

The engine turns manifests into a per-pair sync plan:

- Fields both endpoints mark `rw` are two-way candidates; a field `rw` on one side and `ro`/`na` on
  the other is one-way (or projected, e.g. glossary term -> tag on Databricks).
- `partial_update=False` fields trigger read-modify-write full-replacement in the writer.
- `concurrency` decides ETag/revision use on writes.
- Unsupported entity types are simply not scheduled for that endpoint.

This means the same engine handles a rich connector (Collibra) and a thin one (a future read-only
catalog) without special-casing — the manifest carries the differences.

## 9. Conformance test kit

The SDK ships a reusable pytest suite connector authors run to certify a connector before release:

- **Contract tests** — every abstract method implemented; `capabilities()` returns a valid manifest;
  declared identity keys are non-empty for supported entities.
- **Round-trip tests** — `create` then `read` returns equal envelopes; `update` of a `rw` field is
  reflected on re-`read`; `ro`/`na` fields are never mutated.
- **Idempotency** — re-applying an unchanged diff is a no-op (checksum stable).
- **HTTP behavior** — retries on `429`/`5xx`, respects `Retry-After`, sends `if-match` when
  `concurrency != none`. Uses `respx` for unit mocking and `vcrpy` cassettes for recorded real
  responses.
- **Capability honesty** — attempting to write a field the manifest marks `ro`/`na` raises
  `CapabilityError` rather than calling the API.

A connector that passes the kit is "certified" — the mechanism that lets third parties (later)
contribute connectors we can trust.

## 10. Scaffolding and developer experience

- A `qlabs connector new <name>` command (cookiecutter template) generates a connector package:
  `pyproject.toml` with the entry point pre-wired, a `Connector` subclass stub, a `ConfigModel`, and a
  conformance-test file.
- Connector docs are generated from the `CapabilityManifest`, so the capability matrix per catalog is
  always accurate and published.

## 11. Example: a minimal connector

```
# qlabs_connector_example/__init__.py
from qlabs_catalog_sync_sdk import (
    Connector, CapabilityManifest, EntityCapability, FieldCapability,
    EntityType, ConnectorConfig, HttpEndpoint, retry,
)

class ExampleConfig(ConnectorConfig):
    base_url: str
    api_key: str

class ExampleConnector(Connector):
    name = "example"
    ConfigModel = ExampleConfig

    def setup(self, ctx):
        self.http = HttpEndpoint(ctx.config.base_url, auth=("Bearer", ctx.config.api_key))
        self.log = ctx.logger

    def capabilities(self):
        return CapabilityManifest(entities={
            EntityType.DATASET: EntityCapability(
                supported=True,
                identity_keys=["id"],
                fields={
                    "name": FieldCapability(mode="rw", writable_via="rest-patch"),
                    "description": FieldCapability(mode="rw", writable_via="rest-patch"),
                    "tags": FieldCapability(mode="rw", partial_update=False),
                    "lineage": FieldCapability(mode="ro"),
                },
            )
        }, concurrency="etag")

    def healthcheck(self): ...
    def list_changed(self, entity_type, since): ...
    def read(self, ref): ...
    def create(self, entity): ...
    def update(self, ref, diff): ...
    def delete(self, ref): ...
```

```
# pyproject.toml
[project.entry-points."qlabs_catalog_sync.connectors"]
example = "qlabs_connector_example:ExampleConnector"
```

Installing this package is all it takes for the engine to discover and use `example`.

## 12. How the first two connectors map

- **Databricks** wraps `databricks-sdk` for UC REST and the Statement Execution API for SQL DDL; its
  manifest marks table/column description and tags `rw` via `sql-ddl`, container objects via
  `rest-patch`, glossary entities `na`, concurrency `none` (snapshot+checksum).
- **Qlik** wraps `httpx` against the tenant REST APIs; manifest marks data-product and glossary
  entities `rw`, product arrays `partial_update=False`, glossary writes `concurrency="etag"`, identity
  key `secure_qri` for datasets and term UUID for glossary terms.

Both become plugins with no engine code specific to them — the differences live entirely in each
connector's mapping code and manifest.

## 13. Security and trust model

- In-process plugins run with the engine's privileges; while connectors are internal this is
  acceptable. The certification kit plus code review is the trust gate.
- For a future public ecosystem, the contract is deliberately narrow (six methods + a manifest),
  which keeps a later move to out-of-process isolation (a gRPC transport behind the same `Connector`
  interface) feasible without changing connector-facing semantics. That option is explicitly deferred.

## 14. Open questions

- Whether `list_changed` should also support a connector-provided event/webhook mode now (interface
  hook) or strictly poll until a catalog needs it.
- Exact `FieldDiff` shape for full-replace vs merge fields, shared with RS-04.
- Whether the neutral model types live in the SDK or a separate shared package both SDK and engine
  depend on (leaning: in the SDK, re-exported).
- Connector-scoped rate-limit budgets declared in the manifest vs engine config.

## 15. Next steps

Extract the RS-07 adapter sketch into the `qlabs-catalog-sync-sdk` package (contract + helpers +
conformance kit), build the Databricks and Qlik connectors against it as the RM-01 MVP, and use the
capability manifest as the concrete input to the RS-04 conflict engine.

# Citations

* [Neutral Metadata Model Specification (v1)](/Research/RS-03-neutral-metadata-model/outputs/neutral-metadata-model-spec.md) — entities, identity map, and field envelopes the SDK re-exports.
* [Standalone Python Sync Service — Architecture & Tech Stack](/Research/RS-07-architecture-techstack-references/outputs/architecture-and-techstack.md) — the adapter contract and engine this SDK formalizes.
* https://packaging.python.org/en/latest/specifications/entry-points/ — Python entry points specification (plugin discovery).
* https://docs.python.org/3/library/importlib.metadata.html — importlib.metadata `entry_points` API used for discovery.
* https://packaging.python.org/en/latest/guides/creating-and-discovering-plugins/ — Python packaging guide on entry-point-based plugin systems.
