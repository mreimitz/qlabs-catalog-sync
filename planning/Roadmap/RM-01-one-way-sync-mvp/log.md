# Databricks to Qlik one-way sync MVP Update Log

## 2026-08-20

* **Plan revision**: Narrowed the MVP to a single Databricks-to-Qlik flow (Track A) and moved
  Collibra, Snowflake, and the Qlik glossary write path to Track B, blocked until v0.1 ships.
* **Decision**: Added [decision-databricks-to-qlik-mvp.md](decision-databricks-to-qlik-mvp.md) with
  the eight mappings the build needs (UC schema as data product, no Qlik dataset creation, owner
  resolution, no deletes, no glossary, SQL-gated tags, opt-in activation, async watermark contract).
* **Board**: Rebuilt `tools/agent-plan/tasks.json` — added the gate-repair, dependency-pinning,
  FakeConnector, Qlik reference-resolution, Databricks tag-read, orphan-policy and
  tenant-verification tasks; gave every task its own test directory so parallel agents never share a
  file; removed the ownership collisions and the loop/diff dependency cycle.
* **Scope split**: Moved Track B — the Collibra and Snowflake read connectors and the Qlik glossary write path — out to [RM-05](/Roadmap/RM-05-track-b-connectors-glossary/item.md), with its 15 tasks on their own board. This item is now exactly the Databricks-to-Qlik MVP and completes when v0.1 ships, instead of waiting on work that begins afterwards.

## 2026-08-06

* **Initialization**: Created roadmap item [item.md](item.md).
