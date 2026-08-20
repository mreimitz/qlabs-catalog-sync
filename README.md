# QLabs Catalog Sync

QLabs Catalog Sync is a two-way bridge for **data-product metadata** across data
catalogs (Databricks, Qlik, Snowflake, Collibra, and more).

**v1 is upstream-only:** metadata flows from source catalogs (Databricks, Collibra,
Snowflake) **into Qlik**, with Qlik as the single write target. Source connectors are
read-only. There is no reverse flow and no access-control sync in v1 — see the design
and scope docs under [`planning/`](planning/).

## Monorepo layout

Managed as a [uv](https://docs.astral.sh/uv/) workspace; every package uses a `src/`
layout.

```
packages/
  qlabs-catalog-sync-sdk/       # the public contract, neutral model, helpers, conformance kit (WP1)
  qlabs-catalog-sync/           # the engine: discovery, sync loop, state store, scheduler (WP2, WP7)
  qlabs-connector-qlik/         # sole WRITE connector                                       (WP3)
  qlabs-connector-databricks/   # read-only source connector                                 (WP4)
  qlabs-connector-collibra/     # read-only source connector                                 (WP5)
  qlabs-connector-snowflake/    # read-only source connector                                 (WP6)
planning/                       # design, research & plan — a separately-governed OKF bundle
```

**Dependency rule:** connectors depend only on the SDK (plus their vendor libraries);
the engine depends on the SDK and discovers connectors at runtime via the
`qlabs_catalog_sync.connectors` entry-point group. Nothing depends on a connector
directly.

## Where the design and plan live

All research, decisions, and the build plan live under [`planning/`](planning/), which
is a **separately-governed Open Knowledge Format (OKF) knowledge bundle** with its own
tooling and conformance rules. Highlights:

- `planning/Roadmap/RM-01-one-way-sync-mvp/implementation-plan.md` — work packages, tasks, waves.
- `planning/Roadmap/RM-01-one-way-sync-mvp/agent-guide.md` — conventions and how to add a connector.
- `planning/Roadmap/RM-01-one-way-sync-mvp/decision.md` — the v1 scope decision.

Do not hand-edit `planning/` concepts; use its own commands/tooling.

## Quickstart

```bash
uv sync              # create the venv and install all workspace packages + dev tools
uv run pytest        # run the test suite
uv run ruff check    # lint
uv run mypy          # strict type-check
```

## Finding work

The task board is machine-readable. To see everything that is ready to pick up right
now (all dependencies `done`):

```bash
python3 planning/tools/agent-plan/ready_queue.py
```

Then read `AGENTS.md` for how to claim and land a task.
