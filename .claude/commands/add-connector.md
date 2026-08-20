---
description: Scaffold a new qlabs-connector-<name> package following the RS-08 pattern and the agent-guide checklist.
---

Add a new connector package `qlabs-connector-$ARGUMENTS` to the monorepo, following the
RS-08 connector SDK pattern and the "How to add a connector" checklist in
`planning/Roadmap/RM-01-one-way-sync-mvp/agent-guide.md`. Read that checklist first.

Steps:

1. **Create the package** under `packages/qlabs-connector-<name>/` with a `src/` layout
   (`src/qlabs_connector_<name>/`), a `tests/` dir, and `py.typed`. It is picked up
   automatically by the uv workspace (`packages/*`).
2. **Write its `pyproject.toml`:** name `qlabs-connector-<name>`, version `0.0.0`,
   `requires-python = ">=3.11"`, hatchling build with
   `[tool.hatch.build.targets.wheel] packages = ["src/qlabs_connector_<name>"]`. Its
   only first-party dependency is `qlabs-catalog-sync-sdk` (via
   `[tool.uv.sources] qlabs-catalog-sync-sdk = { workspace = true }`), plus the vendor
   client library it needs.
3. **Declare the entry point** so the engine can discover it:
   ```toml
   [project.entry-points."qlabs_catalog_sync.connectors"]
   <name> = "qlabs_connector_<name>:Connector"
   ```
4. **Subclass the SDK `Connector`** in `__init__.py`: set `name` (matching the
   entry-point key) and `ConfigModel` (a `ConnectorConfig` subclass). Implement
   `capabilities()` plus `setup`, `healthcheck`, `list_changed`, `read`, and — only for
   the Qlik write target — `create`/`update`/`delete`.
5. **Respect v1 scope:** source connectors are READ-ONLY. Do not implement write paths;
   declare writable fields `ro`/`na` in the capability manifest. Qlik is the sole write
   target.
6. **Write an honest capability manifest** (entities, `identity_keys`, per-field
   `mode`, `partial_update`, `concurrency`). The engine plans strictly from it.
7. **Follow conventions:** async I/O, SDK typed exceptions, `structlog`, SDK HTTP helper
   (httpx + tenacity), no secrets in logs/state.
8. **Pass the SDK conformance kit** with `respx` unit mocks and `vcrpy` cassettes, then
   confirm `uv run ruff check .`, `uv run mypy`, and `uv run pytest` are green.
