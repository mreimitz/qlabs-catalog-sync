# Catalog sync console: endpoint management and sync selection Update Log

## 2026-08-20

* **Initialization**: Created roadmap item [item.md](item.md).
* **Decision**: Added [decision-console-config-and-selection.md](decision-console-config-and-selection.md)
  with C1-C8 — configuration moves into the state store while credentials stay outside it as named
  references, selection becomes an ordered include/exclude rule set that supersedes RM-01's D1 glob
  list, one evaluator serves both the preview and the real sync, "installing an endpoint" means
  registering an instance of an already-present connector, the console runs behind a single
  administrator credential and fails closed, and it ships inside the engine container.
* **Plan**: Added [implementation-plan.md](implementation-plan.md) — WP10 configuration store,
  WP11 selection engine and run history, WP12 REST API and generated client, WP13 console SPA on the
  `@elabs-ai` component packages, WP14 packaging, documentation and the console-driven pilot.
* **Board**: Created `tools/agent-plan/tasks-rm-06.json`, 28 tasks. `ready_queue.py` discovers it
  through its `tasks*.json` glob and resolves dependencies across boards, as it already does for
  RM-05.
* **Scope coupling**: RM-01's T9.4 (tag v0.1) now depends on this item's T14.3, so the engine and the
  console ship as one release. Three tasks here edit files RM-01 owns — `sync/loop.py` (T2.4),
  `scheduler.py` (T2.6) and the `Dockerfile` (T9.1) — and each depends on the RM-01 task that owns
  the file, so the two boards never contend for it. No RM-01 task definition was rewritten.
